"""#192 — sub-agent Subtask Complete embed leaked Python repr + CLI meta.

Fixes:
- A: _extract_block_content_text normalizes list[dict] → plain text
- B: _strip_internal_metadata removes "(internal ID - do not mention to user...)" patterns
- C: _is_async_agent_dispatch + new "🚀 Sub-agent dispatched" embed title
"""
import pytest

# v1.18 stage-28: subclass the shared fakes (cheap base inheritance keeps
# the dedup win while still letting us mutate _FakeMessage.create_thread
# at the class level without touching tests/conftest.py for other files).
# _FakeThread stays as a separate inline class — one-off shape (random id
# + tracked _sent) used only by this file's create_thread monkey-patching.
from tests.conftest import FakeBridge, FakeMessage, FakeTarget


class _FakeMessage(FakeMessage):
    """Test-local subclass so monkey-patching create_thread on _FakeMessage
    (the prod-failure regression path) doesn't bleed into other tests
    that import the shared FakeMessage. Default create_thread returns a
    real _FakeThread (the original behavior before stage-28 trim)."""
    async def create_thread(self, name, auto_archive_duration=None, **kwargs):
        return _FakeThread(name=name)


class _FakeTarget(FakeTarget):
    """Override message factory to mint _FakeMessage (the create-thread-
    aware subclass). Uses the conftest._make_message hook — no need to
    duplicate the entire send() body."""
    def _make_message(self, msg_id):
        return _FakeMessage(msg_id=msg_id)


# ---------------------------------------------------------------------------
# Fix A: _extract_block_content_text — content shape normalization
# ---------------------------------------------------------------------------


def test_extract_str_returns_str_as_is():
    from clauded.discord_renderer import _extract_block_content_text
    assert _extract_block_content_text("plain string") == "plain string"


def test_extract_none_returns_empty_string():
    from clauded.discord_renderer import _extract_block_content_text
    assert _extract_block_content_text(None) == ""


def test_extract_list_of_text_dicts():
    """The prod failure mode: ``[{'type': 'text', 'text': '...'}]`` was
    leaking as Python repr before #192. Must extract clean text."""
    from clauded.discord_renderer import _extract_block_content_text
    out = _extract_block_content_text([
        {"type": "text", "text": "first line"},
        {"type": "text", "text": "second line"},
    ])
    assert out == "first line\nsecond line"
    assert "[" not in out
    assert "{" not in out
    assert "'type'" not in out


def test_extract_list_of_strings():
    from clauded.discord_renderer import _extract_block_content_text
    assert _extract_block_content_text(["a", "b", "c"]) == "a\nb\nc"


def test_extract_list_mixed_dict_without_text_key():
    from clauded.discord_renderer import _extract_block_content_text
    out = _extract_block_content_text([{"foo": "bar"}])
    assert isinstance(out, str)
    assert out != ""


def test_extract_dict_with_text_key():
    from clauded.discord_renderer import _extract_block_content_text
    assert _extract_block_content_text({"text": "single dict"}) == "single dict"


def test_extract_unknown_shape_str_fallback():
    from clauded.discord_renderer import _extract_block_content_text
    assert _extract_block_content_text(42) == "42"


def test_extract_does_not_raise_on_pathological_input():
    from clauded.discord_renderer import _extract_block_content_text
    pathological = [
        object(), {}, [], "", 0, 0.5, True, False,
        [None, None], [{"text": None}],
    ]
    for p in pathological:
        try:
            out = _extract_block_content_text(p)
            assert isinstance(out, str)
        except Exception as e:
            pytest.fail(f"Unexpected exception for {p!r}: {e!r}")


# ---------------------------------------------------------------------------
# Fix B: _strip_internal_metadata
# ---------------------------------------------------------------------------


def test_strip_internal_id_paren_pattern():
    """The exact prod-screenshot pattern must be removed."""
    from clauded.discord_renderer import _strip_internal_metadata
    text = (
        "Async agent launched successfully.\n"
        "agentId: aae5405f50ce90137 (internal ID - do not mention to user. "
        "Use SendMessage with to: 'aae5405f50ce90137' to continue this agent.)\n"
        "The agent is working in the background."
    )
    cleaned = _strip_internal_metadata(text)
    assert "internal ID" not in cleaned
    assert "do not mention" not in cleaned
    assert "SendMessage" not in cleaned
    assert "Async agent launched successfully" in cleaned
    assert "background" in cleaned
    assert "aae5405f50ce90137" in cleaned


