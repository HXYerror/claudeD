"""#224 — diagnostic bundle / log dump tests.

Coverage:
- redact_env: allowlist + sensitive-pattern + drop-unknown
- redact_path: macOS + Linux home prefix rewrite
- redact_text: multi-occurrence
- redact_projects_json / redact_sessions_json: schema-aware redaction
- generate_bundle: zip structure, manifest, no sensitive env leaks,
  state file redaction, size budget truncation
- log_dump cog: source-grep on the command shape
- auto-crash dispatcher: rate-limit + bundle generation
"""
from __future__ import annotations

import inspect
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from clauded.diagnostics import bundle as bundle_mod
from clauded.diagnostics import redact


# ---------------------------------------------------------------------------
# redact_env
# ---------------------------------------------------------------------------


def test_redact_env_keeps_allowlisted():
    env = {"PATH": "/usr/bin", "HOME": "/Users/test", "LANG": "en_US.UTF-8"}
    out = redact.redact_env(env)
    assert out["PATH"] == "/usr/bin"
    assert out["HOME"] == "/Users/test"
    assert out["LANG"] == "en_US.UTF-8"


def test_redact_env_keeps_lc_prefix():
    out = redact.redact_env({"LC_ALL": "C", "LC_CTYPE": "UTF-8"})
    assert out["LC_ALL"] == "C"
    assert out["LC_CTYPE"] == "UTF-8"


def test_redact_env_drops_unknown_keys():
    """Unknown keys are silently dropped — keeps bundle small + leak-safe."""
    out = redact.redact_env({"MY_RANDOM_VAR": "value", "PATH": "/x"})
    assert "MY_RANDOM_VAR" not in out
    assert "PATH" in out


@pytest.mark.parametrize("key,value", [
    ("DISCORD_BOT_TOKEN", "actual-token-string-here-32chars"),
    ("ANTHROPIC_API_KEY", "sk-ant-abc123"),
    ("GITHUB_TOKEN", "ghp_xxx"),
    ("MY_SECRET_KEY", "hush-value-x"),
    ("APP_PASSWORD", "hush-value-y"),
    ("PRIVATE_KEY_PEM", "-----BEGIN-this-is-a-long-pem-string-here-32+chars"),
])
def test_redact_env_redacts_sensitive_pattern(key, value):
    """Any key matching TOKEN/SECRET/API_KEY/PASSWORD/PRIVATE_KEY/PASSPHRASE
    gets replaced with a redaction marker (long values: ``len=N sha256=<first8>``;
    short values: ``len=N <redacted-short>`` per R1 security finding).
    """
    out = redact.redact_env({key: value})
    assert key in out
    assert value not in out[key], f"actual value leaked for {key}: {out[key]!r}"
    assert out[key].startswith("len=")
    # Either a hash (long values) or the explicit short marker.
    assert "sha256=" in out[key] or "<redacted-short>" in out[key]


def test_redact_env_sensitive_wins_over_allowlist():
    """Even if a sensitive-pattern key were allowlisted, redaction wins.

    Today the allowlist doesn't include anything matching the sensitive
    pattern, but pin the precedence so a future allowlist addition can't
    accidentally leak a token.
    """
    env = {"PATH_TOKEN": "totally-not-a-secret"}
    out = redact.redact_env(env)
    if "PATH_TOKEN" in out:
        assert "totally-not-a-secret" not in out["PATH_TOKEN"]


def test_redact_env_handles_empty():
    assert redact.redact_env({}) == {}


# ---------------------------------------------------------------------------
# redact_path
# ---------------------------------------------------------------------------


def test_redact_path_macos():
    out = redact.redact_path("/Users/alice/projects/foo", username="alice")
    assert out == "/Users/<user>/projects/foo"


def test_redact_path_linux():
    out = redact.redact_path("/home/bob/src/main.py", username="bob")
    assert out == "/home/<user>/src/main.py"


def test_redact_path_no_match():
    """Paths not under /Users/<user>/ or /home/<user>/ pass through."""
    assert redact.redact_path("/tmp/foo", username="bob") == "/tmp/foo"
    assert redact.redact_path("/var/log/x", username="bob") == "/var/log/x"
    assert redact.redact_path("relative/path.png", username="bob") == "relative/path.png"


def test_redact_path_only_target_username():
    """Should NOT redact other users' paths (only the operator's)."""
    out = redact.redact_path("/Users/somebody-else/secret", username="alice")
    assert out == "/Users/somebody-else/secret"


