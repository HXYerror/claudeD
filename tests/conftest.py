"""Shared pytest fixtures + minimal fakes for renderer tests.

v1.18 stage-28: centralize the ``FakeBridge`` / ``FakeMessage`` / ``FakeTarget``
trio that was being copy-pasted across 4 test files (5 copies in
``test_tool_result_shorttier.py`` alone). Per-test customization stays as
inline subclasses near the test that uses it.

NOT covered by these helpers (different shapes, out of scope):
- ``tests/test_subagent_threads.py`` has its own ``FakeMainThread`` /
  ``FakeChannel`` with parent-channel routing + thread.create_thread
  invocation tracking.
- ``tests/test_session_manager.py`` uses manager-level fixtures.

Per-test customization stays as inline subclass of the base classes here,
e.g. ``FailingSendTarget(FakeTarget)`` or ``FailingAddViewBot``.
"""
from __future__ import annotations

from unittest.mock import MagicMock


class FakeBridge:
    """Minimal ClaudeBridge stand-in for renderer tests.

    ``events`` is the iterable yielded by ``send_message``; tests pass any list
    of SDK event objects (AssistantMessage, UserMessage, ResultMessage, etc).

    Keyword args:
      get_context_usage_returns -- value to return from ``get_context_usage()``
                                   (default ``None``)
      get_context_usage_raises  -- exception to raise instead of returning
    """

    def __init__(
        self,
        events,
        *,
        get_context_usage_returns=None,
        get_context_usage_raises=None,
    ):
        self._events = events
        self._gcu_returns = get_context_usage_returns
        self._gcu_raises = get_context_usage_raises
        self.is_active = True
        self._client = MagicMock()

    async def send_message(self, _text):
        for ev in self._events:
            yield ev

    async def get_context_usage(self):
        if self._gcu_raises is not None:
            raise self._gcu_raises
        return self._gcu_returns


class FakeMessage:
    """Minimal ``discord.Message`` stand-in.

    Captures ``content`` / ``embeds`` / ``view`` / ``attachments`` on
    ``edit`` and ``send``. ``create_thread`` is a no-op by default; tests
    that need a real thread override at the class level (see
    ``tests/test_subtask_complete_render.py``).
    """

    def __init__(self, msg_id: int = 1):
        self.id = msg_id
        self.content = ""
        self.embeds: list = []
        self.attached_view = None
        self.attachments: list = []

    async def edit(self, **kwargs):
        if "content" in kwargs:
            self.content = kwargs["content"]
        if "embed" in kwargs:
            self.embeds = [kwargs["embed"]]
        if "view" in kwargs:
            self.attached_view = kwargs["view"]
        return self

    async def delete(self):
        return None

    async def create_thread(self, name, auto_archive_duration=None, **kwargs):
        # Default no-op; tests override at class level if they need a real thread
        return None


class FakeTarget:
    """Minimal channel/thread stand-in. Records every ``send()`` into ``_sent``."""

    def __init__(self, target_id: int = 1):
        self.id = target_id
        self.name = "fake-target"
        self.mention = f"<#{target_id}>"
        self.parent = None
        self._sent: list[FakeMessage] = []
        self._next_id = target_id * 1000

    def _make_message(self, msg_id: int) -> FakeMessage:
        """Hook subclasses can override to mint a custom message subclass.

        Default returns the shared ``FakeMessage``. Tests that need
        per-message-class behavior (e.g.,
        ``test_subtask_complete_render._FakeMessage`` overriding
        ``create_thread``) override this method instead of duplicating
        the entire ``send()`` body.
        """
        return FakeMessage(msg_id=msg_id)

    async def send(self, *args, **kwargs):
        self._next_id += 1
        msg = self._make_message(self._next_id)
        if "content" in kwargs:
            msg.content = kwargs["content"]
        if "embed" in kwargs:
            msg.embeds = [kwargs["embed"]]
        if "view" in kwargs:
            msg.attached_view = kwargs["view"]
        if "file" in kwargs:
            msg.attachments = [kwargs["file"]]
        if "files" in kwargs:
            # discord.py's Channel.send supports both `file=` (one) and
            # `files=[...]` (list). Test fixture captures both shapes so
            # tests asserting attachment count don't false-negative when
            # production switches between APIs (e.g., #205 table PNGs use
            # files=[png, md] sidecar pattern).
            msg.attachments = list(kwargs["files"])
        self._sent.append(msg)
        return msg


# ---------------------------------------------------------------------------
# #220 — speed up ALL tests that hit `_fetch_context_pct_settled` (footer).
# Without this autouse fixture, every render_response integration test pays
# the helper's 0.5s+0.5s sleep on each turn — adds ~1s per test, ~25s suite-wide.
# Tests can override by passing explicit kwargs in their own monkey-patching.
# ---------------------------------------------------------------------------


import pytest


@pytest.fixture(autouse=True)
def _zero_context_settle_delay(monkeypatch):
    """Auto-zero the #220 footer settle delay across all tests.

    The helper's default (0.5s) is correct for production but makes the
    test suite slow. Tests that specifically exercise the timing behavior
    can re-patch via direct ``monkeypatch`` or by calling the helper
    directly (see `tests/test_footer_context_race.py`).

    R1 simplicity fix: this fixture HARD-OVERWRITES the delay to 0.0;
    it does NOT accept a caller-supplied ``settle_delay`` and silently
    discard it (the previous wrapper shape misled future debuggers).
    Tests that need a non-zero delay must `monkeypatch.setattr` again.
    """
    try:
        import clauded.discord_renderer as _renderer_mod
    except ImportError:  # safety: tests that don't load the renderer don't need this
        return
    if not hasattr(_renderer_mod, "_fetch_context_pct_settled"):
        return
    real_helper = _renderer_mod._fetch_context_pct_settled

    async def _zero_delay_helper(bridge, *, log_label="footer", **_ignored):
        # _ignored swallows any settle_delay kwarg callers may pass.
        # We deliberately force settle_delay=0.0 — see docstring.
        return await real_helper(
            bridge, settle_delay=0.0, log_label=log_label
        )

    monkeypatch.setattr(_renderer_mod, "_fetch_context_pct_settled", _zero_delay_helper)
