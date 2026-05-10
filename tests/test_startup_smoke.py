"""Smoke tests guarding the three startup-blocking bugs in #11/#12/#13.

These tests are intentionally cheap and side-effect free; they exercise the
narrowest path that previously prevented the bot from starting on macOS.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clauded.config import load_config
from clauded.interaction_handler import AskButtonView, AskSelectView
from clauded.project_manager import ProjectManager


# ---------------------------------------------------------------------------
# Bug #11 — projects_root and bind() must agree after symlink resolution.
# ---------------------------------------------------------------------------


def test_bind_works_when_projects_root_is_a_symlink(tmp_path: Path) -> None:
    """A symlinked projects_root (mirrors macOS /tmp -> /private/tmp) still binds.

    Before the fix, ``ProjectManager`` stored ``projects_root`` unresolved
    while ``bind()`` resolved the user-supplied path; on macOS that mismatch
    made every bind under ``/tmp`` fail with "outside the allowed projects
    root".
    """
    real_root = tmp_path / "real_root"
    real_root.mkdir()
    link_root = tmp_path / "link_root"
    link_root.symlink_to(real_root, target_is_directory=True)

    proj = real_root / "myproj"
    proj.mkdir()

    pm = ProjectManager(
        data_dir=str(tmp_path / "data"),
        projects_root=str(link_root),  # the symlinked side
    )

    # Both projects_root and the bound path should be fully resolved, and
    # the bind should not raise "outside the allowed projects root".
    assert pm.projects_root == real_root.resolve()
    stored = pm.bind(1, str(proj))
    assert Path(stored) == proj.resolve()


def test_load_config_resolves_projects_root_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``load_config`` must resolve symlinks in ``CLAUDED_PROJECTS_ROOT``."""
    real_root = tmp_path / "real_root"
    real_root.mkdir()
    link_root = tmp_path / "link_root"
    link_root.symlink_to(real_root, target_is_directory=True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok-abc")
    monkeypatch.setenv("CLAUDED_PROJECTS_ROOT", str(link_root))
    cfg = load_config()
    assert cfg.projects_root == str(real_root.resolve())


# ---------------------------------------------------------------------------
# Bug #13 — ask views can be constructed without a running event loop.
# ---------------------------------------------------------------------------


def test_ask_button_view_constructs_without_running_loop() -> None:
    """Building an ``AskButtonView`` from sync code must not crash.

    Previously the ``__init__`` called ``asyncio.get_running_loop()``, which
    raises ``RuntimeError`` outside an async context — including in early
    bot init and during unit tests. The view now defers future creation
    until ``wait_for_result`` is awaited.
    """
    view = AskButtonView(["Yes", "No"])
    # No future should be allocated yet, and the result is unset.
    assert view._result_future is None
    assert view._resolved is False


def test_ask_select_view_constructs_without_running_loop() -> None:
    """Same guarantee for the select-menu variant."""
    view = AskSelectView(
        labels=["A", "B", "C", "D", "E"],
        descriptions=["", "", "", "", ""],
        multi_select=False,
    )
    assert view._result_future is None
    assert view._resolved is False


def test_ask_button_view_resolve_before_wait_returns_result() -> None:
    """If a callback fires before any waiter, the value is preserved."""
    view = AskButtonView(["Yes", "No"])
    # Simulate a button callback running on the event loop before
    # wait_for_result is ever awaited.
    view._resolve([1])
    assert view._resolved is True
    assert view._result == [1]


@pytest.mark.asyncio
async def test_ask_button_view_wait_for_result_after_resolve() -> None:
    """``wait_for_result`` returns the captured value when already resolved."""
    view = AskButtonView(["Yes", "No"])
    view._resolve([0])
    assert await view.wait_for_result() == [0]


@pytest.mark.asyncio
async def test_ask_button_view_wait_then_resolve() -> None:
    """A waiter that registers first is unblocked when ``_resolve`` fires."""
    import asyncio

    view = AskButtonView(["Yes", "No"])

    async def resolver() -> None:
        # Yield once so the waiter has a chance to register the future.
        await asyncio.sleep(0)
        view._resolve([1])

    waiter_task = asyncio.create_task(view.wait_for_result())
    resolver_task = asyncio.create_task(resolver())
    result = await waiter_task
    await resolver_task
    assert result == [1]
