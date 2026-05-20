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


# ---------------------------------------------------------------------------
# Extra directories
# ---------------------------------------------------------------------------


def test_add_extra_dir(
    manager: ProjectManager, projects_root: Path
) -> None:
    """add_extra_dir stores a directory and returns its resolved path."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(50, str(proj))

    extra = projects_root / "extra"
    extra.mkdir()
    resolved = manager.add_extra_dir(50, str(extra))
    assert Path(resolved) == extra.resolve()
    assert manager.get_extra_dirs(50) == [str(extra.resolve())]


def test_add_extra_dir_no_duplicates(
    manager: ProjectManager, projects_root: Path
) -> None:
    """Adding the same directory twice doesn't duplicate it."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(51, str(proj))

    extra = projects_root / "extra"
    extra.mkdir()
    manager.add_extra_dir(51, str(extra))
    manager.add_extra_dir(51, str(extra))
    assert len(manager.get_extra_dirs(51)) == 1


def test_add_extra_dir_not_a_directory(
    manager: ProjectManager, projects_root: Path
) -> None:
    """add_extra_dir raises ValueError for non-directory paths."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(52, str(proj))

    afile = projects_root / "afile"
    afile.write_text("hi")
    with pytest.raises(ValueError, match="Not a directory"):
        manager.add_extra_dir(52, str(afile))


def test_remove_extra_dir(
    manager: ProjectManager, projects_root: Path
) -> None:
    """remove_extra_dir removes a previously added directory."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(53, str(proj))

    extra = projects_root / "extra"
    extra.mkdir()
    manager.add_extra_dir(53, str(extra))
    assert manager.remove_extra_dir(53, str(extra)) is True
    assert manager.get_extra_dirs(53) == []


def test_remove_extra_dir_not_found(
    manager: ProjectManager, projects_root: Path
) -> None:
    """remove_extra_dir returns False if the directory wasn't added."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(54, str(proj))
    assert manager.remove_extra_dir(54, "/nonexistent") is False


def test_get_extra_dirs_empty(manager: ProjectManager) -> None:
    """get_extra_dirs returns empty list for unknown channels."""
    assert manager.get_extra_dirs(999) == []


def test_extra_dirs_persist(
    data_dir: Path, projects_root: Path
) -> None:
    """Extra directories survive a round-trip through save/load."""
    proj = projects_root / "p"
    proj.mkdir()
    extra = projects_root / "extra"
    extra.mkdir()

    pm1 = ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))
    pm1.bind(60, str(proj))
    pm1.add_extra_dir(60, str(extra))

    pm2 = ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))
    assert pm2.get_extra_dirs(60) == [str(extra.resolve())]


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


def test_add_mcp_server(
    manager: ProjectManager, projects_root: Path
) -> None:
    """add_mcp_server stores an MCP server configuration."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(70, str(proj))

    config = {"type": "stdio", "command": "npx", "args": ["-y", "server"]}
    manager.add_mcp_server(70, "myserver", config)
    servers = manager.get_mcp_servers(70)
    assert "myserver" in servers
    assert servers["myserver"]["command"] == "npx"
    assert servers["myserver"]["args"] == ["-y", "server"]


def test_add_mcp_server_http(
    manager: ProjectManager, projects_root: Path
) -> None:
    """add_mcp_server stores HTTP MCP server configuration."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(71, str(proj))

    config = {"type": "http", "url": "https://example.com/mcp"}
    manager.add_mcp_server(71, "web", config)
    servers = manager.get_mcp_servers(71)
    assert servers["web"]["type"] == "http"
    assert servers["web"]["url"] == "https://example.com/mcp"


def test_add_mcp_server_rejects_duplicate(
    manager: ProjectManager, projects_root: Path
) -> None:
    """#254 — adding a server with an existing name raises instead of silently overwriting."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(72, str(proj))

    manager.add_mcp_server(72, "s", {"type": "stdio", "command": "old"})
    with pytest.raises(ValueError, match="already exists"):
        manager.add_mcp_server(72, "s", {"type": "stdio", "command": "new"})
    # Original config preserved.
    assert manager.get_mcp_servers(72)["s"]["command"] == "old"


