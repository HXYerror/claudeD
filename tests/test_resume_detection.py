"""T2-B: silent-resume-failure detection in ClaudeBridge._capture_session_id.

When a resume was requested but the CLI hands back a different session_id on
the first message, the resume didn't take (CLI started fresh). The bridge must
flag ``resume_failed`` + fire ``on_resume_failed`` so the bot can surface it,
instead of silently losing the user's conversation context.
"""
from __future__ import annotations

from clauded.claude_bridge import ClaudeBridge
from clauded.session_config import SessionConfig
from clauded.config import Config


def _cfg() -> Config:
    return Config(
        discord_bot_token="x",
        claude_model=None,
        claude_permission_mode="default",
        projects_root="/tmp",
    )


class _Msg:
    def __init__(self, sid: str) -> None:
        self.session_id = sid


def test_resume_mismatch_flags_and_fires_callback():
    fired: list[tuple[str, str]] = []
    b = ClaudeBridge(
        "/tmp", _cfg(),
        SessionConfig(resume_session_id="REQ-123",
                      on_resume_failed=lambda r, a: fired.append((r, a))),
    )
    b._capture_session_id(_Msg("DIFFERENT-456"))
    assert b.resume_failed is True
    assert fired == [("REQ-123", "DIFFERENT-456")]


def test_matching_resume_id_is_not_a_failure():
    fired: list = []
    b = ClaudeBridge(
        "/tmp", _cfg(),
        SessionConfig(resume_session_id="SAME",
                      on_resume_failed=lambda r, a: fired.append(1)),
    )
    b._capture_session_id(_Msg("SAME"))
    assert b.resume_failed is False
    assert fired == []


def test_fork_session_new_id_is_not_a_failure():
    # fork_session intentionally produces a new session_id.
    fired: list = []
    b = ClaudeBridge(
        "/tmp", _cfg(),
        SessionConfig(resume_session_id="X", fork_session=True,
                      on_resume_failed=lambda r, a: fired.append(1)),
    )
    b._capture_session_id(_Msg("Y"))
    assert b.resume_failed is False
    assert fired == []


def test_cold_start_no_resume_requested_is_not_a_failure():
    b = ClaudeBridge("/tmp", _cfg(), SessionConfig(resume_session_id=None))
    b._capture_session_id(_Msg("fresh-id"))
    assert b.resume_failed is False


def test_callback_exception_is_swallowed():
    def _boom(_r, _a):
        raise RuntimeError("boom")
    b = ClaudeBridge(
        "/tmp", _cfg(),
        SessionConfig(resume_session_id="REQ", on_resume_failed=_boom),
    )
    # Must not raise even though the callback throws.
    b._capture_session_id(_Msg("OTHER"))
    assert b.resume_failed is True
