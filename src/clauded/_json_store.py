"""Shared helpers for atomic JSON-file persistence (#252).

Background
----------
Every JSON-backed store in this package (``CostTracker``, ``SessionStore``,
``AgentManager``, ``ProjectManager`` × 2 files) was independently
implementing the "write to ``X.tmp`` then ``os.replace(X.tmp, X)`` "
idiom. The fixed ``.tmp`` filename was shared across every concurrent
caller, so two threads racing inside ``_save()`` overwrote each other's
tmp file and the loser's ``os.replace`` raised ``FileNotFoundError``
(issue #252).

Fix
---
``atomic_write_json`` centralises the write so every store gets the
same correctness guarantees:

1. **Unique tmp filename** (``.{pid}.{8-hex}.tmp``) — concurrent callers
   never collide on the tmp path, even cross-thread.
2. **threading.Lock** — serialises the entire write within the process so
   in-memory state can't be torn between ``json.dump`` and ``os.replace``.
3. **Stale tmp cleanup** — a ``finally`` clause unlinks the tmp on any
   error path so a mid-write exception (disk full, perms, KeyboardInterrupt)
   doesn't leave a stale ``*.tmp`` next to the live file.

Cross-process safety is intentionally out of scope: the bot runs as a
single process under LaunchAgent / systemd, and a cross-process file lock
(``fcntl.flock`` / ``msvcrt.locking``) would add Windows/Linux divergence
for zero current benefit. If multi-process writers are ever introduced,
swap the in-memory ``Lock`` for an OS-level one here and every store
inherits the upgrade for free.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any


def atomic_write_json(
    path: Path,
    data: Any,
    lock: "threading.Lock | threading.RLock",
    *,
    indent: int = 2,
    sort_keys: bool = False,
    default: Any = None,
) -> None:
    """Atomically serialise ``data`` to ``path`` as JSON.

    Parameters
    ----------
    path:
        Destination file. Parent directory is created if absent.
    data:
        Anything ``json.dump`` accepts.
    lock:
        A ``threading.Lock`` or ``RLock`` owned by the caller. Held for
        the entire write so concurrent callers from the same process
        serialise. Callers that need to hold the lock across a
        read-modify-write (e.g. ``CostTracker.record``) should pass an
        ``RLock`` and acquire it themselves around the wider critical
        section — ``atomic_write_json`` will then reacquire it
        recursively without deadlocking.
    indent:
        ``json.dump`` indent; default 2 matches every existing store.
    sort_keys:
        ``json.dump`` sort_keys; ProjectManager passes True, others False.
    default:
        Optional ``json.dump`` default-serializer hook (e.g. ``str``).

    Behaviour
    ---------
    - Writes to ``path.with_suffix(f".{pid}.{rand}.tmp")`` then
      ``os.replace``-s into ``path`` — atomic on POSIX and Windows.
    - On any exception the tmp file is unlinked so the data dir stays
      clean (AC3 in the PRD).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique suffix prevents concurrent callers from clobbering each
    # other's tmp file (the #252 root cause).
    tmp = path.with_suffix(f".{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    data,
                    f,
                    indent=indent,
                    sort_keys=sort_keys,
                    default=default,
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
    finally:
        # If we made it past os.replace the tmp is already gone and
        # missing_ok swallows that. If we crashed before replace this
        # cleans up the stale tmp so we don't accumulate junk.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            # Best effort — the live file is already correct (or never
            # was), and leaving a tmp behind is recoverable on next run.
            pass


__all__ = ["atomic_write_json"]
