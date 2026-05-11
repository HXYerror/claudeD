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
    """No PYTEST_CURRENT_TEST → RotatingFileHandler + stderr handler attached, log dir created."""
    _reset_root_logger()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    # _LOG_DIR is captured at import time, so patch the module attribute
    # directly rather than Path.home (which only affects new resolutions).
    from clauded import bot as bot_mod
    monkeypatch.setattr(bot_mod, "_LOG_DIR", tmp_path / "Library" / "Logs" / "clauded")
    bot_mod._configure_logging()
    root = logging.getLogger()
    rotating = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(rotating) == 1, "production mode should add RotatingFileHandler"
    log_dir = tmp_path / "Library" / "Logs" / "clauded"
    assert log_dir.exists(), "log dir should be created"
    assert (log_dir / "clauded.log").parent == log_dir


def test_configure_logging_fallback_on_oserror(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """mkdir raises OSError → fall back to basicConfig, no crash."""
    _reset_root_logger()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    def boom(*args, **kwargs):
        raise OSError("no /tmp")
    monkeypatch.setattr(Path, "mkdir", boom)
    from clauded.bot import _configure_logging
    _configure_logging()  # should not raise


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
    """_touch_heartbeat on darwin creates the file with a recent mtime."""
    monkeypatch.setattr(sys, "platform", "darwin")
    # _HEARTBEAT_PATH was captured at module import; patch directly.
    fake_heartbeat = tmp_path / "Library" / "Caches" / "clauded" / "heartbeat"
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
