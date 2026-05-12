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


def _run_script(home: Path) -> tuple[int, str, str]:
    """Run the healthcheck script with HOME pointed at tmp_path."""
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env={**os.environ, "HOME": str(home)},
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


def test_stale_heartbeat_triggers_kickstart_log(tmp_path: Path) -> None:
    """Stale heartbeat (>120s) → 'heartbeat stale … kickstarting' log line
    in BOTH healthcheck.log AND alerts.log. Kickstart itself silently fails
    in this test env (gui/$(id -u)/com.hxy.clauded not loaded under our
    tmp HOME), which is fine — we only assert log shape, not real recovery.
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


def test_missing_heartbeat_treated_as_stale(tmp_path: Path) -> None:
    """No heartbeat file at all → AGE=99999 → kickstart branch fires."""
    # Don't create the heartbeat file
    rc, _, _ = _run_script(tmp_path)
    assert rc == 0
    health_log = tmp_path / "Library" / "Logs" / "clauded" / "healthcheck.log"
    content = health_log.read_text()
    assert "heartbeat stale (99999 s)" in content
