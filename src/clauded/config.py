"""Configuration loading from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import logging

from dotenv import load_dotenv

log = logging.getLogger("clauded.config")


@dataclass(frozen=True)
class Config:
    """Runtime configuration for the Discord-Claude bridge."""

    discord_bot_token: str
    claude_model: Optional[str]
    # #295: mirrors #198's ``claude_model`` semantics. Unset env
    # (``CLAUDE_PERMISSION_MODE``) → ``None`` → SDK call omits
    # ``permission_mode`` → CLI's ``~/.claude/settings.json``
    # ``permissions.defaultMode`` governs. Setting the env var pins the
    # mode for every session (still overridable per-thread via ``/mode set``).
    claude_permission_mode: Optional[str]
    projects_root: str
    # SECURITY: when False (default), an unbound channel's @bot message is
    # silently ignored — restoring v1.0 behavior. When True, the on-message
    # handler falls back to ``$HOME`` as cwd. Toggling on grants any user
    # with channel-write permission shell access to the operator's home,
    # so this stays opt-in. See PRD R4.2/R4.3.
    allow_unbound_fallback: bool = False


def load_config() -> Config:
    """Load configuration from environment variables (and `.env` if present)."""
    load_dotenv()

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )

    raw_root = os.environ.get("CLAUDED_PROJECTS_ROOT", "").strip()
    if raw_root:
        projects_root = str(Path(raw_root).expanduser().resolve())
    else:
        projects_root = str(Path.home().resolve())

    # Check for common env-var typos
    typo_map = {
        "CLAUDE_PREMISSION_MODE": "CLAUDE_PERMISSION_MODE",
        "DISCORD_TOKEN": "DISCORD_BOT_TOKEN",
        "CLAUDED_PROJECT_ROOT": "CLAUDED_PROJECTS_ROOT",
    }
    for typo, correct in typo_map.items():
        if os.environ.get(typo):
            log.warning("Found '%s' in env — did you mean '%s'?", typo, correct)

    # #198: claude_model is Optional[str]. UNSET env var → None → SDK call
    # omits the `model` kwarg → CLI default from ~/.claude/settings.json is
    # used (same as terminal `claude`). Setting CLAUDE_MODEL is now an
    # explicit admin/ops "force-pin" knob rather than the default path.
    _claude_model_env = os.environ.get("CLAUDE_MODEL", "").strip()
    claude_model: Optional[str] = _claude_model_env or None

    # #295: same shape as #198 above — unset env var means "let CLI
    # settings.json govern" (no forced ``permission_mode=default`` that
    # used to override ``permissions.defaultMode``). Empty / whitespace
    # is treated as unset.
    _claude_perm_env = os.environ.get("CLAUDE_PERMISSION_MODE", "").strip()
    claude_permission_mode: Optional[str] = _claude_perm_env or None

    return Config(
        discord_bot_token=token,
        claude_model=claude_model,
        claude_permission_mode=claude_permission_mode,
        projects_root=projects_root,
        allow_unbound_fallback=(
            os.environ.get("CLAUDED_ALLOW_UNBOUND_FALLBACK", "").strip().lower()
            in ("1", "true", "yes", "on")
        ),
    )
