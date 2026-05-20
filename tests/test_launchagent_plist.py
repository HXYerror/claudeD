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
    """#232 follow-up round 2: ``ProcessType`` IS set, to ``Interactive``.

    Round-1 fix tried removing the key entirely, but launchd's default
    behavior for missing ``ProcessType`` still applies the "inefficient"
    resource throttle (per launchd.plist(5)). 2 SIGKILL events fired
    post-fix at 18:18 + 19:33 CST on 2026-05-18. ``Interactive``
    explicitly opts out of the resource judgement.
    """
    data = _render_template()
    assert "ProcessType" not in data, (
        f"#232 round 3: ProcessType should be absent. Got: {data.get('ProcessType')!r}"
    )
    assert data.get("LowPriorityBackgroundIO") is False, (
        "#232 round 3: LowPriorityBackgroundIO must be false"
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
    # Round-1 comment (removed Background) preserved as historical context
    assert "#232 round 3" in text, (
        "#232: round 3 comment must be present"
    )
    assert "because inefficient" in text, (
        "Pin the launchd reason string in the comment for future grep-back"
    )
    # Round-2 comment explaining the actual fix
    assert "#232 round 3" in text or "LowPriorityBackgroundIO" in text, (
        "#232 round 2: the why-Interactive comment must stay so a future "
        "plist-cleanup refactor doesn't swap back to the wrong value"
    )
