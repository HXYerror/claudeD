"""Channel ↔ project-directory bindings, persisted to JSON.

A `ProjectManager` maps Discord channel IDs to absolute filesystem paths.
State is loaded from and saved to ``<data_dir>/projects.json``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict

log = logging.getLogger("clauded.project_manager")


class ProjectManager:
    """Persisted store of channel-id → project-directory bindings."""

    def __init__(self, data_dir: str = "data") -> None:
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "projects.json")
        self._projects: Dict[str, Dict[str, str]] = {}
        self._load()

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
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._projects, f, indent=2, sort_keys=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def bind(self, channel_id: int, path: str) -> str:
        """Bind ``channel_id`` to ``path``.

        Expands ``~`` and validates the directory exists. Returns the
        absolute, expanded path that was actually stored.

        Raises:
            ValueError: if the path does not point to an existing directory.
        """
        expanded = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(expanded):
            raise ValueError(f"Not a directory: {expanded}")

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
        return entry["path"] if entry else None

    # Alias used by callers that prefer the "path" terminology.
    def get_path(self, channel_id: int) -> str | None:
        """Alias of :meth:`get_project`."""
        return self.get_project(channel_id)

    def is_bound(self, channel_id: int) -> bool:
        """Return True if ``channel_id`` has a binding."""
        return str(channel_id) in self._projects
