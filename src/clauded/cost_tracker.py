"""Per-channel and global cost tracking with JSON persistence."""

from __future__ import annotations
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("clauded.cost_tracker")


class CostTracker:
    """Track API costs per channel and globally."""

    def __init__(self, data_dir: str = "data") -> None:
        self._dir = Path(data_dir)
        self._path = self._dir / "costs.json"
        self._channels: dict[str, dict] = {}  # {channel_id_str: {"total_usd": float, "calls": int}}
        self._global_total: float = 0.0
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            self._channels = data.get("channels", {})
            self._global_total = data.get("global_total_usd", 0.0)
        except (json.JSONDecodeError, OSError):
            log.warning("Failed to load costs.json, starting fresh")

    def _save(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"channels": self._channels, "global_total_usd": self._global_total}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    def record(self, channel_id: int, cost_usd: float) -> None:
        key = str(channel_id)
        entry = self._channels.setdefault(key, {"total_usd": 0.0, "calls": 0})
        entry["total_usd"] += cost_usd
        entry["calls"] += 1
        self._global_total += cost_usd
        self._save()

    def get_channel_cost(self, channel_id: int) -> tuple[float, int]:
        entry = self._channels.get(str(channel_id), {"total_usd": 0.0, "calls": 0})
        return entry["total_usd"], entry["calls"]

    def get_total_cost(self) -> float:
        return self._global_total

    def reset_channel(self, channel_id: int) -> None:
        key = str(channel_id)
        if key in self._channels:
            self._global_total -= self._channels[key]["total_usd"]
            del self._channels[key]
            self._save()
