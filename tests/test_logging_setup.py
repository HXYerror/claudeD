"""Tests for _configure_logging + _heartbeat_task isolation."""
from __future__ import annotations

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

# Tests verify that production logging routes to ~/Library/Logs/clauded/
# while test runs (PYTEST_CURRENT_TEST env present) fall back to stderr-only.


def _reset_root_logger() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def test_configure_logging_in_pytest_env_uses_basicconfig(tmp_path: Path) -> None:
    """PYTEST_CURRENT_TEST set → stderr-only; no RotatingFileHandler attached."""
    _reset_root_logger()
    # PYTEST_CURRENT_TEST is naturally set by pytest itself when running this test.
    assert os.environ.get("PYTEST_CURRENT_TEST"), "pytest should set this env var"
    from clauded.bot import _configure_logging
    _configure_logging()
    root = logging.getLogger()
    rotating = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(rotating) == 0, "PYTEST_CURRENT_TEST should disable file logging"


def test_configure_logging_without_pytest_env_attaches_rotating_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No PYTEST_CURRENT_TEST + _LOG_DIR exists → RotatingFileHandler attached.

    v1.18: _configure_logging no longer mkdirs; _ensure_runtime_dirs owns that.
    The test must pre-create the dir to match the new contract.

    v1.18 stage-28: _LOG_DIR + _configure_logging now live in
    ``clauded._logging_setup``; monkeypatch the source module so the
    function reads our temp path.
    """
    _reset_root_logger()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    # _LOG_DIR is captured at import time, so patch the module attribute
    # directly rather than Path.home (which only affects new resolutions).
    log_dir = tmp_path / "Library" / "Logs" / "clauded"
    log_dir.mkdir(parents=True)  # contract: _ensure_runtime_dirs would do this at startup
    from clauded import _logging_setup as logging_mod
    monkeypatch.setattr(logging_mod, "_LOG_DIR", log_dir)
    logging_mod._configure_logging()
    root = logging.getLogger()
    rotating = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(rotating) == 1, "production mode should add RotatingFileHandler"
    assert log_dir.exists(), "log dir should be present"
    assert (log_dir / "clauded.log").parent == log_dir


def test_configure_logging_fallback_on_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_LOG_DIR doesn't exist (e.g. read-only home, sandboxed runner) → fall
    back to basicConfig (stderr-only), no crash.

    v1.18: _configure_logging no longer mkdirs; it checks _LOG_DIR.exists()
    and falls back to basicConfig when absent. Previously this branch was
    tested by raising OSError on mkdir; now it's tested by simply pointing
    at a path that doesn't exist.

    v1.18 stage-28: monkeypatch ``_logging_setup._LOG_DIR`` (the source
    of truth post-extraction).
    """
    _reset_root_logger()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    from clauded import _logging_setup as logging_mod
    # Path doesn't exist; _ensure_runtime_dirs() not called. _configure_logging
    # must fall back to basicConfig instead of crashing.
    monkeypatch.setattr(logging_mod, "_LOG_DIR", tmp_path / "nonexistent")
    logging_mod._configure_logging()  # should not raise
    root = logging.getLogger()
    rotating = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(rotating) == 0, "missing _LOG_DIR should NOT attach RotatingFileHandler"


