# PRD — #289 Windows support (Subtasks 1-5)

**Issue**: #289 (epic, P2)
**Branch**: `feat/289-windows-support`
**Status**: APPROVED

## Scope (this PR)
Subtasks 1-5 (code fixes for cross-platform). Subtask 6 (install script) deferred.

## Changes

### Subtask 1 — `pwd` import blocker
`src/clauded/diagnostics/redact.py`: wrap `import pwd` in try/except, fallback to `getpass.getuser()`.

### Subtask 2 — logging path
`src/clauded/_logging_setup.py`: add Windows `%LOCALAPPDATA%\clauded\logs` branch.

### Subtask 3 — font candidates
`src/clauded/table_png.py`: add Windows CJK font paths.

### Subtask 4 — attachment + subprocess
`src/clauded/discord_renderer.py`: use `tempfile.gettempdir()` for attachment whitelist.
`src/clauded/bot.py` + `src/clauded/cogs/diff.py`: use `shutil.which()` for subprocess calls.

### Subtask 5 — path redaction
`src/clauded/diagnostics/redact.py`: add `C:\Users\` pattern.

## AC
- AC1: `import clauded.bot` succeeds on all platforms (no `pwd` crash)
- AC6: macOS zero regression (pytest passes)
