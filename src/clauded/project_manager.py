"""Channel ↔ project-directory bindings, persisted to JSON.

A `ProjectManager` maps Discord channel IDs to absolute filesystem paths.
State is loaded from and saved to ``<data_dir>/projects.json``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from ._json_store import atomic_write_json
from ._validation import validate_identifier, validate_env_key

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
        self._projects: dict[str, dict[str, str]] = {}
        self._guild_roots: dict[str, str] = {}
        # Per-channel settings that survive unbind/rebind (v1.17 #138).
        # Stored separately from ``_projects`` so ``unbind`` semantics stay
        # unchanged for system_prompt/budget/etc. Default-True at lookup
        # time means an empty registry preserves v1.1 baseline behavior.
        self._channel_settings: dict[str, dict] = {}
        # Channels that have already been shown the "unbound, falling back to
        # ~" hint this process. In-memory only — bot restart re-arms the hint.
        self._hinted_unbound_channels: set[int] = set()
        # Channels where we've already replied with the unbound-refusal hint
        # this process (v1.18: nudge user to /project bind exactly once per
        # unbound channel so silent-ignore doesn't become invisible-failure).
        # In-memory only — bot restart re-arms.
        self._refused_unbound_channels: set[int] = set()
        # #252: single RLock guards both _projects and _channel_settings
        # writes. RLock so any future helper that already holds the lock
        # can call ``_save`` without deadlocking. One lock for both
        # dicts is fine — they're written from the same callers and the
        # writes are cheap.
        self._lock = threading.RLock()
        self._load()
        self._load_guild_roots()
        self._load_channel_settings()

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
        # #209 operator visibility: report binding entry count so operators
        # who upgrade from pre-#209 builds know the file may contain
        # thread.id-keyed rows (silently written by the old buggy cogs).
        # We don't try to distinguish thread.id from channel.id keys —
        # Discord ids aren't separable without external state — and we
        # deliberately do NOT mutate; new writes route through
        # ``resolve_binding_id`` and land on parent_id, leaving any legacy
        # thread.id rows as dead data. Users on a polluted row simply re-run
        # ``/project bind`` in the parent channel.
        log.info(
            "#209: loaded %d binding entries; thread.id pollution may exist in "
            "pre-#209 builds (no mutation; new writes use resolve_binding_id).",
            len(self._projects),
        )

    def _save(self) -> None:
        # Atomic write via shared helper (#252): unique tmp filename +
        # RLock so concurrent writers don't clobber each other's tmp and
        # the read-modify-write critical sections in the mutator methods
        # don't tear under thread races.
        atomic_write_json(
            self._path, self._projects, self._lock, sort_keys=True,
        )

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
        with self._lock:
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
        with self._lock:
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

    def _assert_bound(self, channel_id: int) -> None:
        """Defense in depth: refuse mutating ops on unbound channels.

        Cog-level ``reject_if_unbound`` is the user-facing guard; this raises
        a programming-error ``ValueError`` if a caller bypasses it.
        """
        if not self.is_bound(channel_id):
            raise ValueError(
                f"channel {channel_id} is not bound; cannot mutate project state"
            )

    # ------------------------------------------------------------------
    # Unbound-channel fallback
    # ------------------------------------------------------------------
    def get_path_or_default(self, channel_id: int) -> tuple[Path, bool]:
        """Return ``(path, is_bound)`` — bound path or ``$HOME`` fallback."""
        if self.is_bound(channel_id):
            p = self.get_path(channel_id)
            if p is not None:
                return Path(p), True
            # Partial entry (no path key) — treat as unbound.
        try:
            return Path.home().resolve(), False
        except (RuntimeError, OSError):
            # ``Path.home()`` raises when ``$HOME`` is unset and there's no
            # passwd entry. Return a sentinel that ``Path.is_dir()`` reports
            # False for, so the caller's broken-home guard fires cleanly.
            return Path("/nonexistent"), False

    def should_hint_unbound(self, channel_id: int) -> bool:
        """Return True only the first time we should nudge ``channel_id`` about /project bind."""
        if channel_id in self._hinted_unbound_channels:
            return False
        self._hinted_unbound_channels.add(channel_id)
        return True

    def should_refuse_unbound(self, channel_id: int) -> bool:
        """Return True only the first time we should reply with the refusal hint.

        Used by ``on_message`` when ``allow_unbound_fallback`` is False so the
        user sees one ``UNBOUND_REFUSE_MESSAGE`` reply per unbound channel
        per process instead of getting silently ignored. Repeating it on
        every message would be noise; once-per-process keeps the channel
        signal-to-noise high while still surfacing the misconfiguration.
        """
        if channel_id in self._refused_unbound_channels:
            return False
        self._refused_unbound_channels.add(channel_id)
        return True

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------
    def set_system_prompt(self, channel_id: int, prompt: str) -> None:
        """Store a system prompt for the given channel binding."""
        with self._lock:
            self._assert_bound(channel_id)
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
        with self._lock:
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
        with self._lock:
            self._assert_bound(channel_id)
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
        with self._lock:
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
        with self._lock:
            self._assert_bound(channel_id)
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
        with self._lock:
            self._assert_bound(channel_id)
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

        Raises:
            ValueError: if ``name`` is empty, whitespace-only, or contains
                newline/carriage-return characters (#255), or if a server
                with that name already exists on this channel (#254).
                Use :meth:`remove_mcp_server` followed by
                :meth:`add_mcp_server` to replace an existing entry.
        """
        validate_identifier(name, "MCP server name")
        with self._lock:
            self._assert_bound(channel_id)
            key = str(channel_id)
            entry = self._projects.get(key, {})
            mcps = entry.get("mcp_servers", {})
            if name in mcps:
                raise ValueError(f"MCP server {name!r} already exists.")
            mcps[name] = config
            entry["mcp_servers"] = mcps
            self._projects[key] = entry
            self._save()

    def get_mcp_servers(self, channel_id: int) -> dict:
        """Return all MCP server configs for ``channel_id``."""
        return self._projects.get(str(channel_id), {}).get("mcp_servers", {})

    def remove_mcp_server(self, channel_id: int, name: str) -> bool:
        """Remove an MCP server by name. Returns True if it existed."""
        with self._lock:
            self._assert_bound(channel_id)
            key = str(channel_id)
            entry = self._projects.get(key, {})
            mcps = entry.get("mcp_servers", {})
            if name in mcps:
                del mcps[name]
                entry["mcp_servers"] = mcps
                self._save()
                return True
            return False

    # #294: one-time migration helper. Yields ``(channel_id_int, path,
    # mcp_servers_dict)`` for every binding that has at least one legacy
    # ``mcp_servers`` entry, so ``ClaudedBot.__init__`` can write out the
    # equivalent ``.mcp.json`` for each project without any of its
    # callers reaching into ``_projects`` directly.
    def iter_mcp_bindings(self) -> list[tuple[int, str, dict]]:
        """Return snapshot of ``[(channel_id, path, mcp_servers_dict), ...]``.

        Only bindings whose entry has a non-empty ``mcp_servers`` dict AND
        a resolvable ``path`` are included. Copies the ``mcp_servers`` sub-
        dict so mutations to the returned tuples cannot corrupt the store.
        """
        out: list[tuple[int, str, dict]] = []
        with self._lock:
            for key, entry in self._projects.items():
                mcps = entry.get("mcp_servers") or {}
                path = entry.get("path")
                if not mcps or not path:
                    continue
                try:
                    cid = int(key)
                except (TypeError, ValueError):
                    continue
                out.append((cid, str(path), dict(mcps)))
        return out

    # ------------------------------------------------------------------
    # Environment variables
    # ------------------------------------------------------------------
    def set_env(self, channel_id: int, key: str, value: str) -> None:
        """Store an environment variable for the given channel binding.

        Raises:
            ValueError: if ``key`` is empty, whitespace-only, contains
                newline/carriage-return characters, or contains ``=``
                (#255). POSIX env-name semantics — a key containing ``=``
                or newlines would corrupt downstream ``.env`` files and
                shell-style environment dumps.
        """
        validate_env_key(key)
        with self._lock:
            self._assert_bound(channel_id)
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
        with self._lock:
            self._assert_bound(channel_id)
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
        with self._lock:
            if mode not in ("thread", "forum"):
                raise ValueError(f"Invalid mode: {mode!r}. Must be 'thread' or 'forum'.")
            self._assert_bound(channel_id)
            key = str(channel_id)
            entry = self._projects.setdefault(key, {})
            entry["channel_mode"] = mode
            self._save()

    def get_channel_mode(self, channel_id: int) -> str:
        """Return the channel mode ('thread' or 'forum') for ``channel_id``."""
        return self._projects.get(str(channel_id), {}).get("channel_mode", "thread")

    # ------------------------------------------------------------------
    # Mention-required toggle — v1.17 #138
    # ------------------------------------------------------------------
    def set_mention_required(self, channel_id: int, required: bool) -> None:
        """Set whether @bot mention is required for this channel to trigger.

        Stored in ``_channel_settings`` (a separate registry from
        ``_projects``) so the value survives unbind/rebind. Other
        channel-level settings (system_prompt, budget, etc.) follow the
        existing ``unbind``-wipes-all semantics — only this single
        setting is intentionally sticky.
        """
        with self._lock:
            self._assert_bound(channel_id)
            key = str(channel_id)
            settings = self._channel_settings.setdefault(key, {})
            settings["mention_required"] = bool(required)
            self._save_channel_settings()

    def get_mention_required(self, channel_id: int) -> bool:
        """Return mention-required setting; defaults to True for unset channels.

        Default True preserves the v1.1 baseline (zero regression for users
        who never touch this knob).
        """
        settings = self._channel_settings.get(str(channel_id))
        if settings is None:
            return True
        return settings.get("mention_required", True)

    def _load_channel_settings(self) -> None:
        p = os.path.join(self.data_dir, "channel_settings.json")
        if not os.path.isfile(p):
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._channel_settings = data
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Failed to load channel_settings.json: %s — starting empty", exc)
            self._channel_settings = {}

    def _save_channel_settings(self) -> None:
        # #252: same shared atomic-write helper as ``_save``, distinct
        # file path. Shares the instance RLock with ``_save`` — both
        # dicts are protected by one lock; ``channel_settings.json`` is
        # tiny so the shared lock is not a contention concern.
        p = Path(self.data_dir) / "channel_settings.json"
        atomic_write_json(p, self._channel_settings, self._lock, sort_keys=True)

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
        with self._lock:
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
        with self._lock:
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
        p = Path(self.data_dir) / "guild_roots.json"
        atomic_write_json(p, self._guild_roots, self._lock, sort_keys=True)
