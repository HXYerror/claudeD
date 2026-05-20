# PRD — #252 CostTracker._save() race condition

**Issue**: #252 (bug, P1)
**Branch**: `fix/252-save-race`
**Status**: APPROVED

## Problem
`_save()` uses fixed tmp filename (`costs.tmp`). Concurrent `record()` calls overwrite each other's tmp → `FileNotFoundError` on `os.replace()`.

## Approach (locked — C from issue: unique tmp + lock)

1. **Unique tmp**: `self._path.with_suffix(f".{os.getpid()}.{secrets.token_hex(4)}.tmp")`
2. **threading.Lock**: serialize `_save()` within process
3. **Apply to all 5 stores**: cost_tracker, session_store, agent_manager, project_manager (×2)

## Implementation

Extract a shared helper `_atomic_write_json(path, data, lock)` in `src/clauded/_json_store.py`:

```python
import json, os, secrets, threading
from pathlib import Path

def atomic_write_json(path: Path, data: dict, lock: threading.Lock) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with lock:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
    finally:
        # Clean up stale tmp on any error
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
```

Each store:
- Add `self._lock = threading.Lock()` in `__init__`
- Replace `_save()` body with `atomic_write_json(self._path, self._data, self._lock)`

## Tests
- Stress test: 100 concurrent `record()` calls → 0 errors, final count == 100
- Same for session_store
- Tmp cleanup: no `.tmp` files left after stress

## AC
- AC1: 100 concurrent record() → 0 FileNotFoundError
- AC2: all 5 stores patched
- AC3: no stale .tmp after crash
- AC4: final persisted data reflects all operations
