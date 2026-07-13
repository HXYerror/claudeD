"""#224 diagnostics — end-to-end /log dump bundle pipeline.

:func:`generate_bundle` produces a single ``.zip`` file containing
manifest + redacted env + tailed logs + state snapshots + runtime
snapshots + diagnostics dumps. Size capped at 8 MB (Discord T0 free
guild upload limit minus 20% buffer).

Bundle layout (per PRD §Bundle Contents):

::

    clauded-dump-<bot_pid>-<UTC_ISO>.zip
    ├── manifest.json
    ├── env-redacted.txt
    ├── logs/
    │   ├── clauded.log              (tail)
    │   └── stream-debug.tail.jsonl  (tail)
    ├── state/
    │   ├── projects.json            (path-redacted)
    │   ├── sessions.json            (path + prompt-redacted)
    │   └── costs.json               (verbatim)
    ├── runtime/
    │   ├── sessions-live.json
    │   └── bot-flags.json
    └── transcripts/                 (#304)
        └── <session_id>.tail.jsonl  (tail 1 MB)

This module imports lazily — bundle generation should not pull in
discord.py at module-load time.
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from . import redact

log = logging.getLogger("clauded.diagnostics.bundle")

# ---------------------------------------------------------------------------
# Tunables — PRD §Size budget
# ---------------------------------------------------------------------------

BUNDLE_SIZE_BUDGET = 8 * 1024 * 1024  # 8 MB (T0 guild limit 10 MB - 20%)
LOG_TAIL_BYTES = 1 * 1024 * 1024  # 1 MB per tailed log file
STREAM_DEBUG_TAIL_BYTES = 5 * 1024 * 1024  # 5 MB jsonl tail
TRANSCRIPT_TAIL_BYTES = 1 * 1024 * 1024  # 1 MB per session transcript

# Directory where rotating logs land (matches _logging_setup._LOG_DIR).
_LOG_DIR = Path.home() / "Library" / "Logs" / "clauded"


# ---------------------------------------------------------------------------
# Log tailing
# ---------------------------------------------------------------------------


def _tail_bytes(path: Path, byte_limit: int) -> bytes:
    """Return at most ``byte_limit`` bytes from the END of ``path``.

    Used so we don't ship the entire rotating log if it's hours-deep.
    Returns ``b""`` if the file is missing or can't be read.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return b""
    try:
        with open(path, "rb") as f:
            if size > byte_limit:
                f.seek(size - byte_limit)
                # Drop the partial first line so the tail is parseable.
                _ = f.readline()
            return f.read()
    except OSError as exc:
        log.warning("#224: log tail failed for %s: %s", path, exc)
        return b""


# ---------------------------------------------------------------------------
# Runtime snapshots
# ---------------------------------------------------------------------------


def _snapshot_live_sessions(bot: Any) -> list[dict]:
    """Best-effort: dump ``bot.session_manager.list_sessions()`` to JSON-safe.

    Each bridge contributes the non-sensitive subset of its state. We
    deliberately exclude ``system_prompt`` text + ``env`` overlay values
    (only the keys). Returns ``[]`` if bot/session_manager not available.
    """
    out: list[dict] = []
    sm = getattr(bot, "session_manager", None)
    if sm is None:
        return out
    try:
        live = sm.list_sessions()
    except Exception:
        return out
    for thread_id, bridge in live.items():
        try:
            entry: dict[str, Any] = {
                "thread_id": thread_id,
                "project_path": redact.redact_path(
                    str(getattr(bridge, "project_path", "") or "")
                ),
                "session_id": getattr(bridge, "session_id", None),
                "is_active": bool(getattr(bridge, "is_active", False)),
                "total_cost": getattr(bridge, "total_cost", 0.0),
                "num_turns": getattr(bridge, "num_turns", 0),
                "model_override": getattr(bridge, "_model_override", None),
                "sdk_model": getattr(bridge, "_sdk_model", None),
                "permission_mode_override": getattr(bridge, "_permission_mode_override", None),
                # system_prompt EXPLICITLY redacted (verbatim is sensitive)
                "system_prompt_marker": redact._digest_marker(
                    str(getattr(bridge, "system_prompt", "") or "")
                ),
            }
            out.append(entry)
        except Exception as exc:
            log.warning("#224: session snapshot failed for thread=%s: %s", thread_id, exc)
    return out


def _snapshot_bot_flags(bot: Any) -> dict:
    """Dump runtime-toggleable flags from the bot for diagnosis."""
    keys = [
        "_debug_logging",
        "_pre_tool_notifications",
        "_notify_enabled",
        "_allow_unbound_fallback",
        "_start_time",
        "_claude_version",
        "_stream_debug_enabled",
    ]
    out: dict = {}
    for k in keys:
        if hasattr(bot, k):
            v = getattr(bot, k)
            try:
                # JSON-safe coerce (dicts and primitives pass; sets/tuples
                # get listified; objects fall back to str).
                if isinstance(v, (str, int, float, bool, list, dict)) or v is None:
                    out[k] = v
                elif isinstance(v, (set, tuple)):
                    out[k] = list(v)
                else:
                    out[k] = str(v)
            except Exception:
                out[k] = "<unrenderable>"
    return out


