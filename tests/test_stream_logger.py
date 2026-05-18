"""#223 PR-A — stream_logger overhaul.

Tests for the runtime-flippable enable, path migration, generalized
log_event signature, and rotation behavior.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from clauded import stream_logger


@pytest.fixture(autouse=True)
def _reset():
    stream_logger._reset_for_tests()
    yield
    stream_logger._reset_for_tests()


# ---------------------------------------------------------------------------
# is_enabled / set_enabled
# ---------------------------------------------------------------------------


def test_is_enabled_default_env_off(monkeypatch):
    monkeypatch.delenv("CLAUDED_STREAM_DEBUG", raising=False)
    assert stream_logger.is_enabled() is False


def test_is_enabled_env_truthy(monkeypatch):
    monkeypatch.setenv("CLAUDED_STREAM_DEBUG", "1")
    assert stream_logger.is_enabled() is True


@pytest.mark.parametrize("val", ["true", "yes", "1"])
def test_is_enabled_env_alternative_truthy(monkeypatch, val):
    monkeypatch.setenv("CLAUDED_STREAM_DEBUG", val)
    assert stream_logger.is_enabled() is True


def test_set_enabled_true_wins_over_env_false(monkeypatch):
    monkeypatch.delenv("CLAUDED_STREAM_DEBUG", raising=False)
    stream_logger.set_enabled(True)
    assert stream_logger.is_enabled() is True


def test_set_enabled_false_wins_over_env_true(monkeypatch):
    monkeypatch.setenv("CLAUDED_STREAM_DEBUG", "1")
    stream_logger.set_enabled(False)
    assert stream_logger.is_enabled() is False


def test_set_enabled_none_reverts_to_env(monkeypatch):
    monkeypatch.setenv("CLAUDED_STREAM_DEBUG", "1")
    stream_logger.set_enabled(False)
    assert stream_logger.is_enabled() is False
    stream_logger.set_enabled(None)
    assert stream_logger.is_enabled() is True


# ---------------------------------------------------------------------------
# Path migration
# ---------------------------------------------------------------------------


def test_log_path_under_library_logs():
    """#223 D2: jsonl moved from logs/ (cwd) to ~/Library/Logs/clauded/."""
    p = stream_logger._LOG_PATH
    assert p.name == "stream-debug.jsonl"
    assert "Library/Logs/clauded" in str(p) or p.parent.name == "clauded", (
        f"#223 D2: stream-debug.jsonl should live under ~/Library/Logs/clauded; got {p}"
    )


# ---------------------------------------------------------------------------
# log_event — dict pass-through (new in #223)
# ---------------------------------------------------------------------------


def test_log_event_disabled_writes_nothing(tmp_path, monkeypatch):
    """When is_enabled() is False, log_event is a no-op."""
    monkeypatch.setattr(stream_logger, "_LOG_PATH", tmp_path / "x.jsonl")
    monkeypatch.setattr(stream_logger, "_LOG_DIR", tmp_path)
    stream_logger.set_enabled(False)
    stream_logger.log_event({"type": "ControlPlane", "method": "get_context_usage"})
    assert not (tmp_path / "x.jsonl").exists()


def test_log_event_dict_payload_passes_through(tmp_path, monkeypatch):
    """#223: log_event accepts plain dicts (post-#223 control-plane / retry / crash)."""
    monkeypatch.setattr(stream_logger, "_LOG_PATH", tmp_path / "x.jsonl")
    monkeypatch.setattr(stream_logger, "_LOG_DIR", tmp_path)
    stream_logger.set_enabled(True)
    stream_logger.log_event({
        "type": "ControlPlane",
        "method": "get_context_usage",
        "result_pct": 47,
    })
    lines = (tmp_path / "x.jsonl").read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["type"] == "ControlPlane"
    assert entry["method"] == "get_context_usage"
    assert entry["result_pct"] == 47
    assert "ts" in entry
    assert isinstance(entry["ts"], (int, float))
    # #223 R1 architect: every event carries schema version v=1 for #224 epic.
    assert entry["v"] == 1


def test_log_event_dict_with_extra_merge(tmp_path, monkeypatch):
    """``extra`` kwarg merges over the base event dict."""
    monkeypatch.setattr(stream_logger, "_LOG_PATH", tmp_path / "x.jsonl")
    monkeypatch.setattr(stream_logger, "_LOG_DIR", tmp_path)
    stream_logger.set_enabled(True)
    stream_logger.log_event({"type": "Crash"}, extra={"thread_id": 42})
    entry = json.loads((tmp_path / "x.jsonl").read_text().splitlines()[0])
    assert entry["type"] == "Crash"
    assert entry["thread_id"] == 42


# ---------------------------------------------------------------------------
# log_event — legacy SDK message types still work (regression)
# ---------------------------------------------------------------------------


def test_log_event_unknown_object_does_not_crash(tmp_path, monkeypatch):
    """Legacy path: arbitrary object gets minimal envelope, no exception."""
    monkeypatch.setattr(stream_logger, "_LOG_PATH", tmp_path / "x.jsonl")
    monkeypatch.setattr(stream_logger, "_LOG_DIR", tmp_path)
    stream_logger.set_enabled(True)

    class Bogus:
        pass

    stream_logger.log_event(Bogus(), buffer_len=99)
    entry = json.loads((tmp_path / "x.jsonl").read_text().splitlines()[0])
    assert entry["type"] == "Bogus"
    assert entry["buffer_len"] == 99
    # #223 R1 architect: legacy path also stamps v=1
    assert entry["v"] == 1


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def test_rotation_triggers_at_max_bytes(tmp_path, monkeypatch):
    """When file exceeds _MAX_BYTES, rotation moves it to .1 suffix."""
    log_path = tmp_path / "x.jsonl"
    log_path.write_text("x" * 100)  # pre-existing content
    monkeypatch.setattr(stream_logger, "_LOG_PATH", log_path)
    monkeypatch.setattr(stream_logger, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(stream_logger, "_MAX_BYTES", 10)  # force rotation
    stream_logger.set_enabled(True)
    stream_logger.log_event({"type": "test"})
    # After rotation .1 should exist and main file should be small (only new line)
    rotated = log_path.with_suffix(".jsonl.1")
    assert rotated.exists(), (
        f"#223 AC6: file > _MAX_BYTES should rotate to .1; "
        f"contents of dir: {list(tmp_path.iterdir())}"
    )
    # New file contains only the post-rotation event
    new_content = log_path.read_text()
    assert "test" in new_content
    assert len(new_content) < 200


def test_no_rotation_below_max_bytes(tmp_path, monkeypatch):
    """Under threshold, file just grows."""
    log_path = tmp_path / "x.jsonl"
    monkeypatch.setattr(stream_logger, "_LOG_PATH", log_path)
    monkeypatch.setattr(stream_logger, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(stream_logger, "_MAX_BYTES", 10_000_000)
    stream_logger.set_enabled(True)
    stream_logger.log_event({"type": "a"})
    stream_logger.log_event({"type": "b"})
    rotated = log_path.with_suffix(".jsonl.1")
    assert not rotated.exists()
    lines = log_path.read_text().splitlines()
    assert len(lines) == 2
