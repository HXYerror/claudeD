"""#210 — read-side leak fix: stored.model never injected as model_override.

Pins the contract from ``docs/prd/v1.18-fix-model-stored-read-side.md``:

1. **Read side**: ``bot._handle_thread_message`` and ``/session resume``
   both BUILD ``SessionConfig`` with ``model_override=None`` regardless
   of what ``stored.get("model")`` contains. Pre-#210, both paths read
   ``stored["model"]`` and reinjected it — re-forcing the "sonnet"
   pollution from pre-#199 builds and re-triggering the very bug #198
   was meant to fix.

2. **Write side**: ``SessionManager.save_session_state`` always writes
   ``model=None`` to the store (decision (b) per PRD §Design — keeps
   new rows clearly distinguishable from legacy "sonnet"-polluted ones
   during forensic inspection).

3. **/session info** uses ``_model_source_for_bridge`` from
   ``cogs.model`` for accurate 4-case display.

4. **Startup log**: ``SessionStore._load`` reports a count of legacy
   stored.model entries for operator visibility (does NOT mutate data).

5. **Integration**: a polluted stored row + no user ``/model switch``
   ends up with ``ClaudeAgentOptions(model=None)`` — same shape as
   ``test_options_does_not_use_sdk_model_as_input`` from #198.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from clauded.config import Config
from clauded.session_config import SessionConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(claude_model: str | None = None) -> Config:
    """Build a Config with the given claude_model env tier value."""
    return Config(
        discord_bot_token="tok",
        claude_model=claude_model,
        claude_permission_mode="default",
        projects_root="/tmp",
    )


class _FakeClient:
    """Stand-in for ``ClaudeSDKClient`` that records constructed options."""

    captured_options: list[Any] = []

    def __init__(self, options: Any = None) -> None:
        type(self).captured_options.append(options)

    async def connect(self, prompt: Any = None) -> None:
        pass

    async def disconnect(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_capture() -> None:
    _FakeClient.captured_options = []


def _bridge_for_case(
    *,
    override: str | None,
    env_model: str | None,
    sdk_model: str | None,
    active: bool = True,
    project_path: str = "/tmp/proj",
    num_turns: int = 1,
    total_cost: float = 0.0,
) -> Any:
    """Build a MagicMock bridge with the requested tier values."""
    bridge = MagicMock()
    bridge._model_override = override
    bridge._sdk_model = sdk_model
    cfg = MagicMock()
    cfg.claude_model = env_model
    bridge._config = cfg
    bridge.model = override or sdk_model or env_model
    bridge.is_active = active
    bridge.project_path = project_path
    bridge.num_turns = num_turns
    bridge.total_cost = total_cost
    return bridge


# ---------------------------------------------------------------------------
# S1 — bot._handle_thread_message: model_override forced to None
#       regardless of stored.get("model").
# ---------------------------------------------------------------------------


class _BotStub:
    """Bypasses full ClaudedBot __init__ for handler-isolation tests."""

    def __init__(self, *, stored: dict | None) -> None:
        self._user = MagicMock(id=42, name="ClaudeBot")
        self._user.name = "ClaudeBot"
        self.allow_unbound_fallback = False
        self.config = _config(claude_model=None)
        # Project manager — parent channel is bound, system prompt empty
        from pathlib import Path
        self.project_manager = MagicMock()
        self.project_manager.is_bound = MagicMock(return_value=True)
        self.project_manager.should_refuse_unbound = MagicMock(return_value=False)
        self.project_manager.get_path_or_default = MagicMock(
            return_value=(Path("/tmp"), True)
        )
        self.project_manager.get_system_prompt = MagicMock(return_value="")
        self.project_manager.get_extra_dirs = MagicMock(return_value=[])
        self.project_manager.get_mcp_servers = MagicMock(return_value=None)
        self.project_manager.get_env = MagicMock(return_value=None)
        # Session manager — bridge None so the create path runs
        self.session_manager = MagicMock()
        self.session_manager.get_session = MagicMock(return_value=None)
        self.session_manager.get_stored_session = MagicMock(return_value=stored)
        self.session_manager.create_session = AsyncMock()
        self.session_manager.get_lock = MagicMock(return_value=MagicMock(
            __aenter__=AsyncMock(), __aexit__=AsyncMock()
        ))
        self._logged_third_party_thread: set[int] = set()
        self._pre_tool_notifications: bool = False
        self._notify_enabled: dict[int, bool] = {}

    @property
    def user(self) -> Any:
        return self._user


def _make_thread_message(
    *, thread_id: int = 9999, parent_id: int = 1234, bot_id: int = 42
) -> Any:
    """Construct a bot-owned thread message (no @-mention required)."""
    msg = MagicMock()

    class _FakeThread(discord.Thread):
        def __init__(self) -> None:
            pass

    thread = _FakeThread()
    object.__setattr__(thread, "id", thread_id)
    object.__setattr__(thread, "owner_id", bot_id)  # bot-owned: skip mention gate
    object.__setattr__(thread, "parent_id", parent_id)
    msg.channel = thread
    msg.id = 8888
    msg.content = "hello"
    msg.author = MagicMock()
    msg.author.id = 1
    msg.author.__str__ = MagicMock(return_value="alice")
    msg.mentions = []
    msg.role_mentions = []
    msg.reply = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_thread_resume_ignores_polluted_stored_model() -> None:
    """#210 core: a polluted stored row with model='sonnet' must NOT
    feed model_override on auto-resume. The SessionConfig passed to
    create_session has model_override=None.
    """
    from clauded.bot import ClaudedBot

    polluted = {
        "session_id": "sess-old",
        "project_path": "/tmp",
        "model": "sonnet",  # legacy pollution
        "system_prompt": "",
        "last_active": "2025-01-01T00:00:00+00:00",
    }
    bot = _BotStub(stored=polluted)
    msg = _make_thread_message()

    # The handler will try to compose user text + render after creating
    # the session; we only care about the create_session call. Let it
    # raise downstream and swallow.
    bot._compose_user_text = AsyncMock(return_value=("hello", None))
    try:
        await ClaudedBot._handle_thread_message(bot, msg, parent_id=1234)
    except Exception:
        pass

    # The contract we're pinning: SessionConfig.model_override is None
    # regardless of the polluted stored.model value.
    assert bot.session_manager.create_session.await_count >= 1, (
        "create_session was never called — handler bailed before "
        "constructing SessionConfig"
    )
    call = bot.session_manager.create_session.await_args_list[0]
    sc = call.args[3]  # (thread_id, project_path, config, session_config)
    assert isinstance(sc, SessionConfig)
    assert sc.model_override is None, (
        f"#210: stored.model='sonnet' leaked into SessionConfig as "
        f"model_override={sc.model_override!r}. Pre-#210 regression."
    )
    # Sanity: resume_session_id is still threaded through (we still
    # resume; we just don't reinject the model).
    assert sc.resume_session_id == "sess-old"


@pytest.mark.asyncio
async def test_thread_resume_no_stored_session_also_yields_none() -> None:
    """Defensive: even with no stored row at all, model_override is
    None (i.e., the change doesn't accidentally regress the no-resume
    path)."""
    from clauded.bot import ClaudedBot

    bot = _BotStub(stored=None)
    msg = _make_thread_message()
    bot._compose_user_text = AsyncMock(return_value=("hello", None))
    try:
        await ClaudedBot._handle_thread_message(bot, msg, parent_id=1234)
    except Exception:
        pass

    call = bot.session_manager.create_session.await_args_list[0]
    sc = call.args[3]
    assert sc.model_override is None
    assert sc.resume_session_id is None


# ---------------------------------------------------------------------------
# S2 — /session resume: model_override forced to None.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_resume_ignores_polluted_stored_model() -> None:
    """/session resume must NOT reinject stored.model as model_override."""
    from clauded.cogs.session import session_resume
    from clauded.bot import ClaudedBot

    polluted = {
        "session_id": "sess-old",
        "project_path": "/tmp",
        "model": "sonnet",  # legacy pollution
        "system_prompt": "",
    }

    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.get_stored_session = MagicMock(return_value=polluted)
    bot.session_manager.stop_session = AsyncMock()
    bot.session_manager.create_session = AsyncMock()
    bot.session_manager.get_lock = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock()
    ))
    # #295: /session resume now reads project_path + system_prompt from
    # project_manager (not from stored). Wire up a project_manager mock
    # that returns a valid bound path.
    bot.project_manager = MagicMock()
    bot.project_manager.get_path = MagicMock(return_value="/tmp")
    bot.project_manager.get_system_prompt = MagicMock(return_value="")
    bot.config = _config(claude_model=None)

    interaction = MagicMock()
    interaction.client = bot
    interaction.channel_id = 9999
    interaction.channel = MagicMock()
    interaction.channel.parent_id = 9999
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    # Bypass the unbound-channel gate
    import clauded.cogs.session as session_mod
    session_mod.reject_if_unbound = AsyncMock(return_value=False)

    await session_resume.callback(interaction)

    assert bot.session_manager.create_session.await_count == 1
    call = bot.session_manager.create_session.await_args
    sc = call.args[3]
    assert isinstance(sc, SessionConfig)
    assert sc.model_override is None, (
        f"#210: /session resume leaked stored.model='sonnet' as "
        f"model_override={sc.model_override!r}"
    )
    assert sc.resume_session_id == "sess-old"


# ---------------------------------------------------------------------------
# S3 — save_session_state writes model=None.
# ---------------------------------------------------------------------------


def test_save_session_state_always_writes_none() -> None:
    """#210: save_session_state passes model=None to the store regardless
    of explicit_model_override (the field is deprecated/vestigial)."""
    from clauded.session_manager import SessionManager

    # Even when the user HAS set an explicit override, we don't persist
    # it — model_override is ephemeral per user decision.
    bridge = MagicMock()
    bridge.session_id = "sess-abc"
    bridge.project_path = "/tmp/proj"
    bridge.system_prompt = ""
    bridge.explicit_model_override = "haiku"  # user has /model switch'd
    bridge._sdk_model = "claude-sonnet-4-5"  # SDK reported sonnet

    sm = SessionManager(MagicMock())
    sm._sessions[42] = bridge

    captured: dict = {}

    def _fake_save(thread_id, session_id, *, permission_mode_override=None):
        captured.update(
            thread_id=thread_id, session_id=session_id,
            permission_mode_override=permission_mode_override,
        )

    sm._session_store.save_session = _fake_save  # type: ignore[method-assign]
    sm.save_session_state(42)

    # #295: model field is entirely gone from save_session's signature
    # — no stored model at all, which strengthens #210's contract
    # (ephemeral override, read side ignores stored.model).
    assert "model" not in captured, (
        f"#295 + #210: save_session_state must not thread model to save_session; "
        f"got {captured!r}. The persistence-field is deprecated; "
        f"read paths ignore it; new rows must be clean."
    )
    # Cross-check that session_id still threads through.
    assert captured["session_id"] == "sess-abc"


def test_save_session_state_writes_none_when_no_user_override() -> None:
    """No user /model switch → model=None (was already the case in #199,
    but pin it explicitly under #210 as well)."""
    from clauded.session_manager import SessionManager

    bridge = MagicMock()
    bridge.session_id = "sess-xyz"
    bridge.project_path = "/tmp/proj"
    bridge.system_prompt = ""
    bridge.explicit_model_override = None
    bridge._sdk_model = "claude-sonnet-4-5"

    sm = SessionManager(MagicMock())
    sm._sessions[99] = bridge

    captured: dict = {}

    def _fake_save(thread_id, session_id, *, permission_mode_override=None):
        captured["session_id"] = session_id

    sm._session_store.save_session = _fake_save  # type: ignore[method-assign]
    sm.save_session_state(99)
    # #295: model is no longer a save_session parameter at all.
    assert "model" not in captured


# ---------------------------------------------------------------------------
# S4 — /session info 4-case display matrix.
# ---------------------------------------------------------------------------


async def _invoke_session_info(bridge: Any) -> str:
    """Drive ``/session info`` with the given bridge; return the sent text."""
    from clauded.cogs.session import session_info
    from clauded.bot import ClaudedBot

    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=bridge)
    bot.config = _config(claude_model=None)

    interaction = MagicMock()
    interaction.client = bot
    interaction.channel_id = 1
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()

    await session_info.callback(interaction)

    # Body sent positionally; ephemeral kwarg set, but message text is args[0]
    call = interaction.response.send_message.await_args
    return call.args[0]