# ---------------------------------------------------------------------------
# State file copy (with redaction per type)
# ---------------------------------------------------------------------------


def _read_state_file(data_dir: Path, name: str) -> tuple[bytes, bool]:
    """Read ``data_dir/<name>``. Returns (bytes, found).

    ``found=False`` when the file doesn't exist — caller skips writing
    that entry to the bundle so we don't have phantom empty files.
    """
    path = data_dir / name
    if not path.exists():
        return (b"", False)
    try:
        return (path.read_bytes(), True)
    except OSError as exc:
        log.warning("#224: state-file read failed for %s: %s", path, exc)
        return (b"", False)


def _redact_state_file(name: str, raw: bytes) -> bytes:
    """Apply per-schema redaction to a state file's JSON bytes.

    Falls back to the original bytes if JSON parsing fails (so we still
    ship SOMETHING even if the file is corrupted).
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return raw
    if name == "projects.json" and isinstance(data, dict):
        data = redact.redact_projects_json(data)
    elif name == "sessions.json" and isinstance(data, dict):
        data = redact.redact_sessions_json(data)
    # costs.json + channel_settings.json + guild_roots.json: verbatim
    return json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _build_manifest(
    *,
    generated_by: str,
    crash_context: dict | None,
    bot: Any,
) -> dict:
    """Assemble the manifest dict per PRD §Bundle Contents.

    #224 R1 security: ``crash_context['traceback']`` and ``exc_message``
    can contain ``/Users/<actual>/`` from stack frames or SDK error
    strings. Apply path redaction before embedding into manifest.
    """
    now = datetime.now(timezone.utc)
    start_time = getattr(bot, "_start_time", None) if bot is not None else None
    uptime_s = (
        round(time.time() - start_time, 1)
        if isinstance(start_time, (int, float))
        else None
    )
    # Redact crash_context in place (shallow copy so we don't mutate caller's dict).
    redacted_crash = None
    if crash_context is not None:
        redacted_crash = dict(crash_context)
        if isinstance(redacted_crash.get("traceback"), list):
            redacted_crash["traceback"] = [
                redact.redact_text(str(line)) for line in redacted_crash["traceback"]
            ]
        elif isinstance(redacted_crash.get("traceback"), str):
            redacted_crash["traceback"] = redact.redact_text(redacted_crash["traceback"])
        if isinstance(redacted_crash.get("exc_message"), str):
            redacted_crash["exc_message"] = redact.redact_text(
                redacted_crash["exc_message"]
            )
    return {
        "bundle_version": 1,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "generated_by": generated_by,
        "bot_pid": os.getpid(),
        "bot_uptime_s": uptime_s,
        "claude_cli_version": getattr(bot, "_claude_version", "unknown") if bot else "unknown",
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "platform": sys.platform,
        "crash_context": redacted_crash,
    }


# ---------------------------------------------------------------------------
# CLI session transcript collection (#304)
# ---------------------------------------------------------------------------


def _collect_transcripts(
    zf: zipfile.ZipFile,
    data_dir: Path,
) -> None:
    """Collect CLI session transcripts from stored sessions.

    For each session with a ``session_id``, compute the project slug from
    ``project_path`` (replace ``/`` → ``-``, strip leading ``-``) and look
    for ``~/.claude/projects/<slug>/<session_id>.jsonl``. If it exists,
    tail 1 MB and add to the zip as ``transcripts/<session_id>.tail.jsonl``.
    """
    # Read stored sessions from data_dir/sessions.json
    raw, found = _read_state_file(data_dir, "sessions.json")
    if not found:
        return
    try:
        sessions = json.loads(raw.decode("utf-8"))
    except Exception:
        return
    if not isinstance(sessions, dict):
        return

    claude_projects = Path.home() / ".claude" / "projects"

    for _thread_id, stored in sessions.items():
        session_id = stored.get("session_id") if isinstance(stored, dict) else None
        project_path = (stored.get("project_path") or "") if isinstance(stored, dict) else ""
        if not session_id or not project_path:
            continue

        slug = project_path.replace("/", "-").lstrip("-")
        transcript = claude_projects / slug / f"{session_id}.jsonl"
        if not transcript.exists():
            continue

        tail = _tail_bytes(transcript, TRANSCRIPT_TAIL_BYTES)
        if tail:
            # Apply path redaction (consistent with other bundle blobs)
            redacted = redact.redact_text(tail.decode("utf-8", errors="replace"))
            zf.writestr(f"transcripts/{session_id}.tail.jsonl", redacted.encode("utf-8"))


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def generate_bundle(
    *,
    bot: Any = None,
    data_dir: Path | str = "data",
    out_dir: Path | str | None = None,
    generated_by: Literal["slash", "auto-crash"] = "slash",
    crash_context: dict | None = None,
    log_dir: Path | None = None,
    size_budget: int = BUNDLE_SIZE_BUDGET,
) -> Path:
    """Build the diagnostic bundle and return the path to the zip.

    Parameters
    ----------
    bot
        ``ClaudedBot`` instance (or any duck-typed equivalent for tests).
        Only attribute-read; never mutated.
    data_dir
        Where the state JSON files live (default ``data/``).
    out_dir
        Where to write the bundle zip (default = ``data_dir``).
    generated_by
        Triggered path: ``"slash"`` or ``"auto-crash"``. Lands in manifest.
    crash_context
        Optional payload for auto-crash: ``{"where": str, "thread_id": int,
        "exc_class": str, "traceback": str}``. Embedded verbatim in manifest.
    log_dir
        Override ``~/Library/Logs/clauded`` (tests use ``tmp_path``).
    size_budget
        Hard cap on bundle size. Tail bytes get more aggressive if the
        initial assembly overshoots.
    """
    data_dir = Path(data_dir)
    out_dir = Path(out_dir) if out_dir is not None else data_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir = log_dir or _LOG_DIR

    manifest = _build_manifest(
        generated_by=generated_by, crash_context=crash_context, bot=bot,
    )

    ts = manifest["generated_at"].replace(":", "").replace("-", "")
    pid = manifest["bot_pid"]
    out_path = out_dir / f"clauded-dump-{pid}-{ts}.zip"

    # Assemble in a BytesIO so we can iterate (re-tail) before writing.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # manifest
        zf.writestr(
            "manifest.json",
            json.dumps(manifest, indent=2, ensure_ascii=False),
        )

        # env (redacted)
        env_redacted = redact.redact_env()
        env_text = "\n".join(f"{k}={v}" for k, v in sorted(env_redacted.items()))
        zf.writestr("env-redacted.txt", env_text)

        # logs/ (tailed)
        for log_name, byte_limit in (
            ("clauded.log", LOG_TAIL_BYTES),
            ("clauded.log.1", LOG_TAIL_BYTES),
            ("stream-debug.jsonl", STREAM_DEBUG_TAIL_BYTES),
        ):
            data = _tail_bytes(log_dir / log_name, byte_limit)
            if not data:
                continue
            # #224 R1 security: path-redact ALL log tails, including
            # jsonl. The earlier "would break JSON parsing" rationale
            # was wrong: substring substitution of /Users/<actual>/
            # → /Users/<user>/ is JSON-safe (path strings stay valid).
            try:
                text = data.decode("utf-8", errors="replace")
                data = redact.redact_text(text).encode("utf-8")
            except Exception:
                pass
            archive_name = f"logs/{log_name}"
            if log_name == "stream-debug.jsonl":
                archive_name = "logs/stream-debug.tail.jsonl"
            zf.writestr(archive_name, data)

        # state/
        for name in (
            "projects.json", "sessions.json", "costs.json",
            "channel_settings.json", "guild_roots.json",
        ):
            raw, found = _read_state_file(data_dir, name)
            if not found:
                continue
            zf.writestr(f"state/{name}", _redact_state_file(name, raw))

        # runtime/
        if bot is not None:
            zf.writestr(
                "runtime/sessions-live.json",
                json.dumps(
                    _snapshot_live_sessions(bot), indent=2, ensure_ascii=False,
                ),
            )
            zf.writestr(
                "runtime/bot-flags.json",
                json.dumps(_snapshot_bot_flags(bot), indent=2, ensure_ascii=False),
            )

        # #304: CLI session transcripts (tail 1 MB each)
        _collect_transcripts(zf, data_dir)

        # #224 R1 simplicity: diagnostics/info.json had 2 fields
        # (python_executable + sys_path_len); merged into manifest.json's
        # python_version/platform fields above. Directory dropped.

    # Write + size-budget check + truncate-on-overrun. Truncation strategy:
    # drop the bulky logs/ entries; keep manifest + state + runtime (the
    # high-value PM-debug data).
    data = buf.getvalue()
    if len(data) > size_budget:
        log.warning(
            "#224: bundle %d bytes exceeds budget %d; dropping logs/",
            len(data), size_budget,
        )
        return _emergency_truncate(
            buf=buf,
            out_path=out_path,
            size_budget=size_budget,
        )

    out_path.write_bytes(data)
    return out_path


def _emergency_truncate(*, buf: io.BytesIO, out_path: Path, size_budget: int) -> Path:
    """If the assembled bundle exceeds budget, drop log/ entries entirely.

    This is the last-resort path. The manifest + state are kept (they're
    the highest-value PM-debug data); logs go on a diet.
    """
    in_zip = zipfile.ZipFile(io.BytesIO(buf.getvalue()), "r")
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for info in in_zip.infolist():
            if info.filename.startswith("logs/") or info.filename.startswith("transcripts/"):
                continue  # drop the bulk
            zf.writestr(info, in_zip.read(info.filename))
        zf.writestr(
            "logs/TRUNCATED.txt",
            f"Logs omitted: bundle exceeded {size_budget} bytes.\n"
            f"Original size: {len(buf.getvalue())} bytes.\n",
        )
    data = out_buf.getvalue()
    out_path.write_bytes(data)
    return out_path
