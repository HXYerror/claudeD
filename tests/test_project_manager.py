"""Unit tests for ``ProjectManager`` (bind / unbind / persistence)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from clauded.project_manager import ProjectManager


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    """Provide a sandbox root under which all bindings must live."""
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Where ProjectManager writes its projects.json."""
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def manager(data_dir: Path, projects_root: Path) -> ProjectManager:
    return ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))


# ---------------------------------------------------------------------------
# bind()
# ---------------------------------------------------------------------------


def test_bind_valid_path_saves_and_returns_resolved_path(
    manager: ProjectManager, projects_root: Path
) -> None:
    proj = projects_root / "myproj"
    proj.mkdir()
    stored = manager.bind(123, str(proj))
    assert Path(stored) == proj.resolve()
    assert manager.get_project(123) == str(proj.resolve())
    assert manager.is_bound(123) is True


def test_bind_nonexistent_path_raises(
    manager: ProjectManager, projects_root: Path
) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        manager.bind(1, str(projects_root / "ghost"))


def test_bind_path_outside_projects_root_raises(
    manager: ProjectManager, tmp_path: Path
) -> None:
    """A real, existing directory outside ``projects_root`` is rejected."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(ValueError, match="outside the allowed projects root"):
        manager.bind(1, str(elsewhere))


def test_bind_rejects_dotdot_segments(
    manager: ProjectManager, projects_root: Path
) -> None:
    """Raw `..` traversal is rejected even before path resolution."""
    with pytest.raises(ValueError, match=r"\.\."):
        manager.bind(1, str(projects_root / ".." / "etc"))


def test_bind_empty_string_raises(manager: ProjectManager) -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        manager.bind(1, "")


def test_bind_path_to_file_raises(
    manager: ProjectManager, projects_root: Path
) -> None:
    f = projects_root / "afile"
    f.write_text("hi")
    with pytest.raises(ValueError, match="Not a directory"):
        manager.bind(1, str(f))


def test_bind_expands_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``~`` is expanded relative to ``$HOME``."""
    home = tmp_path / "home"
    home.mkdir()
    proj = home / "proj"
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # Use the (now monkeypatched) home dir as both root and binding target.
    pm = ProjectManager(data_dir=str(tmp_path / "data"), projects_root=str(home))
    stored = pm.bind(42, "~/proj")
    assert Path(stored) == proj.resolve()


# ---------------------------------------------------------------------------
# unbind() / get_project() / is_bound()
# ---------------------------------------------------------------------------


def test_unbind_returns_true_for_existing(
    manager: ProjectManager, projects_root: Path
) -> None:
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(7, str(proj))
    assert manager.unbind(7) is True
    assert manager.is_bound(7) is False
    assert manager.get_project(7) is None


def test_unbind_returns_false_for_missing(manager: ProjectManager) -> None:
    assert manager.unbind(999) is False


def test_get_project_returns_none_when_unbound(manager: ProjectManager) -> None:
    assert manager.get_project(404) is None


def test_get_path_alias_matches_get_project(
    manager: ProjectManager, projects_root: Path
) -> None:
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(1, str(proj))
    assert manager.get_path(1) == manager.get_project(1)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_persistence_round_trip(
    data_dir: Path, projects_root: Path
) -> None:
    """Bindings written by one manager are visible to a fresh one."""
    proj = projects_root / "p"
    proj.mkdir()

    pm1 = ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))
    pm1.bind(123, str(proj))

    pm2 = ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))
    assert pm2.is_bound(123) is True
    assert pm2.get_project(123) == str(proj.resolve())


def test_persistence_writes_json_file(
    manager: ProjectManager, data_dir: Path, projects_root: Path
) -> None:
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(5, str(proj))
    payload = json.loads((data_dir / "projects.json").read_text())
    assert "5" in payload
    assert payload["5"]["path"] == str(proj.resolve())
    assert "bound_at" in payload["5"]


def test_corrupt_json_handled_gracefully(
    data_dir: Path, projects_root: Path
) -> None:
    """A malformed projects.json yields an empty manager rather than a crash."""
    (data_dir / "projects.json").write_text("{not valid json")
    pm = ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))
    assert pm.is_bound(1) is False
    assert pm.get_project(1) is None


def test_load_skipped_when_no_file(tmp_path: Path) -> None:
    """Constructing against a fresh data dir is a no-op for state."""
    pm = ProjectManager(
        data_dir=str(tmp_path / "fresh"),
        projects_root=str(tmp_path),
    )
    assert pm.is_bound(1) is False
    # No file should have been created yet — _save is only called on bind.
    assert not os.path.exists(os.path.join(str(tmp_path / "fresh"), "projects.json"))


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def test_set_and_get_system_prompt(
    manager: ProjectManager, projects_root: Path
) -> None:
    """set_system_prompt stores a prompt retrievable via get_system_prompt."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(10, str(proj))

    assert manager.get_system_prompt(10) is None
    manager.set_system_prompt(10, "You are a helpful assistant.")
    assert manager.get_system_prompt(10) == "You are a helpful assistant."


def test_clear_system_prompt(
    manager: ProjectManager, projects_root: Path
) -> None:
    """clear_system_prompt removes the prompt; get returns None afterwards."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(20, str(proj))

    manager.set_system_prompt(20, "Be concise.")
    assert manager.get_system_prompt(20) == "Be concise."

    manager.clear_system_prompt(20)
    assert manager.get_system_prompt(20) is None


def test_system_prompt_persists(
    data_dir: Path, projects_root: Path
) -> None:
    """System prompt survives a round-trip through save/load."""
    proj = projects_root / "p"
    proj.mkdir()

    pm1 = ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))
    pm1.bind(30, str(proj))
    pm1.set_system_prompt(30, "Always respond in JSON.")

    pm2 = ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))
    assert pm2.get_system_prompt(30) == "Always respond in JSON."