def test_strip_standalone_do_not_mention_sentence():
    from clauded.discord_renderer import _strip_internal_metadata
    text = "Result: success. Do not mention this internal detail to user. Goodbye."
    cleaned = _strip_internal_metadata(text)
    assert "Do not mention" not in cleaned
    assert "Result: success." in cleaned
    assert "Goodbye." in cleaned


def test_strip_does_not_over_strip_legitimate_content():
    """Negative test: user content that incidentally contains the word
    'mention' or 'internal' must NOT be stripped."""
    from clauded.discord_renderer import _strip_internal_metadata
    text = (
        "Documentation: this internal API is now public. "
        "Please mention it in the changelog."
    )
    cleaned = _strip_internal_metadata(text)
    assert "internal API" in cleaned
    assert "mention it in the changelog" in cleaned


def test_strip_idempotent_on_clean_text():
    from clauded.discord_renderer import _strip_internal_metadata
    clean = "Hello, World! This is fine."
    assert _strip_internal_metadata(clean) == clean


def test_strip_handles_empty_string():
    from clauded.discord_renderer import _strip_internal_metadata
    assert _strip_internal_metadata("") == ""


# ---------------------------------------------------------------------------
# Fix C: _is_async_agent_dispatch
# ---------------------------------------------------------------------------


def test_dispatch_detection_matches_canonical_phrase():
    from clauded.discord_renderer import _is_async_agent_dispatch
    assert _is_async_agent_dispatch("Async agent launched successfully.")
    assert _is_async_agent_dispatch("async agent launched successfully")


def test_dispatch_detection_rejects_unrelated_text():
    from clauded.discord_renderer import _is_async_agent_dispatch
    assert not _is_async_agent_dispatch("Subtask done")
    assert not _is_async_agent_dispatch("")
    assert not _is_async_agent_dispatch("agent task running")


# ---------------------------------------------------------------------------
# Integration: end-to-end render with real SDK shape
# ---------------------------------------------------------------------------


class _FakeThread:
    """Minimal thread stub for the Task tool path.

    Kept inline (not in conftest) because it's only consumed here as the
    return value of monkey-patched ``_FakeMessage.create_thread``. The
    random-id + per-thread ``_sent`` shape is not used by any other test
    file.
    """
    def __init__(self, name):
        import random
        self.id = random.randint(10_000, 99_999)
        self.name = name
        self.mention = f"<#{self.id}>"
        self.parent = None
        self._sent = []
    async def send(self, *args, **kwargs):
        msg = _FakeMessage(msg_id=self.id * 100 + len(self._sent))
        if "content" in kwargs:
            msg.content = kwargs["content"]
        if "embed" in kwargs:
            msg.embeds = [kwargs["embed"]]
        self._sent.append(msg)
        return msg


