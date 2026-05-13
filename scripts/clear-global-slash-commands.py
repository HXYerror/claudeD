#!/usr/bin/env python3
"""One-shot: clear all GLOBAL slash commands for the bot.

#185 cleanup: claudeD historically registered commands in BOTH global
and per-guild scopes, so each command appeared twice in Discord
autocomplete. Per-guild is the new canonical scope (instant sync vs
~1h global propagation). Run this script ONCE to clear the leftover
global commands, then the in-tree fix prevents re-registration.

This script is destructive. It is gated on ``CONFIRM=1`` so a
copy-paste-and-run accident can't silently nuke production commands.

## Usage
    cd /path/to/claudeD
    CONFIRM=1 .venv/bin/python scripts/clear-global-slash-commands.py

Reads ``DISCORD_BOT_TOKEN`` from ``.env`` in the repo root.
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

try:
    from dotenv import dotenv_values
except ImportError:
    print("dotenv not installed. Run `pip install python-dotenv`.")
    sys.exit(1)


APP_ID = "1499415416701980704"


def main() -> int:
    if os.environ.get("CONFIRM") != "1":
        print(
            "REFUSED: this clears GLOBAL slash commands.\n"
            "If you really mean to do that, re-run with CONFIRM=1:\n\n"
            "    CONFIRM=1 .venv/bin/python scripts/clear-global-slash-commands.py\n"
        )
        return 1
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        print(f"ERROR: .env not found at {env_path}")
        return 2
    env = dotenv_values(env_path)
    token = env.get("DISCORD_BOT_TOKEN")
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN missing from .env")
        return 3

    url = f"https://discord.com/api/v10/applications/{APP_ID}/commands"
    print(f"Bulk-overwriting GLOBAL commands at {url} to []...")
    req = urllib.request.Request(
        url,
        data=b"[]",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (claudeD-cleanup, 1.0)",
        },
        method="PUT",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        body = json.loads(resp.read())
        if isinstance(body, list) and len(body) == 0:
            print("OK: GLOBAL commands cleared (now 0).")
            return 0
        print(f"UNEXPECTED response shape: {body!r}")
        return 4
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()[:300]}")
        return 5


if __name__ == "__main__":
    sys.exit(main())