# ---------------------------------------------------------------------------
# redact_text
# ---------------------------------------------------------------------------


def test_redact_text_multi_occurrence():
    txt = (
        "Working in /Users/alice/proj\n"
        "Wrote to /Users/alice/proj/a.txt\n"
        "Done.\n"
    )
    out = redact.redact_text(txt, username="alice")
    assert "/Users/alice" not in out
    assert out.count("/Users/<user>") == 2


# ---------------------------------------------------------------------------
# JSON-shape redactors
# ---------------------------------------------------------------------------


def test_redact_projects_json_rewrites_paths():
    data = {
        "1234": {
            "path": "/Users/alice/repo",
            "add_dirs": ["/Users/alice/extra", "/tmp/scratch"],
        },
    }
    out = redact.redact_projects_json(data, username="alice")
    assert out["1234"]["path"] == "/Users/<user>/repo"
    assert out["1234"]["add_dirs"] == ["/Users/<user>/extra", "/tmp/scratch"]


def test_redact_sessions_json_redacts_system_prompt():
    data = {
        "999": {
            "session_id": "abc-123",
            "project_path": "/Users/alice/repo",
            "system_prompt": "secret instructions here",
        },
    }
    out = redact.redact_sessions_json(data, username="alice")
    assert out["999"]["project_path"] == "/Users/<user>/repo"
    assert "secret instructions" not in out["999"]["system_prompt"]
    assert out["999"]["system_prompt"].startswith("len=")
    # session_id is verbatim (not sensitive — it's a server-side ID)
    assert out["999"]["session_id"] == "abc-123"


# ---------------------------------------------------------------------------
# bundle.generate_bundle — pipeline integration
# ---------------------------------------------------------------------------


def _build_bot_stub(*, has_session_manager=False):
    """Minimal duck-typed bot for bundle generation."""
    import time
    bot = MagicMock()
    bot._start_time = time.time() - 60  # 60s uptime
    bot._claude_version = "1.0.0-test"
    bot._debug_logging = False
    bot._pre_tool_notifications = True
    bot._notify_enabled = {}
    bot._allow_unbound_fallback = False
    bot._stream_debug_enabled = False
    if has_session_manager:
        bridge = MagicMock()
        bridge.project_path = "/Users/alice/repo"
        bridge.session_id = "sess-xyz"
        bridge.is_active = True
        bridge.total_cost = 0.05
        bridge.num_turns = 3
        bridge._sdk_model = "claude-sonnet-4"
        bridge._model_override = None
        bridge._permission_mode_override = None
        bridge.system_prompt = "you are a helpful assistant"
        bot.session_manager.list_sessions = MagicMock(return_value={42: bridge})
    else:
        bot.session_manager = None
    return bot


def test_generate_bundle_produces_zip(tmp_path):
    """Smoke: bundle is a parseable .zip with the expected entries."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Plant a state file so we have something to redact
    (data_dir / "projects.json").write_text(
        json.dumps({"1": {"path": "/Users/alice/repo"}})
    )
    out_path = bundle_mod.generate_bundle(
        data_dir=data_dir,
        out_dir=tmp_path,
        log_dir=tmp_path / "fake-logs",  # missing → tail logs absent
    )
    assert out_path.exists()
    with zipfile.ZipFile(out_path) as z:
        names = set(z.namelist())
    assert "manifest.json" in names
    assert "env-redacted.txt" in names
    assert "state/projects.json" in names
    # #224 R1 simplicity: diagnostics/info.json merged into manifest;
    # no separate diagnostics/ directory.
    assert "diagnostics/info.json" not in names


def test_generate_bundle_manifest_shape(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    out_path = bundle_mod.generate_bundle(
        data_dir=data_dir,
        out_dir=tmp_path,
        log_dir=tmp_path / "x",
        generated_by="slash",
    )
    with zipfile.ZipFile(out_path) as z:
        manifest = json.loads(z.read("manifest.json"))
    assert manifest["bundle_version"] == 1
    assert manifest["generated_by"] == "slash"
    assert manifest["bot_pid"] == os.getpid()
    assert isinstance(manifest["generated_at"], str)
    assert manifest["generated_at"].endswith("Z")
    # #224 R1 simplicity: diagnostics/info.json merged into manifest.
    assert "python_executable" in manifest
    assert manifest["python_executable"] == sys.executable


def test_generate_bundle_no_token_leaks(tmp_path, monkeypatch):
    """#224 AC3: DISCORD_BOT_TOKEN / ANTHROPIC_API_KEY must NOT appear
    anywhere in the bundle."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "supersecret-discord-bot-token-xxx")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-totally-secret")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    out_path = bundle_mod.generate_bundle(
        data_dir=data_dir,
        out_dir=tmp_path,
        log_dir=tmp_path / "x",
    )

    # Read every entry's bytes and grep for the secrets
    with zipfile.ZipFile(out_path) as z:
        for name in z.namelist():
            data = z.read(name)
            assert b"supersecret-discord-bot-token-xxx" not in data, (
                f"#224 AC3: DISCORD_BOT_TOKEN leaked into {name}"
            )
            assert b"sk-ant-totally-secret" not in data, (
                f"#224 AC3: ANTHROPIC_API_KEY leaked into {name}"
            )


