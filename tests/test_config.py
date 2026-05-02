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


def test_default_model_is_sonnet(isolated_env: pytest.MonkeyPatch) -> None:
    isolated_env.setenv("DISCORD_BOT_TOKEN", "tok-abc")
    cfg = load_config()
    assert isinstance(cfg, Config)
    assert cfg.claude_model == "sonnet"


def test_default_permission_mode(isolated_env: pytest.MonkeyPatch) -> None:
    """The current default permission mode is ``"default"``."""
    isolated_env.setenv("DISCORD_BOT_TOKEN", "tok-abc")
    cfg = load_config()
    assert cfg.claude_permission_mode == "default"


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
