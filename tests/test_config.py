"""Unit tests for ``clauded.config.load_config``."""

from __future__ import annotations

from pathlib import Path

import pytest

from clauded.config import Config, load_config


@pytest.fixture
def isolated_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> pytest.MonkeyPatch:
    """Clear all clauded-relevant env vars and neutralise ``load_dotenv``.

    ``load_config`` calls ``load_dotenv()``, which scans the cwd and parents
    for a ``.env`` file. A real ``.env`` in the repo root would pollute our
    assertions, so we monkeypatch ``load_dotenv`` to a no-op **and** strip
    every env var that ``.env`` might set.
    """
    for key in (
        "DISCORD_BOT_TOKEN",
        "CLAUDE_MODEL",
        "CLAUDE_PERMISSION_MODE",
        "CLAUDED_PROJECTS_ROOT",
        "CLAUDE_CLI_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    # Prevent load_dotenv from reading .env file
    monkeypatch.setattr("clauded.config.load_dotenv", lambda *a, **kw: None)
    return monkeypatch


def test_missing_token_raises(isolated_env: pytest.MonkeyPatch) -> None:
    """No DISCORD_BOT_TOKEN → RuntimeError."""
    with pytest.raises(RuntimeError, match="DISCORD_BOT_TOKEN"):
        load_config()


def test_blank_token_raises(isolated_env: pytest.MonkeyPatch) -> None:
    """A whitespace-only token is treated as missing."""
    isolated_env.setenv("DISCORD_BOT_TOKEN", "   ")
    with pytest.raises(RuntimeError, match="DISCORD_BOT_TOKEN"):
        load_config()


def test_default_model_is_none(isolated_env: pytest.MonkeyPatch) -> None:
    """#198: CLAUDE_MODEL unset → claude_model is None (was 'sonnet').

    None signals "no admin override, let SDK pick from
    ~/.claude/settings.json" — matches terminal `claude` behavior.
    """
    isolated_env.setenv("DISCORD_BOT_TOKEN", "tok-abc")
    cfg = load_config()
    assert isinstance(cfg, Config)
    assert cfg.claude_model is None


def test_blank_model_env_is_none(isolated_env: pytest.MonkeyPatch) -> None:
    """#198: whitespace-only CLAUDE_MODEL is treated as unset → None."""
    isolated_env.setenv("DISCORD_BOT_TOKEN", "tok-abc")
    isolated_env.setenv("CLAUDE_MODEL", "   ")
    cfg = load_config()
    assert cfg.claude_model is None


def test_default_permission_mode(isolated_env: pytest.MonkeyPatch) -> None:
    """#295: CLAUDE_PERMISSION_MODE unset → ``None`` (was ``"default"``).

    Mirrors the #198 ``claude_model`` semantic: ``None`` signals "no
    admin override, let SDK omit the flag so ``~/.claude/settings.json``
    ``permissions.defaultMode`` governs" — matches terminal ``claude``
    behavior instead of silently overriding it with ``"default"``.
    """
    isolated_env.setenv("DISCORD_BOT_TOKEN", "tok-abc")
    cfg = load_config()
    assert cfg.claude_permission_mode is None


def test_blank_permission_mode_env_is_none(
    isolated_env: pytest.MonkeyPatch,
) -> None:
    """#295: whitespace-only CLAUDE_PERMISSION_MODE is treated as unset → None."""
    isolated_env.setenv("DISCORD_BOT_TOKEN", "tok-abc")
    isolated_env.setenv("CLAUDE_PERMISSION_MODE", "   ")
    cfg = load_config()
    assert cfg.claude_permission_mode is None


def test_explicit_overrides(isolated_env: pytest.MonkeyPatch) -> None:
    isolated_env.setenv("DISCORD_BOT_TOKEN", "tok-abc")
    isolated_env.setenv("CLAUDE_MODEL", "opus")
    isolated_env.setenv("CLAUDE_PERMISSION_MODE", "acceptEdits")
    cfg = load_config()
    assert cfg.claude_model == "opus"
    assert cfg.claude_permission_mode == "acceptEdits"


def test_projects_root_defaults_to_home(
    isolated_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When CLAUDED_PROJECTS_ROOT is unset the user's home dir is used."""
    home = tmp_path / "home"
    home.mkdir()
    isolated_env.setenv("HOME", str(home))
    isolated_env.setenv("DISCORD_BOT_TOKEN", "tok-abc")
    cfg = load_config()
    assert cfg.projects_root == str(home.resolve())


def test_typo_warning_logged(isolated_env, caplog):
    """Common env var typos produce a warning."""
    import logging
    isolated_env.setenv("DISCORD_BOT_TOKEN", "tok")
    isolated_env.setenv("CLAUDE_PREMISSION_MODE", "bypassPermissions")
    with caplog.at_level(logging.WARNING):
        load_config()
    assert "did you mean" in caplog.text.lower() or "CLAUDE_PERMISSION_MODE" in caplog.text