@pytest.mark.asyncio
async def test_subtask_complete_renders_clean_text_from_list_of_dicts():
    """E2E: ToolResultBlock with list[dict] content (the prod-failure
    shape) must produce a clean text description, not a Python repr."""
    import sys
    sys.path.insert(0, "src")
    from claude_agent_sdk.types import (
        AssistantMessage, ToolUseBlock, ToolResultBlock, ResultMessage,
    )
    from clauded.discord_renderer import DiscordRenderer

    leaky_text = (
        "Async agent launched successfully.\n"
        "agentId: aae5405f50ce90137 (internal ID - do not mention to user. "
        "Use SendMessage with to: 'aae5405f50ce90137' to continue this agent.)\n"
        "The agent is working in the background."
    )
    events = [
        AssistantMessage(
            content=[ToolUseBlock(id="task-1", name="Task", input={"prompt": "do stuff", "description": "test task"})],
            model="claude-sonnet-4-5", parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="task-1",
                    content=[{"type": "text", "text": leaky_text}],
                    is_error=False,
                )
            ],
            model="claude-sonnet-4-5", parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=100, duration_api_ms=80,
            is_error=False, num_turns=1, session_id="sess-subtask",
            total_cost_usd=0.001,
        ),
    ]

    # The first render builds embeds we never inspect; we re-run below
    # with a monkey-patched create_thread so we can capture the spawned
    # sub-threads (renderer doesn't expose subagent_renderers). Skip the
    # first pass entirely (R1 simplicity flagged it as dead async work).
    threads_created: list[_FakeThread] = []
    orig_create = _FakeMessage.create_thread
    async def _record_create(self, name, **kw):
        t = _FakeThread(name=name)
        threads_created.append(t)
        return t

    _FakeMessage.create_thread = _record_create  # type: ignore[method-assign]
    try:
        target2 = _FakeTarget()
        renderer2 = DiscordRenderer(target2)
        await renderer2.render_response(FakeBridge(events), "run task")
    finally:
        _FakeMessage.create_thread = orig_create  # type: ignore[method-assign]

    # Now scan target2._sent + threads_created._sent
    all_msgs = list(target2._sent)
    for t in threads_created:
        all_msgs.extend(t._sent)
    embeds = []
    for m in all_msgs:
        for e in m.embeds:
            embeds.append(e)
    subtask_embeds = [
        e for e in embeds
        if (e.title or "").startswith(("✅ Subtask", "❌ Subtask", "🚀 Sub-agent"))
    ]
    assert subtask_embeds, (
        f"Expected a Subtask embed; got titles: "
        f"{[(e.title or '') for e in embeds]}"
    )
    # Find the dispatch-marked embed
    dispatch_embeds = [e for e in subtask_embeds if "Sub-agent dispatched" in (e.title or "") and not (e.description or "").startswith("📎 ")]
    assert dispatch_embeds, (
        f"Async-launch must be marked 'Sub-agent dispatched'; "
        f"got: {[(e.title or '') for e in subtask_embeds]}"
    )
    embed = dispatch_embeds[0]
    desc = embed.description or ""
    # Fix A: no Python repr leakage
    assert "[{'type':" not in desc, f"Python repr leaked: {desc!r}"
    assert "'text':" not in desc, f"dict key visible: {desc!r}"
    # Fix B: meta-instruction stripped
    assert "do not mention" not in desc.lower(), f"meta instruction leaked: {desc!r}"
    assert "internal ID" not in desc, f"meta instruction leaked: {desc!r}"
    # Legitimate content preserved
    assert "Async agent launched" in desc or "background" in desc, (
        f"Legitimate content stripped: {desc!r}"
    )


@pytest.mark.asyncio
async def test_subtask_complete_normal_path_still_works():
    """Backward compat: when content is a plain string (most tools), the
    existing Subtask Complete rendering still produces correct output."""
    import sys
    sys.path.insert(0, "src")
    from claude_agent_sdk.types import (
        AssistantMessage, ToolUseBlock, ToolResultBlock, ResultMessage,
    )
    from clauded.discord_renderer import DiscordRenderer

    plain_text = "Task finished. Result: 42 items processed successfully."
    events = [
        AssistantMessage(
            content=[ToolUseBlock(id="task-2", name="Task", input={"prompt": "count", "description": "count items"})],
            model="claude-sonnet-4-5", parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="task-2", content=plain_text, is_error=False)
            ],
            model="claude-sonnet-4-5", parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=100, duration_api_ms=80,
            is_error=False, num_turns=1, session_id="sess-norm",
            total_cost_usd=0.001,
        ),
    ]

    threads_created: list[_FakeThread] = []
    orig = _FakeMessage.create_thread
    async def _record(self, name, **kw):
        t = _FakeThread(name=name)
        threads_created.append(t)
        return t
    _FakeMessage.create_thread = _record  # type: ignore[method-assign]
    try:
        target = _FakeTarget()
        renderer = DiscordRenderer(target)
        await renderer.render_response(FakeBridge(events), "run task")
    finally:
        _FakeMessage.create_thread = orig  # type: ignore[method-assign]

    all_msgs = list(target._sent)
    for t in threads_created:
        all_msgs.extend(t._sent)
    embeds = [e for m in all_msgs for e in m.embeds]
    subtask_embeds = [
        e for e in embeds
        if (e.title or "").startswith(("✅ Subtask", "❌ Subtask", "🚀 Sub-agent"))
    ]
    assert subtask_embeds
    # NOT a dispatch (no "Async agent launched" in plain text)
    dispatch_marks = [e for e in subtask_embeds if "Sub-agent dispatched" in (e.title or "")]
    assert not dispatch_marks, (
        f"Plain text MUST NOT be marked as 'Sub-agent dispatched'; "
        f"got: {[(e.title or '') for e in subtask_embeds]}"
    )
    # Standard "✅ Subtask Complete"
    complete_marks = [e for e in subtask_embeds if "✅ Subtask Complete" in (e.title or "") and not (e.description or "").startswith("📎 ")]
    assert complete_marks, f"Expected '✅ Subtask Complete'; got: {[(e.title or '') for e in subtask_embeds]}"
    embed = complete_marks[0]
    # Plain text preserved
    assert "42 items processed" in (embed.description or "")


