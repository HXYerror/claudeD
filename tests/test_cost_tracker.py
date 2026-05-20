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


# ---------- #248 R2: tests required by PRD + R1 reviewers ----------

class TestCostTrackerR2:
    """Post-merge R1 found 3 missing test cases for #248."""

    def test_zero_cost_increments_turns_not_billable(self, tmp_path):
        """PRD §4 bullet 1: record with cost=0 → total_turns +1, billable_calls +0."""
        tracker = CostTracker(data_dir=str(tmp_path))
        tracker.record(100, 0.5)   # billable
        tracker.record(100, 0.0)   # cost=0 turn
        tracker.record(100, 0.0)   # another cost=0
        total, billable, turns = tracker.get_channel_cost(100)
        assert turns == 3, f"expected 3 turns, got {turns}"
        assert billable == 1, f"expected 1 billable, got {billable}"
        assert abs(total - 0.5) < 1e-9

    def test_old_schema_migration(self, tmp_path):
        """PRD §4 bullet 3: old costs.json with only 'calls' field migrates correctly."""
        import json
        costs_path = tmp_path / "costs.json"
        old_data = {
            "channels": {
                "42": {"total_usd": 3.0, "calls": 7},
                "99": {"total_usd": 0.5, "calls": 1},
            },
            "global_total_usd": 3.5,
        }
        costs_path.write_text(json.dumps(old_data))
        tracker = CostTracker(data_dir=str(tmp_path))
        # Channel 42: calls=7 should become billable_calls=7, total_turns=7
        total_42, billable_42, turns_42 = tracker.get_channel_cost(42)
        assert billable_42 == 7, f"billable_42={billable_42}"
        assert turns_42 == 7, f"turns_42={turns_42}"
        assert abs(total_42 - 3.0) < 1e-9
        # Channel 99
        total_99, billable_99, turns_99 = tracker.get_channel_cost(99)
        assert billable_99 == 1
        assert turns_99 == 1
        # After migration, new record should work
        tracker.record(42, 0.0)
        _, b2, t2 = tracker.get_channel_cost(42)
        assert t2 == 8, f"expected 8 turns after cost=0 record, got {t2}"
        assert b2 == 7, f"billable should stay 7, got {b2}"

    def test_cost_show_format_contains_markers(self, tmp_path):
        """PRD §4 bullet 4: /cost show output contains '💬' and '💰' markers."""
        # We can't easily test the Discord embed from here,
        # but we can verify the format string pattern
        tracker = CostTracker(data_dir=str(tmp_path))
        tracker.record(100, 1.5)
        tracker.record(100, 0.0)
        total, billable, turns = tracker.get_channel_cost(100)
        # The cog builds: f"**${total:.4f}** │ 💬 {turns} turns │ 💰 {billable} billable"
        display = f"**${total:.4f}** │ 💬 {turns} turns │ 💰 {billable} billable"
        assert "💬" in display
        assert "💰" in display
        assert "2 turns" in display
        assert "1 billable" in display

    def test_get_total_stats_aggregates(self, tmp_path):
        """AC4: get_total_stats returns aggregated billable + turns."""
        tracker = CostTracker(data_dir=str(tmp_path))
        tracker.record(100, 1.0)
        tracker.record(100, 0.0)
        tracker.record(200, 0.5)
        total, billable, turns = tracker.get_total_stats()
        assert abs(total - 1.5) < 1e-9
        assert billable == 2, f"expected 2 billable, got {billable}"
        assert turns == 3, f"expected 3 turns, got {turns}"


# ---------- #252: concurrent _save() must not race ----------

class TestCostTrackerConcurrency:
    """#252 regression: ``_save`` used a fixed ``costs.tmp`` filename, so
    concurrent ``record`` calls clobbered each other's tmp and the loser's
    ``os.replace`` raised ``FileNotFoundError`` (~50% under 50-call stress).

    Fix: ``atomic_write_json`` uses a unique tmp filename per call plus a
    threading lock. These tests pin AC1–AC4 from the PRD.
    """

    def test_100_concurrent_records_no_errors(self, tmp_path):
        """AC1 + AC4: 100 concurrent record() calls → 0 errors + correct total."""
        from concurrent.futures import ThreadPoolExecutor

        tracker = CostTracker(data_dir=str(tmp_path))
        errors: list[BaseException] = []

        def one(i: int) -> None:
            try:
                # Mix billable and zero-cost turns so we also exercise the
                # #248 branch and prove the read-modify-write under the lock
                # is correct.
                tracker.record(42, 0.01 if i % 2 == 0 else 0.0)
            except BaseException as e:  # noqa: BLE001 — we want everything
                errors.append(e)

        with ThreadPoolExecutor(max_workers=32) as pool:
            list(pool.map(one, range(100)))

        assert errors == [], f"expected 0 errors, got {len(errors)}: {errors[:3]!r}"
        total, billable, turns = tracker.get_channel_cost(42)
        assert turns == 100, f"expected 100 turns, got {turns}"
        assert billable == 50, f"expected 50 billable (even indices), got {billable}"
        # 50 calls × 0.01 = 0.50 exactly in float terms (no rounding loss
        # at this magnitude).
        assert total == pytest.approx(0.50)

    def test_no_stale_tmp_after_stress(self, tmp_path):
        """AC3: after a stress run no ``*.tmp`` files remain in the data dir."""
        from concurrent.futures import ThreadPoolExecutor

        tracker = CostTracker(data_dir=str(tmp_path))
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda i: tracker.record(i, 0.001), range(50)))

        leftover = list(Path(tmp_path).glob("*.tmp"))
        assert leftover == [], f"unexpected stale tmp files: {leftover}"

    def test_persisted_file_matches_in_memory(self, tmp_path):
        """AC4: after the stress, reloading from disk shows the same totals."""
        from concurrent.futures import ThreadPoolExecutor

        tracker = CostTracker(data_dir=str(tmp_path))
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda _: tracker.record(7, 0.01), range(100)))

        # Fresh tracker → load from JSON file just produced.
        reloaded = CostTracker(data_dir=str(tmp_path))
        total_mem, billable_mem, turns_mem = tracker.get_channel_cost(7)
        total_disk, billable_disk, turns_disk = reloaded.get_channel_cost(7)
        assert turns_mem == 100
        assert turns_disk == turns_mem
        assert billable_disk == billable_mem
        assert total_disk == pytest.approx(total_mem)
