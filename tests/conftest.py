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
        self._sent.append(msg)
        return msg
