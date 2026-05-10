"""Channel ↔ project-directory bindings, persisted to JSON.

A `ProjectManager` maps Discord channel IDs to absolute filesystem paths.
State is loaded from and saved to ``<data_dir>/projects.json``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Set, Tuple

log = logging.getLogger("clauded.project_manager")


class ProjectManager:
    """Persisted store of channel-id → project-directory bindings."""

    def __init__(
        self,
        data_dir: str = "data",
        projects_root: str | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "projects.json")
        # Path under which all bindings must live. Defaults to the user's
        # home directory if not supplied. Stored as a fully-resolved Path so
        # symlink-escapes are caught by the bind-time validation.
        root = projects_root if projects_root is not None else str(Path.home())
        self.projects_root: Path = Path(root).expanduser().resolve()
        self._path = Path(self.path)
        self._projects: Dict[str, Dict[str, str]] = {}
        self._guild_roots: Dict[str, str] = {}
        # Channels that have already been shown the "unbound, falling back to
        # ~" hint this process. In-memory only — a bot restart re-arms the
        # one-shot hint, which is the desired behavior per PRD R3.1.
        self._hinted_unbound_channels: Set[int] = set()
        self._load()
        self._load_guild_roots()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._projects = data
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Failed to load %s: %s — starting with empty state", self.path, exc)
            self._projects = {}

    def _save(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        # Atomic write: dump to a sibling .tmp file then rename, so a crash
        # mid-write can't truncate the live projects.json.
        tmp_path = self._path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._projects, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self._path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def bind(self, channel_id: int, path: str, *, guild_id: int | None = None) -> str:
        """Bind ``channel_id`` to ``path``.

        Expands ``~``, fully resolves symlinks, and validates that the
        resulting absolute path is a directory living *under*
        :attr:`projects_root`. Raw (unresolved) ``..`` components in the
        user-supplied path are rejected outright.

        Returns the absolute, resolved path that was actually stored.

        Raises:
            ValueError: if the path traverses out of the allowed root or
                does not point to an existing directory.
        """
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Path must be a non-empty string.")

        # Reject raw `..` traversal in the input. We also resolve symlinks
        # below; this is a belt-and-braces check that catches sneaky inputs
        # before they ever touch the filesystem.
        raw_parts = Path(path).expanduser().parts
        if any(part == ".." for part in raw_parts):
            raise ValueError("Path may not contain '..' segments.")

        try:
            resolved = Path(path).expanduser().resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"Path does not exist: {path}") from exc
        except OSError as exc:
            raise ValueError(f"Could not resolve path: {exc}") from exc

        if not resolved.is_dir():
            raise ValueError(f"Not a directory: {resolved}")

        # Confirm the resolved (symlink-followed) path stays under the
        # configured root. ``Path.is_relative_to`` is available in 3.9+.
        effective_root = self.get_guild_root(guild_id)
        try:
            resolved.relative_to(effective_root)
        except ValueError as exc:
            raise ValueError(
                f"Path {resolved} is outside the allowed projects root "
                f"{effective_root}."
            ) from exc

        expanded = str(resolved)
        self._projects[str(channel_id)] = {
            "path": expanded,
            "bound_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()
        return expanded

    def unbind(self, channel_id: int) -> bool:
        """Remove the binding for ``channel_id``. Returns True if removed."""
        if self._projects.pop(str(channel_id), None) is None:
            return False
        self._save()
        return True

    def get_project(self, channel_id: int) -> str | None:
        """Return the bound path for ``channel_id``, or None if unbound."""
        entry = self._projects.get(str(channel_id))
        if not entry:
            return None
        return entry.get("path")

    # Alias used by callers that prefer the "path" terminology.
    def get_path(self, channel_id: int) -> str | None:
        """Alias of :meth:`get_project`."""
        return self.get_project(channel_id)

    def is_bound(self, channel_id: int) -> bool:
        """Return True if ``channel_id`` has a binding."""
        return str(channel_id) in self._projects

    # ------------------------------------------------------------------
    # Unbound-channel fallback (v1.11, #110)
    # ------------------------------------------------------------------
    def get_path_or_default(self, channel_id: int) -> Tuple[Path, bool]:
        """Return ``(path, is_bound)`` for ``channel_id``.

        - When the channel is bound, returns ``(Path(bound_path), True)``.
        - When unbound, returns ``(Path.home().resolve(), False)``.

        The ``.resolve()`` on the home-directory fallback is intentional —
        macOS ships ``/tmp -> /private/tmp`` and similar symlinked layouts
        that the v1.0 `#11` fix taught us must be canonicalized at the
        binding boundary. Applying ``.resolve()`` to ``Path.home()`` here
        keeps the unbound path subject to the same canonicalization
        discipline (e.g. ``/Users/x`` vs ``/private/Users/x``) so the cwd
        we hand to Claude matches whatever it would receive after a
        ``/project bind``.
        """
        if self.is_bound(channel_id):
            p = self.get_path(channel_id)
            assert p is not None  # is_bound guarantees this
            return Path(p), True
        return Path.home().resolve(), False

    def should_hint_unbound(self, channel_id: int) -> bool:
        """Return True the FIRST time we should nudge ``channel_id`` about
        ``/project bind``; False on every subsequent call.

        Atomic: the membership check and insertion happen together, so two
        concurrent callers can't both receive ``True``. State is in-memory
        only and resets on bot restart (PRD R3.1).
        """
        if channel_id in self._hinted_unbound_channels:
            return False
        self._hinted_unbound_channels.add(channel_id)
        return True

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------
    def set_system_prompt(self, channel_id: int, prompt: str) -> None:
        """Store a system prompt for the given channel binding."""
        key = str(channel_id)
        entry = self._projects.get(key)
        if entry is None:
            entry = {}
            self._projects[key] = entry
        entry["system_prompt"] = prompt
        self._save()

    def get_system_prompt(self, channel_id: int) -> str | None:
        """Return the system prompt for ``channel_id``, or None."""
        entry = self._projects.get(str(channel_id))
        if entry is None:
            return None
        return entry.get("system_prompt")

    def clear_system_prompt(self, channel_id: int) -> None:
        """Remove the system prompt for ``channel_id`` if present."""
        key = str(channel_id)
        entry = self._projects.get(key)
        if entry is not None and "system_prompt" in entry:
            del entry["system_prompt"]
            self._save()

    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------
    def set_budget(self, channel_id: int, amount: float) -> None:
        """Store a max budget (USD) for the given channel binding."""
        key = str(channel_id)
        entry = self._projects.get(key)
        if entry is None:
            entry = {}
            self._projects[key] = entry
        entry["budget"] = amount
        self._save()

    def get_budget(self, channel_id: int) -> float | None:
        """Return the budget for ``channel_id``, or None."""
        entry = self._projects.get(str(channel_id))
        if entry is None:
            return None
        val = entry.get("budget")
        return float(val) if val is not None else None

    def clear_budget(self, channel_id: int) -> None:
        """Remove the budget for ``channel_id`` if present."""
        key = str(channel_id)
        entry = self._projects.get(key)
        if entry is not None and "budget" in entry:
            del entry["budget"]
            self._save()

    # ------------------------------------------------------------------
    # Extra directories
    # ------------------------------------------------------------------
    def add_extra_dir(self, channel_id: int, path: str) -> str:
        """Add an extra directory. Validates and stores. Returns resolved path."""
        raw_parts = Path(path).expanduser().parts
        if any(part == ".." for part in raw_parts):
            raise ValueError("Path may not contain '..' segments.")
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError(f"Not a directory: {path}")
        try:
            resolved.relative_to(self.projects_root)
        except ValueError:
            raise ValueError(
                f"Path {resolved} is outside the allowed projects root {self.projects_root}."
            ) from None
        key = str(channel_id)
        entry = self._projects.get(key, {})
        dirs = entry.get("extra_dirs", [])
        resolved_str = str(resolved)
        if resolved_str not in dirs:
            dirs.append(resolved_str)
        entry["extra_dirs"] = dirs
        self._projects[key] = entry
        self._save()
        return resolved_str

    def get_extra_dirs(self, channel_id: int) -> list[str]:
        """Return extra directories for ``channel_id``."""
        entry = self._projects.get(str(channel_id), {})
        return entry.get("extra_dirs", [])

    def remove_extra_dir(self, channel_id: int, path: str) -> bool:
        """Remove an extra directory. Returns True if removed."""
        key = str(channel_id)
        entry = self._projects.get(key, {})
        dirs = entry.get("extra_dirs", [])
        resolved = str(Path(path).expanduser().resolve())
        if resolved in dirs:
            dirs.remove(resolved)
            entry["extra_dirs"] = dirs
            self._save()
            return True
        return False

    # ------------------------------------------------------------------
    # MCP servers
    # ------------------------------------------------------------------
    def add_mcp_server(self, channel_id: int, name: str, config: dict) -> None:
        """Add an MCP server configuration for the given channel.

        ``config`` should be a dict matching one of the Claude SDK MCP
        server config shapes, e.g.
        ``{"type": "stdio", "command": "npx", "args": [...]}`` or
        ``{"type": "http", "url": "https://..."}``
        """
        key = str(channel_id)
        entry = self._projects.get(key, {})
        mcps = entry.get("mcp_servers", {})
        mcps[name] = config
        entry["mcp_servers"] = mcps
        self._projects[key] = entry
        self._save()

    def get_mcp_servers(self, channel_id: int) -> dict:
        """Return all MCP server configs for ``channel_id``."""
        return self._projects.get(str(channel_id), {}).get("mcp_servers", {})

    def remove_mcp_server(self, channel_id: int, name: str) -> bool:
        """Remove an MCP server by name. Returns True if it existed."""
        key = str(channel_id)
        entry = self._projects.get(key, {})
        mcps = entry.get("mcp_servers", {})
        if name in mcps:
            del mcps[name]
            entry["mcp_servers"] = mcps
            self._save()
            return True
        return False

    # ------------------------------------------------------------------
    # Environment variables
    # ------------------------------------------------------------------
    def set_env(self, channel_id: int, key: str, value: str) -> None:
        """Store an environment variable for the given channel binding."""
        k = str(channel_id)
        entry = self._projects.get(k, {})
        env = entry.get("env", {})
        env[key] = value
        entry["env"] = env
        self._projects[k] = entry
        self._save()

    def get_env(self, channel_id: int) -> dict[str, str]:
        """Return all environment variables for ``channel_id``."""
        entry = self._projects.get(str(channel_id), {})
        return dict(entry.get("env", {}))

    def remove_env(self, channel_id: int, key: str) -> bool:
        """Remove an environment variable. Returns True if removed."""
        k = str(channel_id)
        entry = self._projects.get(k, {})
        env = entry.get("env", {})
        if key in env:
            del env[key]
            entry["env"] = env
            self._save()
            return True
        return False

    # ------------------------------------------------------------------
    # Channel mode (thread vs forum) — #87
    # ------------------------------------------------------------------
    def set_channel_mode(self, channel_id: int, mode: str) -> None:
        """Set channel mode: 'thread' (default) or 'forum'."""
        if mode not in ("thread", "forum"):
            raise ValueError(f"Invalid mode: {mode!r}. Must be 'thread' or 'forum'.")
        key = str(channel_id)
        entry = self._projects.setdefault(key, {})
        entry["channel_mode"] = mode
        self._save()

    def get_channel_mode(self, channel_id: int) -> str:
        """Return the channel mode ('thread' or 'forum') for ``channel_id``."""
        return self._projects.get(str(channel_id), {}).get("channel_mode", "thread")

    # ------------------------------------------------------------------
    # Per-guild project root — #91
    # ------------------------------------------------------------------
    def set_guild_root(self, guild_id: int, path: str) -> str:
        """Set a per-guild projects root directory.

        Validates that the path exists and is a directory. Stores the
        mapping in ``guild_roots.json`` alongside ``projects.json``.
        Returns the resolved absolute path.

        Raises:
            ValueError: if ``path`` is not an existing directory.
        """
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"Not a directory: {path}")
        self._guild_roots[str(guild_id)] = str(resolved)
        self._save_guild_roots()
        return str(resolved)

    def get_guild_root(self, guild_id: int | None) -> Path:
        """Return the projects root for a guild, falling back to the default."""
        if guild_id is not None:
            custom = self._guild_roots.get(str(guild_id))
            if custom:
                return Path(custom).resolve()
        return self.projects_root

    def clear_guild_root(self, guild_id: int) -> bool:
        """Remove a per-guild root override. Returns True if one existed."""
        if self._guild_roots.pop(str(guild_id), None) is not None:
            self._save_guild_roots()
            return True
        return False

    def _load_guild_roots(self) -> None:
        p = os.path.join(self.data_dir, "guild_roots.json")
        if not os.path.isfile(p):
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._guild_roots = data
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Failed to load guild_roots.json: %s", exc)

    def _save_guild_roots(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        p = Path(self.data_dir) / "guild_roots.json"
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._guild_roots, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