def test_add_mcp_server_after_remove_succeeds(
    manager: ProjectManager, projects_root: Path
) -> None:
    """#254 — remove-then-add is the supported replace flow."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(720, str(proj))

    manager.add_mcp_server(720, "s", {"type": "stdio", "command": "old"})
    assert manager.remove_mcp_server(720, "s") is True
    manager.add_mcp_server(720, "s", {"type": "stdio", "command": "new"})
    assert manager.get_mcp_servers(720)["s"]["command"] == "new"


@pytest.mark.parametrize(
    "bad_name",
    ["", "   ", "\t", "\n", "name\nwith\nnewline", "name\rwith\rcr"],
)
def test_add_mcp_server_rejects_invalid_name(
    manager: ProjectManager, projects_root: Path, bad_name: str
) -> None:
    """#255 — MCP server names must not be empty/whitespace/newline-containing."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(721, str(proj))
    with pytest.raises(ValueError, match="empty|whitespace|newline"):
        manager.add_mcp_server(721, bad_name, {"type": "stdio", "command": "x"})
    assert manager.get_mcp_servers(721) == {}


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "   ",
        "\t",
        "\n",
        "K\nWITH\nNEWLINE",
        "K\rWITH\rCR",
        "K=injected",
        "=leading_equal",
    ],
)
def test_set_env_rejects_invalid_key(
    manager: ProjectManager, projects_root: Path, bad_key: str
) -> None:
    """#255 — env keys must not be empty/whitespace/newline/contain '='."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(722, str(proj))
    with pytest.raises(ValueError, match="empty|whitespace|newline|'='"):
        manager.set_env(722, bad_key, "value")
    assert manager.get_env(722) == {}


def test_remove_mcp_server(
    manager: ProjectManager, projects_root: Path
) -> None:
    """remove_mcp_server removes a previously added server."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(73, str(proj))

    manager.add_mcp_server(73, "s", {"type": "stdio", "command": "x"})
    assert manager.remove_mcp_server(73, "s") is True
    assert manager.get_mcp_servers(73) == {}


def test_remove_mcp_server_not_found(
    manager: ProjectManager, projects_root: Path
) -> None:
    """remove_mcp_server returns False if the server doesn't exist."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(74, str(proj))
    assert manager.remove_mcp_server(74, "ghost") is False


def test_get_mcp_servers_empty(manager: ProjectManager) -> None:
    """get_mcp_servers returns empty dict for unknown channels."""
    assert manager.get_mcp_servers(999) == {}


def test_mcp_servers_persist(
    data_dir: Path, projects_root: Path
) -> None:
    """MCP server configs survive a round-trip through save/load."""
    proj = projects_root / "p"
    proj.mkdir()

    pm1 = ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))
    pm1.bind(80, str(proj))
    pm1.add_mcp_server(80, "myserver", {"type": "stdio", "command": "npx", "args": ["-y", "srv"]})

    pm2 = ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))
    servers = pm2.get_mcp_servers(80)
    assert "myserver" in servers
    assert servers["myserver"]["command"] == "npx"


def test_mcp_multiple_servers(
    manager: ProjectManager, projects_root: Path
) -> None:
    """Multiple MCP servers can coexist for the same channel."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(81, str(proj))

    manager.add_mcp_server(81, "a", {"type": "stdio", "command": "a-cmd"})
    manager.add_mcp_server(81, "b", {"type": "http", "url": "http://b"})
    servers = manager.get_mcp_servers(81)
    assert len(servers) == 2
    assert "a" in servers
    assert "b" in servers


