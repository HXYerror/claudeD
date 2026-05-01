"""Configuration loading from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    """Runtime configuration for the Discord-Claude bridge."""

    discord_bot_token: str
    claude_model: str
    claude_permission_mode: str
    projects_root: str


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

    return Config(
        discord_bot_token=token,
        claude_model=os.environ.get("CLAUDE_MODEL", "sonnet").strip() or "sonnet",
        claude_permission_mode=(
            os.environ.get("CLAUDE_PERMISSION_MODE", "default").strip()
            or "default"
        ),
        projects_root=projects_root,
    )
