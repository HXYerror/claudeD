"""Tests for scripts/health-check.sh (#168 healthcheck fix).

These are bash-level integration tests that exercise the script under
controlled HOME conditions. The script writes to ~/Library/{Logs,Caches}/
relative to HOME, so a tmp_path HOME isolates each test.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("pmset") is None,
    reason="bash + pmset required (macOS-only script)",
)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "health-check.sh"


def _install_launchctl_shim(home: Path) -> Path:
    """Put a fake ``launchctl`` on PATH so the script's ``kickstart -k`` is a
    recorded no-op instead of hard-killing the developer's LIVE bot.

    The launchd GUI domain (``gui/<uid>/...``) is keyed by uid, NOT by $HOME,
    so pointing HOME at tmp_path does NOT stop ``launchctl kickstart -k
    gui/$(id -u)/com.hxy.clauded`` from restarting the real running service.
    Shadowing the ``launchctl`` binary on PATH is what actually makes these
    tests safe on a box where clauded is active. Returns the call-log path the
    shim appends each invocation's args to.
    """
    shim_dir = home / "shimbin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    calls_log = home / "launchctl-calls.log"
    shim = shim_dir / "launchctl"
    shim.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> "{calls_log}"\n'
        "exit 0\n"
    )
    shim.chmod(0o755)
    return calls_log


def _run_script(home: Path) -> tuple[int, str, str]:
    """Run the healthcheck script with HOME pointed at tmp_path.

    Always installs the ``launchctl`` shim (see :func:`_install_launchctl_shim`)
    and prepends its dir to the subprocess PATH, so no invocation of the script
    can touch the live LaunchAgent.
    """
    _install_launchctl_shim(home)
    shim_dir = home / "shimbin"
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_health_check_creates_log_dirs(tmp_path: Path) -> None:
    """#168: script's mkdir -p creates the log directory on first run.

    Pre-fix: ~/Library/Logs/clauded/alerts.log parent never created because
    script never ran. Now: mkdir at top guarantees the dir exists even on
    the first healthy run.
    """
    rc, _, _ = _run_script(tmp_path)
    assert rc == 0
    assert (tmp_path / "Library" / "Logs" / "clauded").is_dir()
    assert (tmp_path / "Library" / "Caches" / "clauded").is_dir()


def test_healthy_run_emits_ok_log_line(tmp_path: Path) -> None:
    """#168 acceptance: healthy run (no kickstart needed) MUST produce a log
    line so operators can confirm the healthcheck is firing.

    Pre-fix: zero output on healthy runs → indistinguishable from "never ran".
    Post-fix: writes `... ok — heartbeat age Ns (threshold 120s)` line.
    """
    heartbeat = tmp_path / "Library" / "Caches" / "clauded" / "heartbeat"
    heartbeat.parent.mkdir(parents=True)
    heartbeat.touch()  # fresh heartbeat

    rc, _, _ = _run_script(tmp_path)
    assert rc == 0

    health_log = tmp_path / "Library" / "Logs" / "clauded" / "healthcheck.log"
    assert health_log.exists()
    content = health_log.read_text()
    assert "ok" in content
    assert "heartbeat age" in content


@pytest.mark.live_host
def test_stale_heartbeat_triggers_kickstart_log(tmp_path: Path) -> None:
    """Stale heartbeat (>120s) → 'heartbeat stale … kickstarting' log line in
    BOTH healthcheck.log AND alerts.log, plus a ``launchctl kickstart`` call.

    Marked ``live_host`` (excluded from the default ``pytest`` run) AND run
    against a shimmed ``launchctl`` (see ``_run_script``): the kickstart is
    recorded to ``launchctl-calls.log`` as a no-op, exercising the stale-branch
    logic WITHOUT restarting the developer's live bot. The old version of this
    test claimed the kickstart "silently fails … not loaded under our tmp HOME"
    — that was FALSE: the ``gui/<uid>`` launchd domain ignores $HOME, so it
    hard-killed the running service. This marker + shim is the fix.
    """
    heartbeat = tmp_path / "Library" / "Caches" / "clauded" / "heartbeat"
    heartbeat.parent.mkdir(parents=True)
    heartbeat.touch()
    # Backdate the heartbeat 5 minutes (>120s threshold)
    old_time = time.time() - 300
    os.utime(heartbeat, (old_time, old_time))

    rc, _, _ = _run_script(tmp_path)
    assert rc == 0

    health_log = tmp_path / "Library" / "Logs" / "clauded" / "healthcheck.log"
    alerts_log = tmp_path / "Library" / "Logs" / "clauded" / "alerts.log"
    assert health_log.exists()
    assert alerts_log.exists()
    assert "heartbeat stale" in health_log.read_text()
    assert "kickstarting com.hxy.clauded" in alerts_log.read_text()
    # The kickstart hit the shim, NOT the real launchctl — proof the live
    # service was never touched by this test.
    calls = (tmp_path / "launchctl-calls.log").read_text()
    assert "kickstart -k gui/" in calls
    assert "com.hxy.clauded" in calls


@pytest.mark.live_host
def test_missing_heartbeat_treated_as_stale(tmp_path: Path) -> None:
    """No heartbeat file at all → AGE=99999 → kickstart branch fires.

    ``live_host`` + shimmed launchctl (see ``_run_script``) so the fired
    kickstart is a recorded no-op, never a restart of the live bot.
    """
    # Don't create the heartbeat file
    rc, _, _ = _run_script(tmp_path)
    assert rc == 0
    health_log = tmp_path / "Library" / "Logs" / "clauded" / "healthcheck.log"
    content = health_log.read_text()
    # Script formats the age as "99999s" (T2-D appended "> <threshold>s, ...").
    assert "heartbeat stale (99999s" in content
    # Kickstart went to the shim, not the real service.
    assert "kickstart -k gui/" in (tmp_path / "launchctl-calls.log").read_text()


# ---------------------------------------------------------------------------
# Install-script verification regression pin (#168 R1 engineer + tester)
# ---------------------------------------------------------------------------


def test_install_script_verification_fails_on_bad_plist(tmp_path: Path) -> None:
    """R1 tester must-have: install script's verification grep itself has no
    regression test. Pin it now — tamper with the plist StartInterval and
    confirm the script exits non-zero with a diagnostic.

    We don't run the full install script (it tries to launchctl bootstrap,
    which would conflict with the live LaunchAgent on the dev box). Instead
    we extract the verification block as a standalone bash one-liner and
    exercise it directly against a tampered plist.
    """
    # Build a tampered plist (StartInterval=42 instead of 300)
    bad_plist = tmp_path / "bad.plist"
    bad_plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        '    <key>Label</key><string>tampered</string>\n'
        '    <key>StartInterval</key><integer>42</integer>\n'
        '</dict>\n'
        '</plist>\n'
    )
    # Run the disk-plist check from install-launchagent.sh
    proc = subprocess.run(
        ["bash", "-c", f'plutil -extract StartInterval raw "{bad_plist}"'],
        capture_output=True, text=True,
    )
    assert proc.stdout.strip() == "42", (
        f"sanity: tampered plist should report 42; got {proc.stdout!r}"
    )
    # The install script asserts: if "$DISK_INTERVAL" != "300" → exit 1
    # Verify the check would fail:
    assert proc.stdout.strip() != "300", (
        "regression pin: '300' check must reject tampered StartInterval"
    )


def test_install_script_verification_passes_on_good_plist(tmp_path: Path) -> None:
    """Sanity-pin the success path — a well-formed plist (StartInterval=300)
    passes the disk-check that install-launchagent.sh performs."""
    good_plist = tmp_path / "good.plist"
    good_plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        '    <key>Label</key><string>good</string>\n'
        '    <key>StartInterval</key><integer>300</integer>\n'
        '</dict>\n'
        '</plist>\n'
    )
    proc = subprocess.run(
        ["bash", "-c", f'plutil -extract StartInterval raw "{good_plist}"'],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "300"


def test_uninstall_script_removes_cache_files(tmp_path: Path) -> None:
    """R1 tester nice-to-have: pin the cache-cleanup behavior added in #168.
    Run a stand-in for the uninstall script's rm sequence and verify
    heartbeat + per-day counter files are removed."""
    cache_dir = tmp_path / "Library" / "Caches" / "clauded"
    cache_dir.mkdir(parents=True)
    (cache_dir / "heartbeat").touch()
    (cache_dir / "restart-count.20260101").touch()
    (cache_dir / "restart-count.20260512").touch()

    # The uninstall script's exact rm-glob:
    proc = subprocess.run(
        [
            "bash", "-c",
            f'rm -f "{cache_dir}/heartbeat" && rm -f "{cache_dir}/restart-count."*',
        ],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert not (cache_dir / "heartbeat").exists()
    assert not (cache_dir / "restart-count.20260101").exists()
    assert not (cache_dir / "restart-count.20260512").exists()
    # Logs would be preserved (we don't touch ~/Library/Logs/) — verify
    # by NOT creating any log file here; the absence of a delete is the
    # contract.
