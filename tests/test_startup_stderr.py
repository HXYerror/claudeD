"""Startup-failure diagnostics: surface the CLI's real stderr.

Regression for the "Failed to resume: Command failed with exit code 1 /
Error output: Check stderr output for details" report — the SDK's
``ProcessError`` carries only a hardcoded placeholder, and the SDK only pipes
stderr when a ``stderr`` callback is registered. ``ClaudeBridge`` now wires that
callback into a rolling tail and, on a ``connect()`` failure, raises
``ClaudeStartupError`` with the real output substituted for the placeholder so
the failure is actually investigable.
"""

from __future__ import annotations

from typing import Any

import pytest
from claude_agent_sdk import ProcessError

from clauded.claude_bridge import ClaudeBridge, ClaudeStartupError
from clauded.config import Config


@pytest.fixture
def cfg() -> Config:
    return Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root="/tmp",
    )


def _make_failing_client(stderr_lines: list[str]):
    """SDK client stand-in: emit ``stderr_lines`` via the registered callback,
    then fail ``connect()`` exactly like the real SDK does (opaque
    ``ProcessError`` whose stderr is the useless placeholder)."""

    class _FailingClient:
        def __init__(self, options: Any = None) -> None:
            # start() passes ``stderr=self._capture_stderr`` in options.
            self._cb = getattr(options, "stderr", None)

        async def connect(self, prompt: Any = None) -> None:
            if self._cb is not None:
                for line in stderr_lines:
                    self._cb(line)
            raise ProcessError(
                "Command failed with exit code 1",
                exit_code=1,
                stderr="Check stderr output for details",
            )

        async def disconnect(self) -> None:
            pass

    return _FailingClient


@pytest.mark.asyncio
async def test_start_failure_surfaces_real_stderr(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """A connect() crash raises ClaudeStartupError carrying the real stderr."""
    lines = [
        "Error: ANTHROPIC_API_KEY is invalid or expired",
        "  at validateAuth (cli.js:4242)",
    ]
    monkeypatch.setattr(
        "clauded.claude_bridge.ClaudeSDKClient", _make_failing_client(lines)
    )

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    with pytest.raises(ClaudeStartupError) as ei:
        await bridge.start()

    msg = str(ei.value)
    # Real diagnostic output is present…
    assert "ANTHROPIC_API_KEY is invalid" in msg
    assert "validateAuth (cli.js:4242)" in msg
    # …and the useless SDK placeholder is gone.
    assert "Check stderr output for details" not in msg
    # Split attributes are available for callers that want them.
    assert ei.value.stderr_tail
    assert "exit code 1" in ei.value.original
    # The failed bridge is not marked active.
    assert bridge.is_active is False


@pytest.mark.asyncio
async def test_start_failure_without_stderr_reraises_original(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """No stderr captured ⇒ original exception propagates unchanged.

    We don't manufacture a fake tail; there's nothing better to show than the
    SDK's own message, and callers should still see the real exception type.
    """
    monkeypatch.setattr(
        "clauded.claude_bridge.ClaudeSDKClient", _make_failing_client([])
    )

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    with pytest.raises(ProcessError) as ei:
        await bridge.start()
    assert not isinstance(ei.value, ClaudeStartupError)
    assert bridge.is_active is False


def test_recent_stderr_trims_to_max_chars(cfg: Config) -> None:
    """recent_stderr keeps the most recent output within the char budget."""
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    for i in range(200):
        bridge._capture_stderr(f"line-{i:04d} " + "x" * 40)

    out = bridge.recent_stderr(max_chars=300)
    assert len(out) <= 302  # 300 + the "…\n" marker
    assert out.startswith("…\n")
    # The tail (most recent lines) is retained, the head dropped.
    assert "line-0199" in out
    assert "line-0000" not in out


def test_capture_stderr_is_bounded(cfg: Config) -> None:
    """The rolling tail never grows without bound regardless of stderr volume."""
    from clauded.claude_bridge import _STDERR_TAIL_LINES

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    for i in range(_STDERR_TAIL_LINES * 3):
        bridge._capture_stderr(f"noise {i}")
    assert len(bridge._stderr_tail) == _STDERR_TAIL_LINES


def test_startup_error_strips_only_exact_placeholder() -> None:
    """ClaudeStartupError replaces the placeholder line, keeps the rest."""
    original = (
        "Command failed with exit code 1 (exit code: 1)\n"
        "Error output: Check stderr output for details"
    )
    err = ClaudeStartupError(original, "boom: real detail")
    text = str(err)
    assert "Command failed with exit code 1 (exit code: 1)" in text
    assert "Check stderr output for details" not in text
    assert "boom: real detail" in text
