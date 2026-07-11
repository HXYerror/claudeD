# PRD — #291 default effort=max

**Issue**: #291 (feat, P1)
**Branch**: `feat/291-default-effort`
**Status**: APPROVED

## Fix
`session_config.py`: change `effort` default from `None` to `os.environ.get("CLAUDED_DEFAULT_EFFORT", "max")`.

## AC
- AC1: new session effort=max by default
- AC2: /effort can override
- AC3: env CLAUDED_DEFAULT_EFFORT works
- AC4: resume unchanged