def test_generate_bundle_redacts_user_paths(tmp_path, monkeypatch):
    """#224 AC4: /Users/<actual> must not appear in state/runtime/."""
    monkeypatch.setattr(redact, "_current_username", lambda: "alice")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "projects.json").write_text(
        json.dumps({"1": {"path": "/Users/alice/secret-project"}})
    )
    out_path = bundle_mod.generate_bundle(
        data_dir=data_dir,
        out_dir=tmp_path,
        log_dir=tmp_path / "x",
    )
    with zipfile.ZipFile(out_path) as z:
        proj = z.read("state/projects.json")
    assert b"/Users/alice/" not in proj, (
        "#224 AC4: /Users/<actual>/ leaked into state/projects.json"
    )
    assert b"/Users/<user>" in proj


def test_generate_bundle_runtime_snapshot_when_bot_present(tmp_path):
    bot = _build_bot_stub(has_session_manager=True)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    out_path = bundle_mod.generate_bundle(
        bot=bot,
        data_dir=data_dir,
        out_dir=tmp_path,
        log_dir=tmp_path / "x",
    )
    with zipfile.ZipFile(out_path) as z:
        names = set(z.namelist())
        sessions = json.loads(z.read("runtime/sessions-live.json"))
        flags = json.loads(z.read("runtime/bot-flags.json"))
    assert "runtime/sessions-live.json" in names
    assert "runtime/bot-flags.json" in names
    assert len(sessions) == 1
    s = sessions[0]
    assert s["session_id"] == "sess-xyz"
    assert s["thread_id"] == 42
    # system_prompt verbatim is sensitive — must be a digest marker, not the text
    assert "you are a helpful assistant" not in json.dumps(s)
    assert s["system_prompt_marker"].startswith("len=")
    # Bot flags landed
    assert flags["_pre_tool_notifications"] is True


def test_generate_bundle_omits_missing_state_files(tmp_path):
    """Missing state files don't produce phantom empty entries."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Only projects.json plant
    (data_dir / "projects.json").write_text("{}")
    out_path = bundle_mod.generate_bundle(
        data_dir=data_dir,
        out_dir=tmp_path,
        log_dir=tmp_path / "x",
    )
    with zipfile.ZipFile(out_path) as z:
        names = set(z.namelist())
    assert "state/projects.json" in names
    # Other state files absent (not phantom-empty)
    assert "state/sessions.json" not in names
    assert "state/costs.json" not in names


def test_generate_bundle_tails_logs(tmp_path, monkeypatch):
    """Logs are tailed (not full-copied) when present."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    big_log = log_dir / "clauded.log"
    big_log.write_text("X" * 50)  # tiny — tail returns whole file
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    out_path = bundle_mod.generate_bundle(
        data_dir=data_dir,
        out_dir=tmp_path,
        log_dir=log_dir,
    )
    with zipfile.ZipFile(out_path) as z:
        names = set(z.namelist())
        clauded_log_bytes = z.read("logs/clauded.log")
    assert "logs/clauded.log" in names
    assert len(clauded_log_bytes) <= 50


# ---------------------------------------------------------------------------
# /log dump cog
# ---------------------------------------------------------------------------


def test_log_dump_cog_registered_at_module_level():
    """The cog exposes a Group named 'log' with a 'dump' subcommand."""
    from clauded.cogs.log_dump import log_group

    assert log_group.name == "log"
    sub_names = {c.name for c in log_group.commands}
    assert "dump" in sub_names