@pytest.mark.asyncio
async def test_session_info_case_override() -> None:
    """User /model switch X → shows X without env/SDK suffix."""
    bridge = _bridge_for_case(override="haiku", env_model=None, sdk_model=None)
    body = await _invoke_session_info(bridge)
    assert "haiku" in body
    assert "CLAUDE_MODEL env" not in body
    assert "CLI default" not in body
    assert "unknown" not in body.lower()


@pytest.mark.asyncio
async def test_session_info_case_env() -> None:
    """CLAUDE_MODEL=opus → ``opus (CLAUDE_MODEL env)``."""
    bridge = _bridge_for_case(override=None, env_model="opus", sdk_model=None)
    body = await _invoke_session_info(bridge)
    assert "opus" in body
    assert "CLAUDE_MODEL env" in body


@pytest.mark.asyncio
async def test_session_info_case_sdk_observed() -> None:
    """No override, no env, _sdk_model set → ``<value> (CLI default)``.

    Label parity with ``/model current`` per #213 R1 syntax review:
    both surfaces render the same SDK-observed value as ``CLI default``
    (since the value flows from ~/.claude/settings.json via the SDK).
    """
    bridge = _bridge_for_case(
        override=None, env_model=None, sdk_model="claude-sonnet-4-5"
    )
    body = await _invoke_session_info(bridge)
    assert "claude-sonnet-4-5" in body
    assert "CLI default" in body


