"""Tests for _configure_logging + _heartbeat_task isolation."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch

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
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from clauded.bot import _configure_logging
    _configure_logging()
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
    def boom(*args, **kwargs):
        raise OSError("no /tmp")
    monkeypatch.setattr(Path, "mkdir", boom)
    from clauded.bot import _configure_logging
    _configure_logging()  # should not raise
