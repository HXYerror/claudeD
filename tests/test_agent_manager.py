"""Unit tests for :class:`AgentManager`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from clauded.agent_manager import AgentManager


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def manager(data_dir: Path) -> AgentManager:
    return AgentManager(data_dir=str(data_dir))


# ---------------------------------------------------------------------------
# create / get
# ---------------------------------------------------------------------------


def test_create_and_get(manager: AgentManager) -> None:
    manager.create("reviewer", "Review code carefully", "Code reviewer agent")
    agent = manager.get("reviewer")
    assert agent is not None
    assert agent["prompt"] == "Review code carefully"
    assert agent["description"] == "Code reviewer agent"


def test_create_default_description(manager: AgentManager) -> None:
    manager.create("helper", "Help with things")
    agent = manager.get("helper")
    assert agent is not None
    assert agent["description"] == "Custom agent: helper"


def test_create_rejects_duplicate(manager: AgentManager) -> None:
    """#254 — duplicate ``create`` raises instead of silently overwriting."""
    manager.create("a", "prompt1")
    with pytest.raises(ValueError, match="already exists"):
        manager.create("a", "prompt2", "updated")
    # Original definition is preserved.
    agent = manager.get("a")
    assert agent["prompt"] == "prompt1"


def test_create_after_delete_succeeds(manager: AgentManager) -> None:
    """#254 — delete-then-create is the supported replace flow."""
    manager.create("a", "prompt1")
    assert manager.delete("a") is True
    manager.create("a", "prompt2", "updated")
    agent = manager.get("a")
    assert agent["prompt"] == "prompt2"
    assert agent["description"] == "updated"


@pytest.mark.parametrize(
    "bad_name",
    ["", "   ", "\t", "\n", "name\nwith\nnewline", "name\rwith\rcr"],
)
def test_create_rejects_invalid_name(manager: AgentManager, bad_name: str) -> None:
    """#255 — empty/whitespace-only/newline-containing names are rejected."""
    with pytest.raises(ValueError, match="empty|whitespace|newline"):
        manager.create(bad_name, "prompt")
    # And nothing was written.
    assert bad_name not in manager.list_all()


def test_get_nonexistent(manager: AgentManager) -> None:
    assert manager.get("ghost") is None


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_existing(manager: AgentManager) -> None:
    manager.create("x", "p")
    assert manager.delete("x") is True
    assert manager.get("x") is None


def test_delete_nonexistent(manager: AgentManager) -> None:
    assert manager.delete("nope") is False


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------


def test_list_all_empty(manager: AgentManager) -> None:
    assert manager.list_all() == {}


def test_list_all_returns_copy(manager: AgentManager) -> None:
    manager.create("a", "p1")
    manager.create("b", "p2")
    result = manager.list_all()
    assert set(result.keys()) == {"a", "b"}
    # Mutating the returned dict doesn't affect internal state
    result["c"] = {"prompt": "p3", "description": "d"}
    assert "c" not in manager.list_all()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_persistence_round_trip(data_dir: Path) -> None:
    am1 = AgentManager(data_dir=str(data_dir))
    am1.create("reviewer", "Be strict", "Strict reviewer")

    am2 = AgentManager(data_dir=str(data_dir))
    agent = am2.get("reviewer")
    assert agent is not None
    assert agent["prompt"] == "Be strict"
    assert agent["description"] == "Strict reviewer"


def test_persistence_writes_json(manager: AgentManager, data_dir: Path) -> None:
    manager.create("test", "prompt text")
    payload = json.loads((data_dir / "agents.json").read_text())
    assert "test" in payload
    assert payload["test"]["prompt"] == "prompt text"


def test_corrupt_json_handled(data_dir: Path) -> None:
    (data_dir / "agents.json").write_text("{not valid")
    am = AgentManager(data_dir=str(data_dir))
    assert am.list_all() == {}


def test_no_file_ok(tmp_path: Path) -> None:
    am = AgentManager(data_dir=str(tmp_path / "fresh"))
    assert am.list_all() == {}