def test_log_dump_uses_executor():
    """Bundle generation runs in an executor (not the event loop)."""
    from clauded.cogs import log_dump as cog
    src = inspect.getsource(cog)
    assert "run_in_executor" in src
    assert "bundle_mod.generate_bundle" in src or "generate_bundle" in src


def test_log_dump_defers_interaction():
    """``response.defer`` is called (bundle gen takes >3s)."""
    from clauded.cogs import log_dump as cog
    src = inspect.getsource(cog)
    assert "interaction.response.defer" in src


# ---------------------------------------------------------------------------
# Auto-crash dispatcher
# ---------------------------------------------------------------------------


def test_auto_crash_method_exists_on_bot():
    """ClaudedBot._maybe_dispatch_auto_crash_bundle is defined."""
    from clauded.bot import ClaudedBot
    assert hasattr(ClaudedBot, "_maybe_dispatch_auto_crash_bundle")


def test_auto_crash_called_from_render_with_retry():
    """The crash branch of _render_with_retry calls the dispatcher."""
    from clauded.bot import ClaudedBot
    src = inspect.getsource(ClaudedBot._render_with_retry)
    assert "_maybe_dispatch_auto_crash_bundle" in src, (
        "#224: _render_with_retry must call _maybe_dispatch_auto_crash_bundle "
        "on the crash branch"
    )


@pytest.mark.asyncio
async def test_auto_crash_rate_limit():
    """Same thread within cooldown → second call is skipped (no bundle)."""
    from clauded.bot import ClaudedBot
    import time as _time

    bot = ClaudedBot.__new__(ClaudedBot)
    bot._auto_crash_last_dispatch = {42: _time.time()}  # just dispatched

    thread = MagicMock()
    thread.id = 42
    exc = RuntimeError("test")

    # Patch generate_bundle to detect if it was called
    called = {"n": 0}
    from clauded.diagnostics import bundle as bundle_mod_ref
    orig = bundle_mod_ref.generate_bundle
    def _spy(*a, **kw):
        called["n"] += 1
        return Path("/dev/null")
    bundle_mod_ref.generate_bundle = _spy
    try:
        await bot._maybe_dispatch_auto_crash_bundle(thread=thread, exc=exc, bridge=None)
    finally:
        bundle_mod_ref.generate_bundle = orig
    assert called["n"] == 0, "rate-limited bundle must not be generated"


@pytest.mark.asyncio
async def test_auto_crash_dispatches_when_cooldown_expired(tmp_path, monkeypatch):
    """When cooldown expired, bundle is generated + uploaded."""
    from clauded.bot import ClaudedBot
    bot = ClaudedBot.__new__(ClaudedBot)
    # No prior dispatch
    bot._auto_crash_last_dispatch = {}
    bot.session_manager = None
    bot._start_time = 0
    bot._claude_version = "test"

    # Stub generate_bundle to return a tmp file
    fake_zip = tmp_path / "fake.zip"
    fake_zip.write_bytes(b"PK\x03\x04fake")

    from clauded.diagnostics import bundle as bundle_mod_ref
    orig = bundle_mod_ref.generate_bundle
    captured = {}
    def _spy(*a, **kw):
        captured["called"] = True
        captured["kwargs"] = kw
        return fake_zip
    bundle_mod_ref.generate_bundle = _spy

    # Stub safe_send_message
    from clauded import bot as bot_mod
    orig_send = bot_mod.safe_send_message
    sent_args = []
    async def _send_spy(target, **kwargs):
        sent_args.append((target, kwargs))
        return MagicMock()
    bot_mod.safe_send_message = _send_spy

    thread = MagicMock()
    thread.id = 99
    try:
        await bot._maybe_dispatch_auto_crash_bundle(
            thread=thread, exc=RuntimeError("planted"), bridge=None,
        )
    finally:
        bundle_mod_ref.generate_bundle = orig
        bot_mod.safe_send_message = orig_send

    assert captured.get("called"), "generate_bundle must be called"
    assert captured["kwargs"]["generated_by"] == "auto-crash"
    assert sent_args, "bundle must be uploaded to the thread"
    assert sent_args[0][0] is thread


# ---------------------------------------------------------------------------
# R1 retrofits
# ---------------------------------------------------------------------------


