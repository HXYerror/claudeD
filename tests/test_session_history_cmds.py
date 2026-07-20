"""#audit(#15): /session history·open·rename·tag·delete + DeleteConfirmView.

All mock-based — the claude_agent_sdk calls are patched at the session-cog seam
(session.py imports the helpers by name) or the _sessions_disk seam; nothing
touches the real filesystem, the SDK, or the live bot.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _info(sid, **kw):
    d = dict(
        session_id=sid, summary=None, last_modified=1700000000, file_size=0,
        custom_title=None, first_prompt=None, git_branch=None, cwd=None,
        tag=None, created_at=None,
    )
    d.update(kw)
    return SimpleNamespace(**d)


def _itx(channel_id=42, user_id=111):
    itx = MagicMock()
    itx.channel_id = channel_id
    itx.user = MagicMock()
    itx.user.id = user_id
    itx.response = MagicMock()
    itx.response.send_message = AsyncMock()
    itx.response.defer = AsyncMock()
    itx.response.edit_message = AsyncMock()
    itx.followup = MagicMock()
    itx.followup.send = AsyncMock()
    return itx


# --------------------------------------------------------------------------
# _resolve_session_id (pure)
# --------------------------------------------------------------------------


def test_resolve_exact_uuid():
    from clauded.cogs._sessions_disk import _resolve_session_id
    s = [_info("aaaabbbb-1111"), _info("ccccdddd-2222")]
    assert _resolve_session_id(s, "aaaabbbb-1111") == "aaaabbbb-1111"


def test_resolve_unique_prefix():
    from clauded.cogs._sessions_disk import _resolve_session_id
    s = [_info("aaaabbbb-1111"), _info("ccccdddd-2222")]
    assert _resolve_session_id(s, "aaaabbbb") == "aaaabbbb-1111"


def test_resolve_ambiguous_prefix_raises():
    from clauded.cogs._sessions_disk import _resolve_session_id
    s = [_info("aaaabbbb-1"), _info("aaaabbbb-2")]
    with pytest.raises(ValueError, match="Ambiguous"):
        _resolve_session_id(s, "aaaabbbb")


def test_resolve_no_match_raises():
    from clauded.cogs._sessions_disk import _resolve_session_id
    with pytest.raises(ValueError, match="No session"):
        _resolve_session_id([_info("aaaabbbb-1")], "zzzzzzzz")


# --------------------------------------------------------------------------
# event-loop safety: every SDK call routes through asyncio.to_thread
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sessions_routes_through_to_thread(monkeypatch):
    import clauded.cogs._sessions_disk as sd

    calls = []

    async def fake_to_thread(fn, *a, **kw):
        calls.append((fn, a))
        return ["sentinel"]

    fake_sdk = MagicMock()
    monkeypatch.setattr(sd.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(sd, "_sdk", lambda: fake_sdk)

    out = await sd._list_sessions("/dir", limit=10)
    assert out == ["sentinel"]
    assert calls[0][0] is fake_sdk.list_sessions
    assert calls[0][1] == ("/dir", 10, 0, True)  # positional order matches SDK sig


# --------------------------------------------------------------------------
# autocomplete
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autocomplete_not_ready_returns_empty():
    from clauded.cogs._sessions_disk import _session_id_autocomplete
    itx = MagicMock()
    itx.client = object()  # not a ClaudedBot
    assert await _session_id_autocomplete(itx, "") == []


@pytest.mark.asyncio
async def test_autocomplete_unbound_returns_empty(monkeypatch):
    import clauded.cogs._sessions_disk as sd
    from clauded.bot import ClaudedBot
    monkeypatch.setattr(sd, "_resolve_project_dir", lambda i, b: None)
    itx = MagicMock()
    itx.client = MagicMock(spec=ClaudedBot)
    assert await sd._session_id_autocomplete(itx, "") == []


@pytest.mark.asyncio
async def test_autocomplete_happy_filters_by_current(monkeypatch):
    import clauded.cogs._sessions_disk as sd
    from clauded.bot import ClaudedBot
    monkeypatch.setattr(sd, "_resolve_project_dir", lambda i, b: "/dir")

    async def fake_list(directory, limit=25):
        return [_info("uuid-alpha-1", summary="Alpha"), _info("uuid-beta-2", summary="Beta")]

    monkeypatch.setattr(sd, "_list_sessions", fake_list)
    itx = MagicMock()
    itx.client = MagicMock(spec=ClaudedBot)
    out = await sd._session_id_autocomplete(itx, "alpha")
    assert [c.value for c in out] == ["uuid-alpha-1"]


# --------------------------------------------------------------------------
# /session history
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_unbound_soft_refuses(monkeypatch):
    from clauded.cogs.session import session_history
    from clauded.bot import ClaudedBot
    monkeypatch.setattr("clauded.cogs.session._resolve_project_dir", lambda i, b: None)
    itx = _itx()
    itx.client = MagicMock(spec=ClaudedBot)
    await session_history.callback(itx, 10)
    itx.response.send_message.assert_awaited_once()
    assert "isn't bound" in itx.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_history_happy_renders_embed(monkeypatch):
    from clauded.cogs.session import session_history
    from clauded.bot import ClaudedBot
    bot = MagicMock(spec=ClaudedBot)
    bot._get_resume_session_id = MagicMock(return_value=None)
    monkeypatch.setattr("clauded.cogs.session._resolve_project_dir", lambda i, b: "/dir")

    async def fake_list(directory, limit=25):
        return [_info("uuid-1234-5678", summary="Hello world")]

    monkeypatch.setattr("clauded.cogs.session._list_sessions", fake_list)
    itx = _itx()
    itx.client = bot
    await session_history.callback(itx, 10)
    itx.response.defer.assert_awaited_once()
    embed = itx.followup.send.await_args.kwargs["embed"]
    assert "Past sessions" in embed.title


# --------------------------------------------------------------------------
# /session open
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_missing_session_errors_without_recreate(monkeypatch):
    from clauded.cogs.session import session_open
    from clauded.bot import ClaudedBot
    bot = MagicMock(spec=ClaudedBot)
    bot._recreate_session = AsyncMock()
    monkeypatch.setattr("clauded.cogs.session.reject_if_unbound", AsyncMock(return_value=False))
    monkeypatch.setattr("clauded.cogs.session._resolve_project_dir", lambda i, b: "/dir")

    async def fake_get(d, sid):
        return None

    monkeypatch.setattr("clauded.cogs.session._get_info", fake_get)
    itx = _itx()
    itx.client = bot
    await session_open.callback(itx, "nope-uuid")
    bot._recreate_session.assert_not_awaited()
    itx.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_happy_recreates_with_resume_id(monkeypatch):
    from clauded.cogs.session import session_open
    from clauded.bot import ClaudedBot
    bot = MagicMock(spec=ClaudedBot)
    bot._recreate_session = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr("clauded.cogs.session.reject_if_unbound", AsyncMock(return_value=False))
    monkeypatch.setattr("clauded.cogs.session._resolve_project_dir", lambda i, b: "/dir")

    async def fake_get(d, sid):
        return _info(sid)

    monkeypatch.setattr("clauded.cogs.session._get_info", fake_get)
    itx = _itx()
    itx.client = bot
    await session_open.callback(itx, "uuid-full-1234")
    bot._recreate_session.assert_awaited_once()
    assert bot._recreate_session.await_args.kwargs.get("resume_session_id") == "uuid-full-1234"


# --------------------------------------------------------------------------
# /session tag (clear semantics)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tag_dash_clears_to_none(monkeypatch):
    from clauded.cogs.session import session_tag
    from clauded.bot import ClaudedBot
    monkeypatch.setattr("clauded.cogs.session.reject_if_unbound", AsyncMock(return_value=False))
    monkeypatch.setattr("clauded.cogs.session._resolve_project_dir", lambda i, b: "/dir")

    async def fake_list(d, limit=50):
        return [_info("uuid-tag-1234")]

    captured = {}

    async def fake_tag(d, sid, tag):
        captured["tag"] = tag

    monkeypatch.setattr("clauded.cogs.session._list_sessions", fake_list)
    monkeypatch.setattr("clauded.cogs.session._tag", fake_tag)
    itx = _itx()
    itx.client = MagicMock(spec=ClaudedBot)
    await session_tag.callback(itx, "uuid-tag-1234", "-")
    assert captured["tag"] is None


# --------------------------------------------------------------------------
# /session delete + DeleteConfirmView
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_refuses_active_session(monkeypatch):
    from clauded.cogs.session import session_delete
    from clauded.bot import ClaudedBot
    bot = MagicMock(spec=ClaudedBot)
    bot._get_resume_session_id = MagicMock(return_value="uuid-active-1234")
    monkeypatch.setattr("clauded.cogs.session.reject_if_unbound", AsyncMock(return_value=False))
    monkeypatch.setattr("clauded.cogs.session._resolve_project_dir", lambda i, b: "/dir")

    async def fake_list(d, limit=50):
        return [_info("uuid-active-1234")]

    del_spy = AsyncMock()
    monkeypatch.setattr("clauded.cogs.session._list_sessions", fake_list)
    monkeypatch.setattr("clauded.cogs.session._delete", del_spy)
    itx = _itx()
    itx.client = bot
    await session_delete.callback(itx, "uuid-active-1234")
    del_spy.assert_not_awaited()
    assert "active/stored" in itx.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_delete_valid_shows_confirm_view(monkeypatch):
    from clauded.cogs.session import session_delete, DeleteConfirmView
    from clauded.bot import ClaudedBot
    bot = MagicMock(spec=ClaudedBot)
    bot._get_resume_session_id = MagicMock(return_value="uuid-active-1234")
    monkeypatch.setattr("clauded.cogs.session.reject_if_unbound", AsyncMock(return_value=False))
    monkeypatch.setattr("clauded.cogs.session._resolve_project_dir", lambda i, b: "/dir")

    async def fake_list(d, limit=50):
        return [_info("uuid-other-9999", summary="Other")]

    monkeypatch.setattr("clauded.cogs.session._list_sessions", fake_list)
    itx = _itx()
    itx.client = bot
    await session_delete.callback(itx, "uuid-other-9999")
    view = itx.followup.send.await_args.kwargs.get("view")
    assert isinstance(view, DeleteConfirmView)
    assert view._sid == "uuid-other-9999"
    assert view._author == itx.user.id


@pytest.mark.asyncio
async def test_confirm_view_non_author_refused():
    from clauded.cogs.session import DeleteConfirmView
    v = DeleteConfirmView("/dir", "sid-1234", author_id=111)
    itx = MagicMock()
    itx.user.id = 222  # NOT the author
    itx.response.send_message = AsyncMock()
    ok = await v.interaction_check(itx)
    assert ok is False
    itx.response.send_message.assert_awaited_once()  # explicit refusal, not a bare False


@pytest.mark.asyncio
async def test_confirm_view_author_deletes(monkeypatch):
    import clauded.cogs.session as se
    from clauded.cogs.session import DeleteConfirmView
    del_spy = AsyncMock()
    monkeypatch.setattr(se, "_delete", del_spy)
    v = DeleteConfirmView("/dir", "sid-1234", author_id=111)
    itx = MagicMock()
    itx.user.id = 111
    itx.response.edit_message = AsyncMock()
    await v._confirm.callback(itx)
    del_spy.assert_awaited_once_with("/dir", "sid-1234")
    itx.response.edit_message.assert_awaited_once()