@pytest.mark.asyncio
async def test_session_info_case_unset_placeholder() -> None:
    """Bridge active + nothing set + pre-first-turn → placeholder."""
    bridge = _bridge_for_case(override=None, env_model=None, sdk_model=None)
    body = await _invoke_session_info(bridge)
    assert "unknown" in body.lower()
    assert "send a message" in body.lower()


@pytest.mark.asyncio
async def test_session_info_no_bridge() -> None:
    """No bridge at all → "No active Claude session" reply (unchanged)."""
    from clauded.cogs.session import session_info
    from clauded.bot import ClaudedBot

    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=None)

    interaction = MagicMock()
    interaction.client = bot
    interaction.channel_id = 1
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()

    await session_info.callback(interaction)
    body = interaction.response.send_message.await_args.args[0]
    assert "No active" in body


# ---------------------------------------------------------------------------
# S5 — Startup legacy-field migration (updated for #295).
#
# #210's earlier forensic-preserving behavior (count + INFO log, no mutation)
# was superseded by #295: the load path now strips ``model`` /
# ``system_prompt`` / ``project_path`` from every entry so operators don't
# carry stale shadow data forward. These three tests are the #295 version
# of the previous #210 log tests.
# ---------------------------------------------------------------------------


def test_session_store_strips_legacy_shadow_fields(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#295: _load strips model/system_prompt/project_path and rewrites the file."""
    from clauded.session_store import SessionStore

    payload = {
        "1": {"session_id": "a", "project_path": "/p", "model": "sonnet",
              "system_prompt": "sp", "last_active": "x"},
        "2": {"session_id": "b", "project_path": "/p", "model": "opus",
              "system_prompt": "", "last_active": "x"},
        "3": {"session_id": "c", "project_path": "/p", "model": None,
              "system_prompt": "", "last_active": "x"},
    }
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sessions_path = data_dir / "sessions.json"
    sessions_path.write_text(json.dumps(payload))

    with caplog.at_level(logging.INFO, logger="clauded.session_store"):
        store = SessionStore(data_dir=str(data_dir))

    # #295 log emitted, no more #210 forensic log.
    matches = [r for r in caplog.records if "#295" in r.getMessage()]
    assert matches, "expected one #295 shadow-field strip INFO log"
    assert not [r for r in caplog.records if "#210" in r.getMessage()]

    # File has been rewritten with shadow fields removed.
    after = json.loads(sessions_path.read_text())
    for entry in after.values():
        assert "model" not in entry
        assert "system_prompt" not in entry
        assert "project_path" not in entry
    # session_id / last_active preserved verbatim.
    assert after["1"]["session_id"] == "a"
    assert after["1"]["last_active"] == "x"
    # And the in-memory copy matches the on-disk copy.
    assert store.get_session_info(1) == after["1"]


def test_session_store_no_legacy_no_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """All rows already clean → no #295 strip log emitted, file untouched."""
    from clauded.session_store import SessionStore

    payload = {
        "1": {"session_id": "a", "last_active": "x"},
    }
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sessions_path = data_dir / "sessions.json"
    sessions_path.write_text(json.dumps(payload))
    before = sessions_path.read_text()

    with caplog.at_level(logging.INFO, logger="clauded.session_store"):
        SessionStore(data_dir=str(data_dir))

    assert not [r for r in caplog.records if "#295" in r.getMessage()]
    assert not [r for r in caplog.records if "#210" in r.getMessage()]
    # No mutation when nothing to strip.
    assert sessions_path.read_text() == before


def test_session_store_missing_file_no_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No sessions.json at all → no strip log, no crash."""
    from clauded.session_store import SessionStore

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with caplog.at_level(logging.INFO, logger="clauded.session_store"):
        SessionStore(data_dir=str(data_dir))
    assert not [r for r in caplog.records if "#295" in r.getMessage()]
    assert not [r for r in caplog.records if "#210" in r.getMessage()]


# ---------------------------------------------------------------------------
# S6 — Integration: polluted stored row + no user switch =>
#       ClaudeAgentOptions(model=None) (same shape as #198's
#       test_options_does_not_use_sdk_model_as_input).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_polluted_stored_yields_options_model_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: polluted stored row + no user override →
    SessionConfig(model_override=None) →
    ClaudeBridge.start() constructs ClaudeAgentOptions(model=None).

    This mirrors ``test_options_does_not_use_sdk_model_as_input`` from
    #198 but ties it to the read-side path that #210 fixed.
    """
    from clauded.claude_bridge import ClaudeBridge

    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _FakeClient)

    # Construct the SessionConfig as the post-#210 read path would
    # (stored.model="sonnet" is ignored).
    sc = SessionConfig(
        system_prompt="",
        model_override=None,  # #210: ephemeral, NOT stored["model"]
        resume_session_id="sess-old",
    )
    bridge = ClaudeBridge(
        project_path="/tmp/p", config=_config(claude_model=None),
        session_config=sc,
    )
    await bridge.start()

    assert _FakeClient.captured_options, "ClaudeAgentOptions was not built"
    opts = _FakeClient.captured_options[-1]
    assert opts.model is None, (
        f"#210 integration: SDK options.model leaked to {opts.model!r}. "
        f"Polluted stored.model='sonnet' must NOT reach the SDK."
    )


