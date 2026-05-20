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
        self._channels: dict[str, dict] = {}  # {channel_id_str: {"total_usd": float, "billable_calls": int, "total_turns": int}}
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
            # #248: migrate old "calls" schema → billable_calls + total_turns
            for _k, _v in self._channels.items():
                if "calls" in _v and "billable_calls" not in _v:
                    _v["billable_calls"] = _v.pop("calls")
                    _v.setdefault("total_turns", _v["billable_calls"])
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
        """Record a turn. Always increments total_turns; only increments
        billable_calls when cost_usd > 0.  (#248)"""
        key = str(channel_id)
        entry = self._channels.setdefault(
            key, {"total_usd": 0.0, "billable_calls": 0, "total_turns": 0},
        )
        entry["total_usd"] += cost_usd
        entry["total_turns"] += 1
        if cost_usd > 0:
            entry["billable_calls"] += 1
        self._global_total += cost_usd
        self._save()

    def get_channel_cost(self, channel_id: int) -> tuple[float, int, int]:
        """Return (total_usd, billable_calls, total_turns) for a channel."""
        entry = self._channels.get(
            str(channel_id),
            {"total_usd": 0.0, "billable_calls": 0, "total_turns": 0},
        )
        return (
            entry["total_usd"],
            entry.get("billable_calls", entry.get("calls", 0)),
            entry.get("total_turns", entry.get("calls", 0)),
        )

    def get_total_cost(self) -> float:
        return self._global_total

    def get_total_stats(self) -> tuple[float, int, int]:
        """Return (total_usd, total_billable, total_turns) across all channels."""
        billable = sum(e.get("billable_calls", 0) for e in self._channels.values())
        turns = sum(e.get("total_turns", 0) for e in self._channels.values())
        return self._global_total, billable, turns

    def reset_channel(self, channel_id: int) -> None:
        key = str(channel_id)
        if key in self._channels:
            self._global_total -= self._channels[key]["total_usd"]
            del self._channels[key]
            self._save()
