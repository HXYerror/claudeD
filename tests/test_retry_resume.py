"""#227 — renderer crash retry resumes SDK conversation (not fresh-start).

When the renderer crashes mid-turn, the Retry button must rebuild bridge
with `SessionConfig(resume_session_id=...)` so the SDK continues the same
conversation. Without this, every crash dropped all conversation context.

R1 engineer/tester caught: original v1 tests bypassed prod code (built
SessionConfig inside the test, asserted MagicMock returns) — would pass
even with the production fix reverted. v2 drives the module-level helper
`_build_retry_session_config` directly so mental revert fails ≥1 test.
"""
import logging
import pytest

from clauded.bot import _build_retry_session_config
from clauded.session_config import SessionConfig


# -- core fix: resume_session_id propagates from stored to SessionConfig --

def test_retry_helper_propagates_stored_session_id():
    """#227 core: when stored.session_id is set, the retry SessionConfig
    must carry it as resume_session_id. THIS test is the regression pin —
    if the production line is reverted, this assertion fails."""
    stored = {
        "session_id": "sess-resume-target-uuid",
        "model": None,
        "permission_mode_override": None,
    }
    base_sc = SessionConfig(model_override="opus")

    retry_sc = _build_retry_session_config(
        stored,
        base_sc,
        on_ask_user=lambda *_a, **_k: None,
        thread_id_for_log=42,
    )

    assert retry_sc.resume_session_id == "sess-resume-target-uuid", (
        "#227 regression: retry SessionConfig lost the stored session_id; "
        "renderer crash will cold-start and discard conversation context"
    )


def test_retry_helper_preserves_other_session_config_fields():
    """Non-regression: every non-on_ask_user field flows through unchanged."""
    base_sc = SessionConfig(
        system_prompt="be helpful",
        model_override="opus",
        permission_mode_override="acceptEdits",
        effort="high",
        allowed_tools=["Read", "Write"],
        max_budget_usd=5.0,
    )
    stored = {"session_id": "s1"}

    retry_sc = _build_retry_session_config(
        stored, base_sc, on_ask_user=lambda *_a, **_k: "answer"
    )

    assert retry_sc.system_prompt == "be helpful"
    assert retry_sc.model_override == "opus"
    assert retry_sc.permission_mode_override == "acceptEdits"
    assert retry_sc.effort == "high"
    assert retry_sc.allowed_tools == ["Read", "Write"]
    assert retry_sc.max_budget_usd == 5.0
    # on_ask_user must be the fresh callback, not base_sc.on_ask_user
    assert retry_sc.on_ask_user is not base_sc.on_ask_user


# -- fallback: stored missing → cold start + warning --

def test_retry_helper_falls_back_when_stored_missing(caplog):
    """No stored entry → SessionConfig.resume_session_id is None +
    log.warning fires so #224 /log dump epic captures it."""
    caplog.set_level(logging.WARNING, logger="clauded.bot")

    retry_sc = _build_retry_session_config(
        None,
        SessionConfig(),
        on_ask_user=lambda *_a, **_k: None,
        thread_id_for_log=99,
    )

    assert retry_sc.resume_session_id is None
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "#227" in r.getMessage()
        and "cold start" in r.getMessage()
    ]
    assert warnings, (
        f"missing #227 cold-start warning; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    assert "99" in warnings[0].getMessage()


def test_retry_helper_falls_back_when_session_id_field_missing(caplog):
    """Stored row exists but session_id key absent (legacy or partial).
    Same fallback: None resume + warning fires."""
    caplog.set_level(logging.WARNING, logger="clauded.bot")

    retry_sc = _build_retry_session_config(
        {"model": "opus"},  # no session_id key
        SessionConfig(),
        on_ask_user=lambda *_a, **_k: None,
    )

    assert retry_sc.resume_session_id is None
    assert any(
        "#227" in r.getMessage() and "cold start" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


# -- base_sc None edge --

def test_retry_helper_handles_none_base_session_config():
    """Defensive: if upstream lost the original SessionConfig (legacy code
    path), still build a minimal one with resume_session_id wired."""
    stored = {"session_id": "s-edge"}

    retry_sc = _build_retry_session_config(
        stored, None, on_ask_user=lambda *_a, **_k: None
    )

    assert retry_sc.resume_session_id == "s-edge"
    assert retry_sc.system_prompt is None


# -- engineer Q3: resume + permission_mode_override pairing --

def test_retry_helper_pairs_resume_with_permission_mode_override():
    """Engineer R1 Q3: resume_session_id pulled from stored, but
    permission_mode_override pulled from in-process base_sc snapshot. Pin
    that both flow through together so SDK resume + persistent mode work
    in tandem (#211 / #221 interaction)."""
    base_sc = SessionConfig(
        permission_mode_override="bypassPermissions",
        model_override="opus",
    )
    stored = {"session_id": "s-mode-pair", "permission_mode_override": "plan"}

    retry_sc = _build_retry_session_config(
        stored, base_sc, on_ask_user=lambda *_a, **_k: None
    )

    # resume from stored.session_id (server-side conversation)
    assert retry_sc.resume_session_id == "s-mode-pair"
    # permission_mode_override from in-process base_sc (client-side override)
    # NOT from stored (stored.permission_mode_override is read by a
    # separate auto-resume path, not here)
    assert retry_sc.permission_mode_override == "bypassPermissions"


# -- public crash embed copy --

def test_crash_embed_copy_says_conversation_will_continue():
    """Pin user-visible copy against accidental revert."""
    from clauded.discord_renderer import DiscordRenderer
    import inspect

    src = inspect.getsource(DiscordRenderer.send_error_with_retry)
    # Adjacent literals split across newlines; check fragments
    assert "continue from where it" in src
    assert "crashed (#227)" in src
    assert "fresh session will be started" not in src, (
        "crash embed still uses pre-#227 wording 'fresh session will be started'"
    )
