"""#223 PR-A — instrumentation in claude_bridge / _http_retry / bot.

Tests that the new log lines + stream_logger events fire from each
of the 4 blind-spot sites.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from clauded import stream_logger


@pytest.fixture(autouse=True)
def _reset():
    stream_logger._reset_for_tests()
    yield
    stream_logger._reset_for_tests()


@pytest.fixture
def captured_events(monkeypatch):
    """Replace stream_logger.log_event with a list-append spy."""
    events: list = []

    def _spy(event, buffer_len=0, extra=None):
        if not stream_logger.is_enabled():
            return
        entry = dict(event) if isinstance(event, dict) else {"_obj": event}
        if extra:
            entry.update(extra)
        events.append(entry)

    monkeypatch.setattr(stream_logger, "log_event", _spy)
    stream_logger.set_enabled(True)
    return events


# ---------------------------------------------------------------------------
# Blind spot #1 / #2 — ClaudeBridge.get_context_usage / get_server_info
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_context_usage_success_emits_debug_and_stream_event(
    caplog, captured_events,
):
    """#223 AC1: success path → log.debug + ControlPlane stream event."""
    from clauded.claude_bridge import ClaudeBridge

    # Build a minimal bridge with a stub client
    bridge = ClaudeBridge.__new__(ClaudeBridge)
    bridge._active = True
    bridge._client = MagicMock()
    bridge._client.get_context_usage = AsyncMock(return_value={"percentage": 42, "tokens_used": 1000})

    caplog.set_level(logging.DEBUG, logger="clauded.claude_bridge")
    result = await bridge.get_context_usage()
    assert result == {"percentage": 42, "tokens_used": 1000}
    # At least one DEBUG line about get_context_usage
    debug_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("get_context_usage" in m for m in debug_msgs), (
        f"Expected DEBUG log for get_context_usage; got: {debug_msgs}"
    )
    # ControlPlane event captured
    cp_events = [e for e in captured_events if e.get("type") == "ControlPlane"]
    assert len(cp_events) == 1
    assert cp_events[0]["method"] == "get_context_usage"
    assert cp_events[0]["result_pct"] == 42


@pytest.mark.asyncio
async def test_get_context_usage_failure_emits_warning_and_stream_event(
    caplog, captured_events,
):
    """#223 AC1: failure path → log.warning(exc_info=True) + ControlPlane error event."""
    from clauded.claude_bridge import ClaudeBridge

    bridge = ClaudeBridge.__new__(ClaudeBridge)
    bridge._active = True
    bridge._client = MagicMock()
    bridge._client.get_context_usage = AsyncMock(side_effect=RuntimeError("boom"))

    caplog.set_level(logging.WARNING, logger="clauded.claude_bridge")
    with pytest.raises(RuntimeError, match="boom"):
        await bridge.get_context_usage()

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("get_context_usage failed" in r.getMessage() for r in warns)
    assert any(r.exc_info for r in warns), "WARNING should include exc_info"

    err_events = [e for e in captured_events if e.get("type") == "ControlPlane" and e.get("error")]
    assert len(err_events) == 1
    assert err_events[0]["method"] == "get_context_usage"


@pytest.mark.asyncio
async def test_get_server_info_success_emits_event(captured_events):
    """#223 AC1: get_server_info gets symmetric instrumentation."""
    from clauded.claude_bridge import ClaudeBridge

    bridge = ClaudeBridge.__new__(ClaudeBridge)
    bridge._active = True
    bridge._client = MagicMock()
    bridge._client.get_server_info = AsyncMock(return_value={"version": "v1.0", "commands": []})

    result = await bridge.get_server_info()
    assert result["version"] == "v1.0"
    events = [e for e in captured_events if e.get("method") == "get_server_info"]
    assert len(events) == 1
    assert events[0]["type"] == "ControlPlane"
    assert set(events[0]["result_keys"]) == {"version", "commands"}


@pytest.mark.asyncio
async def test_get_server_info_failure_emits_warning_and_event(
    caplog, captured_events,
):
    """#223 R1 tester: symmetric to get_context_usage — failure path
    must produce log.warning(exc_info=True) + ControlPlane error event."""
    from clauded.claude_bridge import ClaudeBridge

    bridge = ClaudeBridge.__new__(ClaudeBridge)
    bridge._active = True
    bridge._client = MagicMock()
    bridge._client.get_server_info = AsyncMock(side_effect=RuntimeError("si-boom"))

    caplog.set_level(logging.WARNING, logger="clauded.claude_bridge")
    with pytest.raises(RuntimeError, match="si-boom"):
        await bridge.get_server_info()

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("get_server_info failed" in r.getMessage() for r in warns)
    assert any(r.exc_info for r in warns)

    err_events = [
        e for e in captured_events
        if e.get("type") == "ControlPlane"
        and e.get("method") == "get_server_info"
        and e.get("error")
    ]
    assert len(err_events) == 1


@pytest.mark.asyncio
async def test_get_context_usage_inactive_returns_none_without_event(captured_events):
    """When bridge inactive, fast-path returns None without instrumentation."""
    from clauded.claude_bridge import ClaudeBridge

    bridge = ClaudeBridge.__new__(ClaudeBridge)
    bridge._active = False
    bridge._client = MagicMock()

    result = await bridge.get_context_usage()
    assert result is None
    # No ControlPlane event recorded (inactive bridge = fast path)
    cp = [e for e in captured_events if e.get("type") == "ControlPlane"]
    assert cp == []