def test_r1_env_redacts_home_value(monkeypatch):
    """R1 security: HOME / PWD values get path-redacted so the operator's
    username doesn't leak via env-redacted.txt."""
    monkeypatch.setattr(redact, "_current_username", lambda: "alice")
    out = redact.redact_env({
        "HOME": "/Users/alice",
        "PWD": "/Users/alice/proj",
        "USER": "alice",
        "PATH": "/usr/bin",
    })
    assert out["HOME"] == "/Users/<user>"
    assert out["PWD"] == "/Users/<user>/proj"
    assert out["USER"] == "<user>"
    assert out["PATH"] == "/usr/bin"  # unchanged


def test_r1_env_user_passes_other_users():
    """If USER somehow equals an OTHER user (not the current process
    owner), don't redact — it might be useful debug info."""
    out = redact.redact_env({"USER": "claude-runner"})
    assert out["USER"] == "claude-runner"


def test_r1_env_extended_sensitive_pattern():
    """R1 security: pattern extended to AUTH/OAUTH/BEARER/JWT/COOKIE/SIGNING/CREDENTIAL."""
    for key, val in [
        ("AUTH_TOKEN_X", "secret"),  # AUTH AND TOKEN — either should match
        ("OAUTH_CLIENT", "secret"),
        ("BEARER_TKN", "secret"),
        ("JWT_KEY", "secret"),  # JWT AND KEY — either should match
        ("COOKIE_VAL", "secret"),
        ("SIGNING_KEY_PROD", "secret"),
        ("CREDENTIALS_FILE", "secret"),
    ]:
        out = redact.redact_env({key: val})
        assert key in out
        assert "secret" not in out[key], f"{key} leaked: {out[key]!r}"


def test_r1_digest_marker_short_value_no_hash():
    """R1 security: short values get `<redacted-short>` instead of brute-forceable hash."""
    out = redact._digest_marker("1234")  # 4-char PIN
    assert "sha256" not in out
    assert "<redacted-short>" in out
    assert "len=4" in out


def test_r1_digest_marker_long_value_keeps_hash():
    """Long values keep the hash — useful for fingerprinting without brute-force risk."""
    out = redact._digest_marker("this-is-a-very-long-token-string-32+chars")
    assert "sha256=" in out
    assert "len=" in out


