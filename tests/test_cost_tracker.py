"""Tests for the CostTracker module."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from clauded.cost_tracker import CostTracker


@pytest.fixture
def data_dir(tmp_path: Path) -> str:
    """Return a fresh temporary directory for cost data."""
    return str(tmp_path / "cost_data")


class TestCostTracker:
    def test_record_and_get(self, data_dir: str) -> None:
        tracker = CostTracker(data_dir=data_dir)
        tracker.record(123, 0.05)
        total, billable, turns = tracker.get_channel_cost(123)
        assert total == pytest.approx(0.05)
        assert billable == 1

        tracker.record(123, 0.03)
        total, billable, turns = tracker.get_channel_cost(123)
        assert total == pytest.approx(0.08)
        assert billable == 2

    def test_global_total(self, data_dir: str) -> None:
        tracker = CostTracker(data_dir=data_dir)
        tracker.record(100, 0.10)
        tracker.record(200, 0.20)
        tracker.record(100, 0.05)
        assert tracker.get_total_cost() == pytest.approx(0.35)

        # Per-channel checks
        total_100, billable_100, turns_100 = tracker.get_channel_cost(100)
        assert total_100 == pytest.approx(0.15)
        assert billable_100 == 2

        total_200, billable_200, turns_200 = tracker.get_channel_cost(200)
        assert total_200 == pytest.approx(0.20)
        assert billable_200 == 1

    def test_reset_channel(self, data_dir: str) -> None:
        tracker = CostTracker(data_dir=data_dir)
        tracker.record(100, 0.10)
        tracker.record(200, 0.20)
        assert tracker.get_total_cost() == pytest.approx(0.30)

        tracker.reset_channel(100)
        total, billable, turns = tracker.get_channel_cost(100)
        assert total == pytest.approx(0.0)
        assert billable == 0
        assert tracker.get_total_cost() == pytest.approx(0.20)

        # Reset non-existent channel is a no-op
        tracker.reset_channel(999)
        assert tracker.get_total_cost() == pytest.approx(0.20)

    def test_persistence(self, data_dir: str) -> None:
        tracker1 = CostTracker(data_dir=data_dir)
        tracker1.record(42, 0.12)
        tracker1.record(42, 0.08)
        tracker1.record(99, 0.05)

        # Reload from same directory
        tracker2 = CostTracker(data_dir=data_dir)
        total_42, billable_42, turns_42 = tracker2.get_channel_cost(42)
        assert total_42 == pytest.approx(0.20)
        assert billable_42 == 2

        total_99, billable_99, turns_99 = tracker2.get_channel_cost(99)
        assert total_99 == pytest.approx(0.05)
        assert billable_99 == 1

        assert tracker2.get_total_cost() == pytest.approx(0.25)

    def test_empty_channel(self, data_dir: str) -> None:
        tracker = CostTracker(data_dir=data_dir)
        total, billable, turns = tracker.get_channel_cost(999)
        assert total == pytest.approx(0.0)
        assert billable == 0

    def test_corrupted_file(self, data_dir: str) -> None:
        """Tracker recovers gracefully from a corrupted costs.json."""
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        (path / "costs.json").write_text("not valid json {{{")

        tracker = CostTracker(data_dir=data_dir)
        assert tracker.get_total_cost() == pytest.approx(0.0)

        # Can still record after recovery
        tracker.record(1, 0.01)
        assert tracker.get_total_cost() == pytest.approx(0.01)

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        """Data directory is created on first record if it doesn't exist."""
        nested = str(tmp_path / "a" / "b" / "c")
        tracker = CostTracker(data_dir=nested)
        tracker.record(1, 0.01)
        assert (Path(nested) / "costs.json").exists()
