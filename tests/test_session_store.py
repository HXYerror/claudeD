"""Tests for :class:`SessionStore`."""

from __future__ import annotations

import os
import tempfile

import pytest

from clauded.session_store import SessionStore


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Return a fresh temp directory to use as SessionStore data_dir."""
    return str(tmp_path / "store_data")


def test_save_and_get(tmp_data_dir: str) -> None:
    store = SessionStore(data_dir=tmp_data_dir)
    store.save_session(123, "sess-abc", "/tmp/project", model="sonnet", system_prompt="be helpful")
    info = store.get_session_info(123)
    assert info is not None
    assert info["session_id"] == "sess-abc"
    assert info["project_path"] == "/tmp/project"
    assert info["model"] == "sonnet"
    assert info["system_prompt"] == "be helpful"
    assert "last_active" in info


def test_remove(tmp_data_dir: str) -> None:
    store = SessionStore(data_dir=tmp_data_dir)
    store.save_session(456, "sess-def", "/tmp/p2")
    assert store.get_session_info(456) is not None
    store.remove_session(456)
    assert store.get_session_info(456) is None


def test_persistence(tmp_data_dir: str) -> None:
    store1 = SessionStore(data_dir=tmp_data_dir)
    store1.save_session(789, "sess-ghi", "/tmp/p3", model="opus")

    # Create a new instance pointing at the same directory
    store2 = SessionStore(data_dir=tmp_data_dir)
    info = store2.get_session_info(789)
    assert info is not None
    assert info["session_id"] == "sess-ghi"
    assert info["model"] == "opus"


def test_list_all(tmp_data_dir: str) -> None:
    store = SessionStore(data_dir=tmp_data_dir)
    store.save_session(1, "s1", "/p1")
    store.save_session(2, "s2", "/p2")
    store.save_session(3, "s3", "/p3")
    all_sessions = store.list_all()
    assert len(all_sessions) == 3
    assert "1" in all_sessions
    assert "2" in all_sessions
    assert "3" in all_sessions


def test_missing_returns_none(tmp_data_dir: str) -> None:
    store = SessionStore(data_dir=tmp_data_dir)
    assert store.get_session_info(999) is None