# ---------------------------------------------------------------------------
# Blind spot #3 — _http_retry.safe_http transient retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_http_transient_retry_emits_stream_event(captured_events):
    """#223 AC2: each transient retry emits DiscordHTTPRetry."""
    from clauded._http_retry import safe_http
    import discord

    attempts = {"n": 0}

    async def _flaky():
        attempts["n"] += 1
        if attempts["n"] < 2:
            # Build a transient HTTP exception (status=503)
            resp = MagicMock()
            resp.status = 503
            raise discord.HTTPException(resp, "transient")
        return "ok"

    result = await safe_http(_flaky, label="testop", retries=3, backoff=0.0)
    assert result == "ok"
    retry_events = [e for e in captured_events if e.get("type") == "DiscordHTTPRetry"]
    assert len(retry_events) == 1
    assert retry_events[0]["label"] == "testop"
    assert retry_events[0]["attempt"] == 1
    assert retry_events[0]["exc_type"] == "HTTPException"


@pytest.mark.asyncio
async def test_safe_http_giveup_emits_event_with_giveup_flag(captured_events):
    """#223: exhaustion logs a final event with giveup=True."""
    from clauded._http_retry import safe_http
    import discord

    async def _always_fail():
        resp = MagicMock()
        resp.status = 503
        raise discord.HTTPException(resp, "transient")

    result = await safe_http(_always_fail, label="doomed", retries=2, backoff=0.0)
    assert result is None
    events = [e for e in captured_events if e.get("type") == "DiscordHTTPRetry"]
    giveups = [e for e in events if e.get("giveup")]
    assert len(giveups) == 1
    assert giveups[0]["label"] == "doomed"


# ---------------------------------------------------------------------------
# Blind spot #4 — render_response crash dump
# ---------------------------------------------------------------------------


def test_crash_event_payload_shape():
    """#223 AC3: pin the payload structure that bot.py emits in the
    `except Exception as exc:` branch of _render_with_retry.

    Source-level pin (mental revert: deleting the log_event call would
    still pass this test, so we also have ``test_crash_event_real_runtime``
    below for behavioral coverage — R1 tester finding).
    """
    import inspect
    from clauded import bot

    src = inspect.getsource(bot.ClaudedBot._render_with_retry)
    # Pin the crash-dump event shape
    assert '"type": "Crash"' in src
    assert '"where": "render_response"' in src
    assert "_tb.format_exc()" in src or "traceback.format_exc()" in src
    # #223 R1 product: session_id must be in payload for #224 cross-ref
    assert '"session_id"' in src and "bridge" in src


@pytest.mark.asyncio
async def test_crash_event_real_runtime_emits_with_session_id(
    captured_events, monkeypatch,
):
    """#223 R1 tester finding: replace the source-grep crash test with a
    real runtime test. Drive ``_render_with_retry`` with a renderer that
    raises a non-transient exception — the outer except must dump a Crash
    event containing bridge.session_id + thread_id + traceback.
    """
    from clauded.bot import ClaudedBot

    bot = ClaudedBot.__new__(ClaudedBot)
    bot.config = MagicMock()

    # Stub session_manager: stop_session is awaited inside crash path
    sm = MagicMock()
    sm.stop_session = AsyncMock(return_value=True)
    sm.get_stored_session = MagicMock(return_value=None)
    sm.get_lock = MagicMock()
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock()
    lock_cm.__aexit__ = AsyncMock()
    sm.get_lock.return_value = lock_cm
    bot.session_manager = sm

    # Renderer that raises a NON-transient exception (so we hit the
    # crash branch, not the transient-recovery branch)
    renderer = MagicMock()
    renderer.render_response = AsyncMock(side_effect=ValueError("renderer-boom"))
    renderer.send_error_with_retry = AsyncMock()

    # Bridge with a known session_id
    bridge = MagicMock()
    bridge.session_id = "sess-runtime-id-xyz"

    thread = MagicMock()
    thread.id = 99999

    # Build SessionConfig stub so retry-closure setup doesn't blow up
    from clauded.session_config import SessionConfig

    # ``_render_with_retry`` is bound; pass through __get__
    bound = ClaudedBot._render_with_retry.__get__(bot)

    # Drive it. The crash branch runs but the retry button itself only
    # triggers if user clicks — we just need the crash event to fire.
    await bound(
        renderer=renderer,
        bridge=bridge,
        user_text="hi",
        thread=thread,
        project_path=MagicMock(),
        session_config=SessionConfig(),
        author_id=12345,
    )

    # Assert Crash event was emitted with full payload
    crash_events = [e for e in captured_events if e.get("type") == "Crash"]
    assert len(crash_events) == 1, (
        f"Expected 1 Crash event; got {len(crash_events)}; "
        f"all events: {captured_events}"
    )
    crash = crash_events[0]
    assert crash["where"] == "render_response"
    assert crash["thread_id"] == 99999
    assert crash["session_id"] == "sess-runtime-id-xyz"
    assert crash["exc_class"] == "ValueError"
    assert "renderer-boom" in crash["traceback"]


# ---------------------------------------------------------------------------
# stream_logger imports are wired
# ---------------------------------------------------------------------------


def test_claude_bridge_imports_stream_logger():
    """Regression: claude_bridge.py must import stream_logger module."""
    import inspect
    from clauded import claude_bridge

    src = inspect.getsource(claude_bridge)
    assert "from . import stream_logger" in src or "import stream_logger" in src


def test_http_retry_imports_stream_logger():
    import inspect
    from clauded import _http_retry

    src = inspect.getsource(_http_retry)
    assert "stream_logger" in src


def test_bot_imports_stream_logger():
    import inspect
    from clauded import bot

    src = inspect.getsource(bot)
    assert "stream_logger" in src
