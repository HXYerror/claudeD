"""#185 — slash commands were registered to BOTH global and per-guild
scopes, causing every command to appear twice in Discord autocomplete.

Fix: only sync per-guild from on_ready (with idempotency guard).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


class _BotStub:
    """Bare stand-in: bypass ClaudedBot's full __init__ (needs discord
    Client wiring, intents, etc.) and only assert on the sync surface."""
    def __init__(self, guilds=()):
        self._tree = MagicMock()
        self._tree.sync = AsyncMock()
        self._tree.copy_global_to = MagicMock()
        self.guilds = list(guilds)
        self._slash_synced: set[int] = set()
        self.user = MagicMock(id=999, __str__=lambda self: "ClaudeBot#1130")

    @property
    def tree(self):
        return self._tree


@pytest.mark.asyncio
async def test_on_ready_per_guild_sync_with_idempotency():
    """on_ready syncs per-guild, marking each guild in ``_slash_synced``
    so a reconnect re-fire doesn't re-sync."""
    from clauded.bot import ClaudedBot
    g1 = MagicMock(id=111, name="Guild1")
    g2 = MagicMock(id=222, name="Guild2")
    stub = _BotStub(guilds=[g1, g2])
    stub._tree.sync.return_value = [MagicMock(), MagicMock()]

    # First on_ready
    await ClaudedBot.on_ready(stub)
    assert stub._slash_synced == {111, 222}
    assert stub._tree.sync.await_count == 2
    assert stub._tree.copy_global_to.call_count == 2

    # Second on_ready (reconnect simulation): no new syncs
    stub._tree.sync.reset_mock()
    stub._tree.copy_global_to.reset_mock()
    await ClaudedBot.on_ready(stub)
    assert stub._tree.sync.await_count == 0
    assert stub._tree.copy_global_to.call_count == 0


@pytest.mark.asyncio
async def test_on_ready_new_guild_after_reconnect_gets_synced():
    """If a guild was added between on_readys, only the new one syncs."""
    from clauded.bot import ClaudedBot
    g1 = MagicMock(id=111, name="Old")
    g2 = MagicMock(id=222, name="New")
    stub = _BotStub(guilds=[g1, g2])
    stub._slash_synced = {111}
    stub._tree.sync.return_value = [MagicMock()]

    await ClaudedBot.on_ready(stub)
    assert stub._tree.sync.await_count == 1
    sync_call = stub._tree.sync.await_args_list[0]
    assert sync_call.kwargs.get("guild") is g2
    assert stub._slash_synced == {111, 222}


@pytest.mark.asyncio
async def test_on_ready_sync_failure_logged_but_does_not_block_others():
    """One guild fails → others still sync."""
    from clauded.bot import ClaudedBot
    g1 = MagicMock(id=111, name="Fail")
    g2 = MagicMock(id=222, name="OK")
    stub = _BotStub(guilds=[g1, g2])
    stub._tree.sync = AsyncMock(side_effect=[Exception("synthetic"), [MagicMock()]])

    await ClaudedBot.on_ready(stub)
    assert 222 in stub._slash_synced
    assert 111 not in stub._slash_synced


@pytest.mark.asyncio
async def test_on_ready_with_no_guilds_does_nothing():
    """No guilds (rare boot state): no syncs, no crash."""
    from clauded.bot import ClaudedBot
    stub = _BotStub(guilds=[])
    await ClaudedBot.on_ready(stub)
    assert stub._tree.sync.await_count == 0
    assert stub._slash_synced == set()


def test_setup_hook_source_has_no_global_tree_sync():
    """R1 tester regression pin: source-level assertion that setup_hook
    never calls ``tree.sync()`` (i.e., without a guild kwarg). This was
    THE bug — global sync registered all 26 commands in both scopes,
    causing the autocomplete to show each twice.

    Source-level test instead of behavioral mock because:
    1. ``self.tree`` is a property with no setter on commands.Bot,
       making a behavioral mock awkward (we'd have to patch the
       property descriptor)
    2. The bug class is "someone re-adds tree.sync() somewhere in
       setup_hook" — a textual guard catches that even if the call
       site moves
    """
    import inspect
    from clauded.bot import ClaudedBot
    src = inspect.getsource(ClaudedBot.setup_hook)
    # Allow tree.sync(guild=...) but forbid bare tree.sync() / tree.sync(
    # without a guild= keyword. Approximate via substring match (this is
    # a regression test, not a parser):
    for forbidden in ("await self.tree.sync()", "self.tree.sync()"):
        assert forbidden not in src, (
            f"setup_hook MUST NOT call {forbidden} — it registers commands "
            f"GLOBALLY, which is what caused #185 (every command appeared "
            f"twice in autocomplete). Per-guild sync from on_ready instead."
        )


import inspect

def test_setup_hook_does_not_call_global_tree_sync():
    """#185 regression pin: ``setup_hook`` must NOT contain
    ``self.tree.sync(`` (without a guild kwarg). Per-guild sync is
    deferred to ``on_ready``; a global sync here would re-introduce
    the duplicate-commands bug across all 26 commands.
    """
    from clauded.bot import ClaudedBot
    src = inspect.getsource(ClaudedBot.setup_hook)
    assert "self.tree.sync(" not in src, (
        "setup_hook must not call self.tree.sync(...) — that would "
        "global-sync commands and reintroduce #185 (duplicate "
        "autocomplete entries). Per-guild sync lives in on_ready."
    )