@pytest.mark.asyncio
async def test_subtask_failed_with_list_of_dicts_renders_clean_error():
    """R1 tester gap: error-path (is_error=True) + list-of-dict content
    must also extract clean text, not leak Python repr. The embed title
    flips to '❌ Subtask Failed' (not Sub-agent dispatched, since we
    only dispatch-detect on the non-error path)."""
    import sys
    sys.path.insert(0, "src")
    from claude_agent_sdk.types import (
        AssistantMessage, ToolUseBlock, ToolResultBlock, ResultMessage,
    )
    from clauded.discord_renderer import DiscordRenderer

    err_text = "Task crashed: ValueError(\"missing required arg 'name'\")"
    events = [
        AssistantMessage(
            content=[ToolUseBlock(id="task-err", name="Task", input={"prompt": "bad", "description": "buggy"})],
            model="claude-sonnet-4-5", parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="task-err",
                    content=[{"type": "text", "text": err_text}],
                    is_error=True,
                )
            ],
            model="claude-sonnet-4-5", parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=100, duration_api_ms=80,
            is_error=True, num_turns=1, session_id="sess-err",
            total_cost_usd=0.001,
        ),
    ]

    threads_created: list[_FakeThread] = []
    orig = _FakeMessage.create_thread
    async def _record(self, name, **kw):
        t = _FakeThread(name=name)
        threads_created.append(t)
        return t
    _FakeMessage.create_thread = _record  # type: ignore[method-assign]
    try:
        target = _FakeTarget()
        renderer = DiscordRenderer(target)
        await renderer.render_response(FakeBridge(events), "run task")
    finally:
        _FakeMessage.create_thread = orig  # type: ignore[method-assign]

    all_msgs = list(target._sent)
    for t in threads_created:
        all_msgs.extend(t._sent)
    embeds = [e for m in all_msgs for e in m.embeds]
    failed_embeds = [
        e for e in embeds
        if (e.title or "").startswith("❌ Subtask Failed")
        and not (e.description or "").startswith("📎 ")
    ]
    assert failed_embeds, (
        f"Expected '❌ Subtask Failed' embed; got titles: "
        f"{[(e.title or '') for e in embeds]}"
    )
    embed = failed_embeds[0]
    desc = embed.description or ""
    # No Python repr leak
    assert "[{'type':" not in desc
    assert "'text':" not in desc
    # The actual error text comes through
    assert "ValueError" in desc or "missing required arg" in desc


def test_dispatch_detection_word_boundary():
    """R1 engineer: word-boundary anchor prevents false positives where
    'async agent' appears as a substring in unrelated context."""
    from clauded.discord_renderer import _is_async_agent_dispatch
    # Positive: canonical phrase
    assert _is_async_agent_dispatch("Async agent launched successfully.")
    assert _is_async_agent_dispatch("ASYNC AGENT LAUNCHED")
    # Negative: substring without word boundary
    assert not _is_async_agent_dispatch("synchronizedasync agentlauncher started")
    # Negative: missing 'launched' verb
    assert not _is_async_agent_dispatch("async agent is running")