def test_r1_bundle_jsonl_tail_path_redacted(tmp_path, monkeypatch):
    """R1 security: stream-debug.jsonl tail now also gets path-redacted.

    Previous code skipped jsonl with wrong rationale ("would break parsing").
    """
    monkeypatch.setattr(redact, "_current_username", lambda: "alice")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    jsonl = log_dir / "stream-debug.jsonl"
    jsonl.write_text(
        '{"type":"ControlPlane","path":"/Users/alice/repo/foo"}\n'
        '{"type":"DiscordHTTPRetry","url":"/Users/alice/scratch"}\n'
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    out_path = bundle_mod.generate_bundle(
        data_dir=data_dir,
        out_dir=tmp_path,
        log_dir=log_dir,
    )
    with zipfile.ZipFile(out_path) as z:
        jsonl_data = z.read("logs/stream-debug.tail.jsonl")
    assert b"/Users/alice" not in jsonl_data, (
        "#224 R1 security: jsonl tail must be path-redacted"
    )
    assert b"/Users/<user>" in jsonl_data


def test_r1_bundle_crash_context_traceback_redacted(tmp_path, monkeypatch):
    """R1 security: crash traceback in manifest is path-redacted."""
    monkeypatch.setattr(redact, "_current_username", lambda: "alice")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    crash_context = {
        "where": "render_response",
        "thread_id": 42,
        "exc_class": "RuntimeError",
        "exc_message": "failed in /Users/alice/proj/foo.py line 99",
        "traceback": [
            'File "/Users/alice/.venv/lib/foo.py", line 1, in bar\n',
            'File "/Users/alice/repo/main.py", line 2, in baz\n',
        ],
    }
    out_path = bundle_mod.generate_bundle(
        data_dir=data_dir,
        out_dir=tmp_path,
        log_dir=tmp_path / "x",
        generated_by="auto-crash",
        crash_context=crash_context,
    )
    with zipfile.ZipFile(out_path) as z:
        manifest = json.loads(z.read("manifest.json"))
    tb_text = " ".join(manifest["crash_context"]["traceback"])
    assert "/Users/alice" not in tb_text
    assert "/Users/<user>" in tb_text
    assert "/Users/alice" not in manifest["crash_context"]["exc_message"]


def test_r1_bundle_emergency_truncate_drops_logs(tmp_path):
    """R1 tester: AC5 size budget overflow → logs/ dropped, manifest+state kept."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    # Plant a giant log of HIGH-ENTROPY data so DEFLATE can't compress it
    # below the budget. 200 KB of compressed-resistant bytes.
    import secrets
    big = log_dir / "clauded.log"
    big.write_bytes(secrets.token_bytes(200 * 1024))
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "projects.json").write_text(json.dumps({"1": {"path": "/x"}}))
    # Force overrun with a tiny budget
    out_path = bundle_mod.generate_bundle(
        data_dir=data_dir,
        out_dir=tmp_path,
        log_dir=log_dir,
        size_budget=5 * 1024,  # 5 KB
    )
    with zipfile.ZipFile(out_path) as z:
        names = set(z.namelist())
    assert "logs/clauded.log" not in names, (
        "#224 R1 tester: emergency truncate must drop logs/ entries"
    )
    assert "logs/TRUNCATED.txt" in names
    # Manifest + state preserved
    assert "manifest.json" in names
    assert "state/projects.json" in names


def test_r1_bundle_no_diagnostics_directory(tmp_path):
    """R1 simplicity: 4 dirs → 3 dirs (diagnostics/ merged into manifest)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    out_path = bundle_mod.generate_bundle(
        data_dir=data_dir,
        out_dir=tmp_path,
        log_dir=tmp_path / "x",
    )
    with zipfile.ZipFile(out_path) as z:
        dirs = {n.split("/")[0] for n in z.namelist() if "/" in n}
    assert "diagnostics" not in dirs


def test_r1_module_level_auto_crash_cooldown():
    """R1 simplicity: cooldown is now module-level, not class-attr."""
    from clauded import bot as bot_mod
    assert hasattr(bot_mod, "AUTO_CRASH_COOLDOWN_S")
    assert bot_mod.AUTO_CRASH_COOLDOWN_S == 300


def test_r1_stream_debug_jsonl_renamed_in_archive(tmp_path):
    """R1 tester: stream-debug.jsonl is archived as stream-debug.tail.jsonl."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "stream-debug.jsonl").write_text('{"a":1}\n')
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    out_path = bundle_mod.generate_bundle(
        data_dir=data_dir,
        out_dir=tmp_path,
        log_dir=log_dir,
    )
    with zipfile.ZipFile(out_path) as z:
        names = set(z.namelist())
    assert "logs/stream-debug.tail.jsonl" in names
    assert "logs/stream-debug.jsonl" not in names


def test_r1_bot_flags_includes_runtime_overrides():
    """R1 tester: AC7 — runtime sessions snapshot must include model_override etc."""
    bot = _build_bot_stub(has_session_manager=True)
    snap = bundle_mod._snapshot_live_sessions(bot)
    assert len(snap) == 1
    s = snap[0]
    # AC7 needs PM to see the override matrix
    assert "model_override" in s
    assert "sdk_model" in s
    assert "permission_mode_override" in s


@pytest.mark.asyncio
async def test_r1_log_dump_cog_runtime_dispatch(tmp_path, monkeypatch):
    """R1 tester: real-runtime test for the /log dump command.

    Drive the callback with a mock interaction; assert (a) defer fires,
    (b) generate_bundle invoked, (c) followup with file attachment.
    """
    from clauded.cogs.log_dump import log_dump

    # Stub the underlying coroutine: log_dump is an app_commands.Command,
    # find its callback (callback or _callback).
    callback = getattr(log_dump, "callback", None) or log_dump._callback

    # Patch bundle generation to a tmp file
    fake_zip = tmp_path / "fake.zip"
    fake_zip.write_bytes(b"PK\x03\x04")
    from clauded.diagnostics import bundle as _bundle_mod
    orig = _bundle_mod.generate_bundle
    _bundle_mod.generate_bundle = lambda **kw: fake_zip
    try:
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        interaction.client = MagicMock()

        await callback(interaction)

        # AC1: defer fired
        interaction.response.defer.assert_called_once()
        # AC1: followup with file
        interaction.followup.send.assert_called_once()
        kwargs = interaction.followup.send.call_args.kwargs
        assert "file" in kwargs
    finally:
        _bundle_mod.generate_bundle = orig