# ---------------------------------------------------------------------------
# Regression: #199 R1 architect contract still holds.
# ---------------------------------------------------------------------------


def test_regression_199_r1_no_sdk_loop_still_holds() -> None:
    """Re-pin the #199 R1 architect contract under the #210 update:
    save_session_state must NOT persist _sdk_model. With #210 going
    further (always None), this is now strictly stronger — we never
    persist anything for the model field — but the original assertion
    (no _sdk_model leak) must still hold.
    """
    from clauded.session_manager import SessionManager

    bridge = MagicMock()
    bridge.session_id = "sess-abc"
    bridge.project_path = "/tmp/proj"
    bridge.system_prompt = ""
    bridge._model_override = None
    bridge._sdk_model = "claude-haiku-4-5"
    bridge.explicit_model_override = None

    sm = SessionManager(MagicMock())
    sm._sessions[42] = bridge

    captured: dict = {}

    def _fake_save(thread_id, session_id, *, permission_mode_override=None):
        captured["session_id"] = session_id
        captured["permission_mode_override"] = permission_mode_override

    sm._session_store.save_session = _fake_save  # type: ignore[method-assign]
    sm.save_session_state(42)

    # #295: model is no longer a save_session parameter at all — the
    # #199 "model != _sdk_model" and #210 "model is None" invariants
    # are subsumed: the field simply doesn't exist in the payload,
    # which is the strongest possible form of both guarantees.
    assert "model" not in captured


