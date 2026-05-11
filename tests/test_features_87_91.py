"""Tests for #87 (forum channel mode) and #91 (multi-guild root)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clauded.project_manager import ProjectManager


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def manager(data_dir: Path, projects_root: Path) -> ProjectManager:
    return ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))


# ===========================================================================
# #87 — Channel mode (thread vs forum)
# ===========================================================================


class TestChannelMode:
    """Tests for set_channel_mode / get_channel_mode."""

    def test_default_mode_is_thread(self, manager: ProjectManager) -> None:
        assert manager.get_channel_mode(999) == "thread"

    def test_set_mode_to_forum(
        self, manager: ProjectManager, projects_root: Path
    ) -> None:
        proj = projects_root / "p"
        proj.mkdir()
        manager.bind(100, str(proj))
        manager.set_channel_mode(100, "forum")
        assert manager.get_channel_mode(100) == "forum"

    def test_set_mode_to_thread(
        self, manager: ProjectManager, projects_root: Path
    ) -> None:
        proj = projects_root / "p"
        proj.mkdir()
        manager.bind(100, str(proj))
        manager.set_channel_mode(100, "forum")
        manager.set_channel_mode(100, "thread")
        assert manager.get_channel_mode(100) == "thread"

    def test_set_mode_invalid_raises(
        self, manager: ProjectManager, projects_root: Path
    ) -> None:
        proj = projects_root / "p"
        proj.mkdir()
        manager.bind(100, str(proj))
        with pytest.raises(ValueError, match="Invalid mode"):
            manager.set_channel_mode(100, "invalid")

    def test_mode_persists(
        self, data_dir: Path, projects_root: Path
    ) -> None:
        proj = projects_root / "p"
        proj.mkdir()
        pm1 = ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))
        pm1.bind(200, str(proj))
        pm1.set_channel_mode(200, "forum")

        pm2 = ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))
        assert pm2.get_channel_mode(200) == "forum"

    def test_mode_saved_in_projects_json(
        self, manager: ProjectManager, data_dir: Path, projects_root: Path
    ) -> None:
        proj = projects_root / "p"
        proj.mkdir()
        manager.bind(300, str(proj))
        manager.set_channel_mode(300, "forum")
        payload = json.loads((data_dir / "projects.json").read_text())
        assert payload["300"]["channel_mode"] == "forum"

    def test_mode_with_existing_binding(
        self, manager: ProjectManager, projects_root: Path
    ) -> None:
        proj = projects_root / "myproj"
        proj.mkdir()
        manager.bind(400, str(proj))
        manager.set_channel_mode(400, "forum")
        # Both binding and mode should coexist
        assert manager.get_project(400) == str(proj.resolve())
        assert manager.get_channel_mode(400) == "forum"

    def test_mode_on_unbound_channel_raises(
        self, manager: ProjectManager
    ) -> None:
        """Mutating channel state on an unbound channel must raise (arch-2)."""
        with pytest.raises(ValueError, match="not bound"):
            manager.set_channel_mode(500, "forum")
        # And reading a never-set mode falls back to the default.
        assert manager.get_channel_mode(500) == "thread"


# ===========================================================================
# #91 — Per-guild project root
# ===========================================================================


class TestGuildRoot:
    """Tests for set_guild_root / get_guild_root / clear_guild_root."""

    def test_default_guild_root_is_projects_root(
        self, manager: ProjectManager, projects_root: Path
    ) -> None:
        assert manager.get_guild_root(None) == projects_root
        assert manager.get_guild_root(12345) == projects_root

    def test_set_guild_root(
        self, manager: ProjectManager, tmp_path: Path
    ) -> None:
        guild_dir = tmp_path / "guild_projects"
        guild_dir.mkdir()
        resolved = manager.set_guild_root(111, str(guild_dir))
        assert Path(resolved) == guild_dir.resolve()
        assert manager.get_guild_root(111) == guild_dir.resolve()

    def test_set_guild_root_not_a_directory_raises(
        self, manager: ProjectManager, tmp_path: Path
    ) -> None:
        afile = tmp_path / "notadir"
        afile.write_text("hi")
        with pytest.raises(ValueError, match="Not a directory"):
            manager.set_guild_root(111, str(afile))

    def test_set_guild_root_nonexistent_raises(
        self, manager: ProjectManager, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="Not a directory"):
            manager.set_guild_root(111, str(tmp_path / "ghost"))

    def test_clear_guild_root(
        self, manager: ProjectManager, tmp_path: Path, projects_root: Path
    ) -> None:
        guild_dir = tmp_path / "guild_projects"
        guild_dir.mkdir()
        manager.set_guild_root(222, str(guild_dir))
        assert manager.get_guild_root(222) == guild_dir.resolve()

        assert manager.clear_guild_root(222) is True
        assert manager.get_guild_root(222) == projects_root

    def test_clear_guild_root_not_set(self, manager: ProjectManager) -> None:
        assert manager.clear_guild_root(999) is False

    def test_guild_root_persists(
        self, data_dir: Path, projects_root: Path, tmp_path: Path
    ) -> None:
        guild_dir = tmp_path / "guild_projects"
        guild_dir.mkdir()

        pm1 = ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))
        pm1.set_guild_root(333, str(guild_dir))

        pm2 = ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))
        assert pm2.get_guild_root(333) == guild_dir.resolve()

    def test_guild_roots_saved_to_separate_file(
        self, manager: ProjectManager, data_dir: Path, tmp_path: Path
    ) -> None:
        guild_dir = tmp_path / "guild_projects"
        guild_dir.mkdir()
        manager.set_guild_root(444, str(guild_dir))
        payload = json.loads((data_dir / "guild_roots.json").read_text())
        assert "444" in payload
        assert payload["444"] == str(guild_dir.resolve())

    def test_bind_uses_guild_root(
        self, manager: ProjectManager, tmp_path: Path
    ) -> None:
        """bind() uses the guild root when guild_id is provided."""
        guild_dir = tmp_path / "guild_projects"
        guild_dir.mkdir()
        proj = guild_dir / "myproj"
        proj.mkdir()

        manager.set_guild_root(555, str(guild_dir))
        stored = manager.bind(100, str(proj), guild_id=555)
        assert Path(stored) == proj.resolve()

    def test_bind_rejects_path_outside_guild_root(
        self, manager: ProjectManager, tmp_path: Path, projects_root: Path
    ) -> None:
        """bind() with guild_id rejects paths outside that guild's root."""
        guild_dir = tmp_path / "guild_projects"
        guild_dir.mkdir()
        manager.set_guild_root(666, str(guild_dir))

        # Path inside default root but outside guild root
        proj = projects_root / "proj"
        proj.mkdir()
        with pytest.raises(ValueError, match="outside the allowed projects root"):
            manager.bind(100, str(proj), guild_id=666)

    def test_bind_without_guild_uses_default(
        self, manager: ProjectManager, projects_root: Path
    ) -> None:
        """bind() without guild_id uses the default root."""
        proj = projects_root / "proj"
        proj.mkdir()
        stored = manager.bind(100, str(proj))
        assert Path(stored) == proj.resolve()

    def test_multiple_guilds(
        self, manager: ProjectManager, tmp_path: Path
    ) -> None:
        """Multiple guilds can each have their own root."""
        g1 = tmp_path / "guild1"
        g1.mkdir()
        g2 = tmp_path / "guild2"
        g2.mkdir()

        manager.set_guild_root(1, str(g1))
        manager.set_guild_root(2, str(g2))

        assert manager.get_guild_root(1) == g1.resolve()
        assert manager.get_guild_root(2) == g2.resolve()