def test_add_extra_dir_outside_root_raises(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "forbidden"
    outside.mkdir()
    pm = ProjectManager(data_dir=str(tmp_path / "data"), projects_root=str(root))
    pm.bind(1, str(root))  # need a binding first
    with pytest.raises(ValueError, match="outside"):
        pm.add_extra_dir(1, str(outside))


# ---------------------------------------------------------------------------
# Unbound-channel fallback helpers (v1.11, #110)
# ---------------------------------------------------------------------------


def test_get_path_or_default_bound(
    manager: ProjectManager, projects_root: Path
) -> None:
    """A bound channel returns ``(bound_path, True)``."""
    proj = projects_root / "p"
    proj.mkdir()
    manager.bind(100, str(proj))

    path, is_bound = manager.get_path_or_default(100)
    assert is_bound is True
    assert path == proj.resolve()


def test_get_path_or_default_unbound(manager: ProjectManager) -> None:
    """An unbound channel returns ``(Path.home().resolve(), False)``."""
    path, is_bound = manager.get_path_or_default(101)
    assert is_bound is False
    assert path == Path.home().resolve()


def test_should_hint_unbound_first_call_returns_true(
    manager: ProjectManager,
) -> None:
    """First call for a channel returns True."""
    assert manager.should_hint_unbound(200) is True


def test_should_hint_unbound_second_call_returns_false(
    manager: ProjectManager,
) -> None:
    """Subsequent calls for the same channel return False."""
    assert manager.should_hint_unbound(201) is True
    assert manager.should_hint_unbound(201) is False
    assert manager.should_hint_unbound(201) is False


def test_should_hint_unbound_isolated_per_channel(
    manager: ProjectManager,
) -> None:
    """Different channel ids each get their own first-hint."""
    assert manager.should_hint_unbound(300) is True
    assert manager.should_hint_unbound(301) is True
    # And each is now suppressed independently.
    assert manager.should_hint_unbound(300) is False
    assert manager.should_hint_unbound(301) is False


# ---------------------------------------------------------------------------
# tester-1: defensive partial-entry handling in get_path_or_default
# ---------------------------------------------------------------------------


def test_get_path_or_default_partial_entry_falls_through_to_home(
    manager: ProjectManager,
) -> None:
    """``is_bound`` returns True for an entry without a ``path`` key (e.g.
    a stale projects.json from a partial write). The helper must fall
    through to the unbound branch instead of returning ``None``-shaped
    garbage that downstream code asserts on.
    """
    # Simulate a partial entry directly — corresponds to a JSON file
    # written before/around a v1.0 crash that left the row without "path".
    manager._projects["999"] = {"system_prompt": "stale"}
    assert manager.is_bound(999) is True
    assert manager.get_path(999) is None  # the trap

    path, is_bound = manager.get_path_or_default(999)
    assert is_bound is False, "partial entry must be treated as unbound"
    assert path == Path.home().resolve()


# ---------------------------------------------------------------------------
# sec-3 / tester-1: Path.home() raise is caught
# ---------------------------------------------------------------------------


def test_get_path_or_default_no_home(
    manager: ProjectManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Path.home()`` raises ``RuntimeError`` when ``$HOME`` is unset and
    there's no passwd entry. ``get_path_or_default`` must catch that and
    return a sentinel path that ``is_dir()`` reports False for, so the
    bot's broken-home guard fires cleanly instead of bubbling a stack
    trace into the on-message handler.
    """
    def _raise(*_args, **_kwargs):
        raise RuntimeError("HOME not set")

    monkeypatch.setattr(Path, "home", _raise)

    path, is_bound = manager.get_path_or_default(8888)
    assert is_bound is False
    assert path.is_dir() is False, "sentinel must report not-a-directory"


def test_get_path_or_default_home_oserror(
    manager: ProjectManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Path.home().resolve()`` can raise ``OSError`` on broken filesystems.
    The helper must catch that too — same sentinel.
    """
    def _raise(*_args, **_kwargs):
        raise OSError("EIO")

    # OSError on the very ``Path.home()`` call exercises the OSError leg of
    # the except clause without needing to fake a Path subclass.
    monkeypatch.setattr(Path, "home", _raise)

    path, is_bound = manager.get_path_or_default(8889)
    assert is_bound is False
    assert path.is_dir() is False


# ---------------------------------------------------------------------------
# arch-2: _assert_bound invariant in project-mutating methods
# ---------------------------------------------------------------------------


@pytest.fixture
def bound_manager(
    manager: ProjectManager, projects_root: Path
) -> tuple[ProjectManager, int]:
    """Manager with channel id 7000 already bound to a real directory."""
    proj = projects_root / "boundproj"
    proj.mkdir()
    manager.bind(7000, str(proj))
    return manager, 7000


@pytest.mark.parametrize(
    "method_name,args",
    [
        ("set_system_prompt", ("hi there",)),
        ("set_budget", (1.50,)),
        ("set_env", ("FOO", "bar")),
        ("remove_env", ("FOO",)),
        ("add_mcp_server", ("srv", {"type": "stdio", "command": "/bin/true"})),
        ("remove_mcp_server", ("srv",)),
        ("set_channel_mode", ("forum",)),
        ("remove_extra_dir", ("/tmp/x",)),
    ],
)
def test_mutators_raise_on_unbound_channel(
    manager: ProjectManager, method_name: str, args: tuple
) -> None:
    """Every project-mutating method must raise ``ValueError`` containing
    "not bound" when called against an unbound channel id. This is the
    arch-2 defense-in-depth invariant — even if a future cog forgets the
    user-facing ``reject_if_unbound`` guard, the manager refuses to write.
    """
    fn = getattr(manager, method_name)
    with pytest.raises(ValueError, match="not bound"):
        fn(123456789, *args)
    # And nothing was written.
    assert "123456789" not in manager._projects


def test_add_extra_dir_raises_on_unbound_channel(
    manager: ProjectManager, projects_root: Path
) -> None:
    """``add_extra_dir`` is parametrized separately because its second
    arg must be a real directory; the others' inputs are all literals.
    """
    target = projects_root / "extra"
    target.mkdir()
    with pytest.raises(ValueError, match="not bound"):
        manager.add_extra_dir(987654321, str(target))
    assert "987654321" not in manager._projects


def test_mutators_succeed_on_bound_channel(
    bound_manager: tuple[ProjectManager, int], projects_root: Path
) -> None:
    """Sanity: arch-2 doesn't break the happy path. Each mutator must
    succeed on a channel that's been bound.
    """
    pm, ch = bound_manager
    pm.set_system_prompt(ch, "hi")
    assert pm.get_system_prompt(ch) == "hi"
    pm.set_budget(ch, 0.50)
    assert pm.get_budget(ch) == pytest.approx(0.50)
    pm.set_env(ch, "K", "v")
    assert pm.get_env(ch) == {"K": "v"}
    assert pm.remove_env(ch, "K") is True
    pm.add_mcp_server(ch, "srv", {"type": "stdio", "command": "/bin/true"})
    assert "srv" in pm.get_mcp_servers(ch)
    assert pm.remove_mcp_server(ch, "srv") is True
    pm.set_channel_mode(ch, "forum")
    assert pm.get_channel_mode(ch) == "forum"
    extra = projects_root / "another"
    extra.mkdir()
    pm.add_extra_dir(ch, str(extra))
    assert str(extra.resolve()) in pm.get_extra_dirs(ch)
    assert pm.remove_extra_dir(ch, str(extra)) is True


# ---------------------------------------------------------------------------
# Mention-required toggle (v1.17 #138)
# ---------------------------------------------------------------------------


def test_get_mention_required_default_true(manager: ProjectManager) -> None:
    """Never-set channel returns True (zero regression default)."""
    assert manager.get_mention_required(99999) is True


def test_set_mention_required_persists(
    bound_manager: tuple[ProjectManager, int],
    data_dir: Path,
    projects_root: Path,
) -> None:
    """Set False, reload manager from disk, get back False."""
    pm, ch = bound_manager
    pm.set_mention_required(ch, False)
    assert pm.get_mention_required(ch) is False
    # Reload from disk
    pm2 = ProjectManager(data_dir=str(data_dir), projects_root=str(projects_root))
    assert pm2.get_mention_required(ch) is False


def test_set_mention_required_unbound_raises(manager: ProjectManager) -> None:
    """Unbound channel rejection mirrors set_channel_mode / set_system_prompt."""
    with pytest.raises(ValueError):
        manager.set_mention_required(12345, False)


def test_unbind_preserves_mention_required(
    bound_manager: tuple[ProjectManager, int],
    projects_root: Path,
) -> None:
    """v1.17 invariant: mention_required survives unbind/rebind (intentional stick)."""
    pm, ch = bound_manager
    pm.set_mention_required(ch, False)
    pm.unbind(ch)
    # Rebind same channel
    proj = projects_root / "boundproj"  # same path used by bound_manager fixture
    pm.bind(ch, str(proj))
    assert pm.get_mention_required(ch) is False


def test_unbind_does_not_preserve_other_settings(
    bound_manager: tuple[ProjectManager, int],
    projects_root: Path,
) -> None:
    """v1.17 invariant pin: only mention_required survives unbind; system_prompt does not."""
    pm, ch = bound_manager
    pm.set_system_prompt(ch, "sticky prompt")
    pm.set_mention_required(ch, False)
    pm.unbind(ch)
    proj = projects_root / "boundproj"
    pm.bind(ch, str(proj))
    # system_prompt wiped, mention_required kept
    assert pm.get_system_prompt(ch) is None
    assert pm.get_mention_required(ch) is False


def test_mention_required_stored_in_separate_registry(
    bound_manager: tuple[ProjectManager, int],
    data_dir: Path,
) -> None:
    """Architect-decided invariant: registry lives in channel_settings.json,
    NOT in projects.json. Pins the separate-file design choice (a future
    revert to nesting would fail this test)."""
    pm, ch = bound_manager
    pm.set_mention_required(ch, False)
    settings_file = data_dir / "channel_settings.json"
    assert settings_file.exists()
    with open(settings_file) as f:
        data = json.load(f)
    assert data[str(ch)]["mention_required"] is False