@pytest.mark.asyncio
async def test_session_info_strips_backticks_in_sdk_model_attacker_string() -> None:
    """R1 security hardening: ``_sdk_model`` flows from
    ``ResultMessage.model`` which is attacker-influenceable. A malicious
    proxy returning a model name containing backticks could break the
    inline code fence in the embed and let downstream markdown leak.
    Defense-in-depth: strip backticks + cap length before embedding.
    """
    # Synthetic attacker model name: contains backticks + is long
    malicious = "evil`escape`-" + ("x" * 500)
    bridge = _bridge_for_case(
        override=None, env_model=None, sdk_model=malicious
    )
    body = await _invoke_session_info(bridge)
    # 1) Backticks inside the value MUST be stripped (replaced by ')
    # The body still contains the OUTER pair from the f"`{safe_value}`"
    # template, but the inner attacker-controlled backticks are gone.
    # Extract just the value between the first ` and the matching `:
    import re
    match = re.search(r"Model:\s*`([^`]*)`", body)
    assert match is not None, f"Expected `Model: \\`...\\`` format; got: {body!r}"
    rendered_value = match.group(1)
    assert "`" not in rendered_value, (
        f"Backticks leaked through inline code fence; got: {rendered_value!r}"
    )
    # 2) Length capped (we use [:120])
    assert len(rendered_value) <= 120
    # 3) The (CLI default) suffix still renders after the cleaned value
    assert "(CLI default)" in body


@pytest.mark.asyncio
async def test_model_current_strips_backticks_in_sdk_model_attacker_string() -> None:
    """Same security hardening on ``/model current``'s sdk-observed branch.
    Both surfaces share the parity contract from R1 syntax + same
    attacker-influenceable input."""
    from clauded.cogs.model import model_current
    from clauded.bot import ClaudedBot

    malicious = "evil`escape`-" + ("x" * 500)
    bot_spec = MagicMock(spec=ClaudedBot)
    bot_spec.session_manager = MagicMock()
    bridge = MagicMock()
    bridge._model_override = None
    bridge._sdk_model = malicious
    bridge._config = MagicMock(claude_model=None)
    bot_spec.session_manager.get_session = MagicMock(return_value=bridge)
    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel.id = 1
    interaction.response.send_message = AsyncMock()
    await model_current.callback(interaction)
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    desc = embed.description
    # Extract value between the inline-code-fence backticks
    import re
    match = re.search(r"`([^`]*)`", desc)
    assert match is not None
    rendered_value = match.group(1)
    assert "`" not in rendered_value
    assert len(rendered_value) <= 120
    assert "(CLI default)" in desc
