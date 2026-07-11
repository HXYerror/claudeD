"""Tests for :class:`SessionStore`."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from clauded.session_store import SessionStore


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Return a fresh temp directory to use as SessionStore data_dir."""
    return str(tmp_path / "store_data")


def test_save_and_get(tmp_data_dir: str) -> None:
    store = SessionStore(data_dir=tmp_data_dir)
    store.save_session(123, "sess-abc", permission_mode_override="plan")
    info = store.get_session_info(123)
    assert info is not None
    assert info["session_id"] == "sess-abc"
    assert info["permission_mode_override"] == "plan"
    assert "last_active" in info
    # #295: shadow fields must NOT be persisted (canonical source is
    # ProjectManager / already-deprecated).
    assert "project_path" not in info
    assert "model" not in info
    assert "system_prompt" not in info


def test_remove(tmp_data_dir: str) -> None:
    store = SessionStore(data_dir=tmp_data_dir)
    store.save_session(456, "sess-def")
    assert store.get_session_info(456) is not None
    store.remove_session(456)
    assert store.get_session_info(456) is None


def test_persistence(tmp_data_dir: str) -> None:
    store1 = SessionStore(data_dir=tmp_data_dir)
    store1.save_session(789, "sess-ghi", permission_mode_override="acceptEdits")

    # Create a new instance pointing at the same directory
    store2 = SessionStore(data_dir=tmp_data_dir)
    info = store2.get_session_info(789)
    assert info is not None
    assert info["session_id"] == "sess-ghi"
    assert info["permission_mode_override"] == "acceptEdits"


def test_list_all(tmp_data_dir: str) -> None:
    store = SessionStore(data_dir=tmp_data_dir)
    store.save_session(1, "s1")
    store.save_session(2, "s2")
    store.save_session(3, "s3")
    all_sessions = store.list_all()
    assert len(all_sessions) == 3
    assert "1" in all_sessions
    assert "2" in all_sessions
    assert "3" in all_sessions


def test_missing_returns_none(tmp_data_dir: str) -> None:
    store = SessionStore(data_dir=tmp_data_dir)
    assert store.get_session_info(999) is None


def test_load_strips_legacy_shadow_fields(tmp_path: Path) -> None:
    """#295: on load, ``model`` / ``system_prompt`` / ``project_path``
    should be stripped from every entry and the migration should
    rewrite ``sessions.json`` in place.

    Preserves: ``session_id``, ``last_active``, ``permission_mode_override``
    (and any future unknown fields — we only drop the 3 named shadows).
    """
    data_dir = tmp_path / "d"
    data_dir.mkdir()
    legacy = {
        "1": {
            "session_id": "s1",
            "project_path": "/tmp/legacy",
            "model": "sonnet",
            "system_prompt": "hello",
            "permission_mode_override": "plan",
            "last_active": "2024-01-01T00:00:00+00:00",
            "future_field": "keep-me",
        },
        "2": {
            "session_id": "s2",
            # older schema: missing permission_mode_override
            "project_path": "/tmp/older",
            "model": None,
            "system_prompt": "",
            "last_active": "x",
        },
    }
    (data_dir / "sessions.json").write_text(json.dumps(legacy))

    store = SessionStore(data_dir=str(data_dir))

    info1 = store.get_session_info(1)
    assert info1 is not None
    assert info1["session_id"] == "s1"
    assert info1["permission_mode_override"] == "plan"
    assert info1["last_active"] == "2024-01-01T00:00:00+00:00"
    assert info1["future_field"] == "keep-me"
    # Shadow fields dropped
    assert "project_path" not in info1
    assert "model" not in info1
    assert "system_prompt" not in info1

    info2 = store.get_session_info(2)
    assert info2 is not None
    assert info2["session_id"] == "s2"
    assert "project_path" not in info2
    assert "model" not in info2
    assert "system_prompt" not in info2
    # Legacy row without permission_mode_override stays field-less
    assert info2.get("permission_mode_override") is None

    # Migration wrote back to disk — reloading confirms.
    on_disk = json.loads((data_dir / "sessions.json").read_text())
    assert "project_path" not in on_disk["1"]
    assert "model" not in on_disk["1"]
    assert "system_prompt" not in on_disk["1"]
    assert on_disk["1"]["future_field"] == "keep-me"


def test_load_no_migration_when_clean(tmp_path: Path) -> None:
    """Startup should NOT touch the file if there are no shadow fields
    to strip (avoid pointless atomic-write churn on every boot)."""
    data_dir = tmp_path / "d"
    data_dir.mkdir()
    clean = {
        "1": {
            "session_id": "s1",
            "permission_mode_override": None,
            "last_active": "2024-01-01T00:00:00+00:00",
        }
    }
    path = data_dir / "sessions.json"
    path.write_text(json.dumps(clean))
    mtime_before = path.stat().st_mtime_ns

    # Sleep-free: SessionStore._save uses atomic_write_json which
    # rewrites the file; if migration didn't fire, mtime is unchanged.
    SessionStore(data_dir=str(data_dir))
    mtime_after = path.stat().st_mtime_ns
    assert mtime_before == mtime_after, (
        "clean sessions.json should not be rewritten on startup"
    )