def test_configure_logging_skips_on_non_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sys.platform != darwin → basicConfig, no RotatingFileHandler, no ~/Library/ junk."""
    _reset_root_logger()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    from clauded.bot import _configure_logging
    _configure_logging()
    root = logging.getLogger()
    rotating = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(rotating) == 0, "non-darwin should not attach RotatingFileHandler"


def test_touch_heartbeat_writes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_touch_heartbeat on darwin creates the file (when parent dir exists)
    with a recent mtime.

    v1.18: parent dir creation moved to _ensure_runtime_dirs (one-shot at
    startup); test pre-creates the dir to match the new contract.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    cache_dir = tmp_path / "Library" / "Caches" / "clauded"
    cache_dir.mkdir(parents=True)  # contract: _ensure_runtime_dirs creates this at startup
    fake_heartbeat = cache_dir / "heartbeat"
    from clauded import bot as bot_mod
    monkeypatch.setattr(bot_mod, "_HEARTBEAT_PATH", fake_heartbeat)
    before = time.time()
    bot_mod._touch_heartbeat()
    after = time.time()
    assert fake_heartbeat.exists(), "heartbeat file should be created"
    mtime = fake_heartbeat.stat().st_mtime
    # Allow ±2s clock-tick slack so the test isn't flaky.
    assert before - 2 <= mtime <= after + 2, "heartbeat mtime should be recent"


def test_touch_heartbeat_skips_on_non_darwin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_touch_heartbeat on linux/windows is a no-op — no file, no mkdir."""
    monkeypatch.setattr(sys, "platform", "linux")
    fake_heartbeat = tmp_path / "Library" / "Caches" / "clauded" / "heartbeat"
    from clauded import bot as bot_mod
    monkeypatch.setattr(bot_mod, "_HEARTBEAT_PATH", fake_heartbeat)
    bot_mod._touch_heartbeat()
    assert not fake_heartbeat.exists(), "non-darwin should not create heartbeat"
    assert not fake_heartbeat.parent.exists(), "non-darwin should not mkdir"


# ---------------------------------------------------------------------------
# v1.18 R2 carry-overs (PR #149 deferrals)
# ---------------------------------------------------------------------------


def test_ensure_runtime_dirs_creates_both_on_darwin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_ensure_runtime_dirs` creates `_LOG_DIR` AND `_CACHE_DIR` once at startup
    so subsequent `_touch_heartbeat` and `_configure_logging` calls don't redo
    the mkdir per tick (engineer R2 mkdir-on-every-tick nit).

    v1.18 stage-28: function + dirs moved to ``_logging_setup``; the
    bot ``_HEARTBEAT_PATH`` binding still lives in ``bot.py``.
    """
    from clauded import bot as bot_mod
    from clauded import _logging_setup as logging_mod
    monkeypatch.setattr(logging_mod, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(logging_mod, "_CACHE_DIR", tmp_path / "caches")
    monkeypatch.setattr(bot_mod, "_HEARTBEAT_PATH", tmp_path / "caches" / "heartbeat")
    monkeypatch.setattr(logging_mod.sys, "platform", "darwin")
    logging_mod._ensure_runtime_dirs()
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "caches").is_dir()


def test_ensure_runtime_dirs_noop_on_non_darwin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Linux/Windows dev box → no `~/Library/` pollution."""
    from clauded import _logging_setup as logging_mod
    monkeypatch.setattr(logging_mod, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(logging_mod, "_CACHE_DIR", tmp_path / "caches")
    monkeypatch.setattr(logging_mod.sys, "platform", "linux")
    logging_mod._ensure_runtime_dirs()
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "caches").exists()


def test_touch_heartbeat_no_longer_creates_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_touch_heartbeat` should NOT mkdir on every call (cost-on-hot-path).
    Parent dir creation moved to `_ensure_runtime_dirs` (one-shot at startup).
    If the cache dir doesn't exist, _touch_heartbeat silently no-ops via OSError."""
    from clauded import bot as bot_mod
    cache_dir = tmp_path / "caches"  # deliberately NOT created
    monkeypatch.setattr(bot_mod, "_CACHE_DIR", cache_dir)
    monkeypatch.setattr(bot_mod, "_HEARTBEAT_PATH", cache_dir / "heartbeat")
    monkeypatch.setattr(bot_mod.sys, "platform", "darwin")
    bot_mod._touch_heartbeat()
    # Without _ensure_runtime_dirs() first, heartbeat write fails silently.
    # Parent NOT created (regression pin for the engineer R2 nit).
    assert not cache_dir.exists()
