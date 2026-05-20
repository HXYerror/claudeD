"""Manage custom Claude agent definitions with JSON persistence."""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from ._json_store import atomic_write_json

log = logging.getLogger("clauded.agent_manager")


class AgentManager:
    """Persisted store of named agent definitions (name → prompt + description)."""

    def __init__(self, data_dir: str = "data") -> None:
        self._dir = Path(data_dir)
        self._path = self._dir / "agents.json"
        self._agents: dict[str, dict] = {}  # {name: {"description": str, "prompt": str}}
        # #252: RLock so create/delete can hold across read-modify-write.
        self._lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                self._agents = json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("Failed to load agents.json")
            self._agents = {}

    def _save(self) -> None:
        atomic_write_json(self._path, self._agents, self._lock)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def create(self, name: str, prompt: str, description: str = "") -> None:
        """Create or overwrite an agent definition."""
        with self._lock:
            self._agents[name] = {
                "prompt": prompt,
                "description": description or f"Custom agent: {name}",
            }
            self._save()

    def delete(self, name: str) -> bool:
        """Delete an agent by name. Returns True if it existed."""
        with self._lock:
            if name in self._agents:
                del self._agents[name]
                self._save()
                return True
            return False

    def get(self, name: str) -> dict | None:
        """Return the agent definition dict, or None."""
        return self._agents.get(name)

    def list_all(self) -> dict[str, dict]:
        """Return a shallow copy of all agent definitions."""
        return dict(self._agents)


__all__ = ["AgentManager"]
