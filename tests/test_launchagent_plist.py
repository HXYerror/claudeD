"""#232 — pin LaunchAgent plist shape so ProcessType=Background can't return.

This template is consumed by `scripts/install-launchagent.sh`. A future
"clean up the plist" refactor that re-adds the wrong ProcessType would
silently re-introduce the every-15-min SIGKILL bug class.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).parent.parent / "scripts" / "com.hxy.clauded.plist.template"


def _render_template() -> dict:
    """Substitute {{HOME}}/{{REPO}} and parse as plist."""
    text = TEMPLATE.read_text()
    text = text.replace("{{HOME}}", "/Users/test")
    text = text.replace("{{REPO}}", "/repo")
    return plistlib.loads(text.encode("utf-8"))


def test_process_type_key_absent():
    """#232: ProcessType key must NOT be set. Default (Standard) is correct."""
    data = _render_template()
    assert "ProcessType" not in data, (
        f"#232: plist must NOT have ProcessType key (Standard default is "
        f"correct for long-running interactive services). Found: "
        f"{data.get('ProcessType')!r}"
    )


def test_keep_alive_and_throttle_preserved():
    """Regression: removing ProcessType must not nuke KeepAlive / throttle."""
    data = _render_template()
    assert data.get("KeepAlive") is True
    assert data.get("ThrottleInterval") == 30
    assert data.get("RunAtLoad") is True


def test_label_and_paths():
    """Sanity: the essential identity stays."""
    data = _render_template()
    assert data["Label"] == "com.hxy.clauded"
    assert "/repo/.venv/bin/clauded" in data["ProgramArguments"]
    assert data["WorkingDirectory"] == "/repo"
    assert "/Users/test/Library/Logs/clauded" in data["StandardOutPath"]


def test_comment_explains_absence():
    """Pin the #232 explainer comment so it's not stripped by accident."""
    text = TEMPLATE.read_text()
    assert "#232 removed" in text, (
        "#232: the why-this-key-is-absent comment must stay; without it a "
        "future plist-cleanup refactor would silently restore ProcessType."
    )
    assert "because inefficient" in text, (
        "Pin the launchd reason string in the comment for future grep-back"
    )
