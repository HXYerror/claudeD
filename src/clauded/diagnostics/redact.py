"""#224 diagnostics — redaction primitives for the /log dump bundle.

Three responsibilities:

1. :func:`redact_env` — filter ``os.environ`` to an explicit allowlist;
   replace sensitive keys (TOKEN / SECRET / API_KEY / etc.) with
   ``len=<N> sha256=<first8>`` provenance markers.

2. :func:`redact_path` — rewrite ``/Users/<actual>/`` (or platform
   equivalent ``/home/<actual>/``) to ``/Users/<user>/`` so a bundle
   doesn't leak the operator's account name.

3. :func:`redact_text` — apply path redaction across an arbitrary text
   blob (used for tail-log redaction).

All functions are pure / synchronous / no IO. The bundle pipeline
:mod:`clauded.diagnostics.bundle` composes them.
"""
from __future__ import annotations

import hashlib
import os
import getpass
try:
    import pwd
except ImportError:  # Windows
    pwd = None  # type: ignore[assignment]
import re
from typing import Iterable

# ---------------------------------------------------------------------------
# Env allowlist
# ---------------------------------------------------------------------------

# Vars that are safe to dump verbatim.
ENV_ALLOWLIST: frozenset[str] = frozenset({
    # OS / shell basics
    "PATH", "HOME", "USER", "LANG", "TZ", "SHELL", "TERM", "PWD",
    # pytest
    "PYTEST_CURRENT_TEST",
    # clauded knobs
    "CLAUDED_STREAM_DEBUG",
    "CLAUDED_SESSION_TIMEOUT",
    "CLAUDED_BRIDGE_STOP_TIMEOUT",
    "CLAUDED_ALLOW_UNBOUND_FALLBACK",
    "CLAUDED_TESTBOT_ID",
    "CLAUDED_PROJECTS_ROOT",
    # Claude CLI / SDK knobs
    "CLAUDE_PERMISSION_MODE",
    "CLAUDE_MODEL",
})

# Prefix-allowlisted: any var starting with ``LC_`` is locale (LC_ALL,
# LC_CTYPE, ...) — safe to keep.
ENV_PREFIX_ALLOWLIST: tuple[str, ...] = ("LC_",)

# Pattern matching var names that MUST be redacted (case-insensitive).
# #224 R1 security: extended beyond the original TOKEN/SECRET/etc. to
# include OAuth-era names (AUTH, OAUTH, BEARER, JWT, COOKIE, SIGNING)
# as defense-in-depth. Default-deny on unknown keys still catches them,
# but pattern-match wins early so any future allowlist addition can't
# accidentally pass them through.
ENV_SENSITIVE_PATTERN = re.compile(
    r"TOKEN|SECRET|API_KEY|PASSWORD|PRIVATE_KEY|PASSPHRASE"
    r"|AUTH|OAUTH|BEARER|JWT|COOKIE|SIGNING|CREDENTIAL",
    re.IGNORECASE,
)


def _digest_marker(value: str) -> str:
    """Return ``len=<N> sha256=<first8>`` provenance for a redacted value.

    #224 R1 security: for short values (<= 12 chars), the
    ``len=N sha256=<first8>`` envelope is brute-forceable via rainbow
    tables (especially 4-digit PINs / short passphrases). Drop the
    sha256 prefix for short values — length alone still helps PM
    distinguish "empty" from "set" without leaking guessable hash.
    """
    if not value:
        return "len=0 empty"
    n = len(value)
    if n <= 12:
        # Short — don't ship a hash a rainbow table could invert.
        return f"len={n} <redacted-short>"
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
    return f"len={n} sha256={digest[:8]}"


def redact_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of ``env`` ready to dump to the bundle.

    - Allowlisted keys → verbatim value (with path redaction on path-shape vars)
    - Keys matching ``ENV_SENSITIVE_PATTERN`` → ``len=N sha256=...``
    - Everything else → omitted entirely (keep the bundle small, avoid
      leaking app-internal env we haven't audited)

    #224 R1 security: ``HOME`` / ``USER`` / ``PWD`` carry the operator's
    actual username ("/Users/alice", "alice"). Apply the same path-redact
    treatment to their values so the bundle doesn't leak the account name.
    """
    if env is None:
        env = dict(os.environ)
    username = _current_username()
    out: dict[str, str] = {}
    for key, value in env.items():
        # Sensitive override always wins (TOKEN-named keys never leak even
        # if also somehow allowlisted).
        if ENV_SENSITIVE_PATTERN.search(key):
            out[key] = _digest_marker(value)
            continue
        if key in ENV_ALLOWLIST:
            # #224 R1 security: path-redact the value for path-shape vars
            # so the username doesn't leak via env-redacted.txt.
            if key in ("HOME", "PWD"):
                out[key] = redact_path(value, username=username)
            elif key == "USER":
                out[key] = "<user>" if value == username else value
            else:
                out[key] = value
            continue
        if any(key.startswith(p) for p in ENV_PREFIX_ALLOWLIST):
            out[key] = value
            continue
        # Drop silently.
    return out


# ---------------------------------------------------------------------------
# Path redaction
# ---------------------------------------------------------------------------


def _current_username() -> str:
    """Best-effort: get the OS account name. Falls back to ``$USER`` or 'user'."""
    try:
        if pwd is not None:
            return pwd.getpwuid(os.getuid()).pw_name
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "user") or "user"


def redact_path(path: str, *, username: str | None = None) -> str:
    """Rewrite ``/Users/<actual>/foo`` → ``/Users/<user>/foo``.

    Handles macOS (``/Users``) + Linux (``/home``) prefixes. Leaves
    paths that don't contain a per-user component untouched.
    """
    if not path:
        return path
    if username is None:
        username = _current_username()
    # Use re.sub so we catch both ``/Users/alice/`` and ``/Users/alice``
    # (no trailing) without breaking on the platform's path-sep variants.
    for prefix in ("/Users/", "/home/"):
        path = re.sub(
            rf"{re.escape(prefix)}{re.escape(username)}([/\\]|$)",
            rf"{prefix}<user>\1",
            path,
        )
    # Windows path (case-insensitive, backslash or forward slash)
    for win_prefix in ("C:\\Users\\", "C:/Users/"):
        path = re.sub(
            re.escape(win_prefix) + re.escape(username) + r"([/\\]|$)",
            win_prefix.replace("\\", "/") + r"<user>\1",
            path,
            flags=re.IGNORECASE,
        )
    return path


def redact_text(text: str, *, username: str | None = None) -> str:
    """Apply path redaction across an arbitrary text blob.

    Used for tail-log redaction. Cheap regex sweep; doesn't try to be
    clever about partial matches or escaped paths.
    """
    if not text:
        return text
    if username is None:
        username = _current_username()
    for prefix in ("/Users/", "/home/"):
        text = re.sub(
            rf"{re.escape(prefix)}{re.escape(username)}([/\\]|\b)",
            rf"{prefix}<user>\1",
            text,
        )
    # Windows paths (#289)
    for win_prefix in ("C:\\Users\\", "C:/Users/"):
        text = re.sub(
            re.escape(win_prefix) + re.escape(username) + r"([/\\]|\b)",
            win_prefix.replace("\\", "/") + r"<user>\1",
            text,
            flags=re.IGNORECASE,
        )
    return text


# ---------------------------------------------------------------------------
# JSON-shape redactors
# ---------------------------------------------------------------------------


def redact_projects_json(data: dict, *, username: str | None = None) -> dict:
    """Walk a parsed ``projects.json`` dict and redact filesystem paths.

    Schema (per ``project_manager.py``): map of ``binding_id -> {path: ..., ...}``.
    """
    if username is None:
        username = _current_username()
    out: dict = {}
    for k, v in data.items():
        if isinstance(v, dict):
            redacted_v = dict(v)
            for path_key in ("path", "system_prompt_path"):
                if path_key in redacted_v and isinstance(redacted_v[path_key], str):
                    redacted_v[path_key] = redact_path(redacted_v[path_key], username=username)
            # review E1: extra directories are persisted under ``extra_dirs``
            # (see ProjectManager.add_extra_dir / get_extra_dirs). The old
            # code redacted ``add_dirs`` — a key projects.json never uses — so
            # the absolute paths from ``/project add-dir`` (which embed the
            # operator's username) leaked unredacted into every /log dump.
            # Redact both: ``extra_dirs`` is the live key, ``add_dirs`` kept
            # defensively for any older on-disk schema.
            for dirs_key in ("extra_dirs", "add_dirs"):
                if dirs_key in redacted_v and isinstance(redacted_v[dirs_key], list):
                    redacted_v[dirs_key] = [
                        redact_path(p, username=username) if isinstance(p, str) else p
                        for p in redacted_v[dirs_key]
                    ]
            out[k] = redacted_v
        else:
            out[k] = v
    return out


def redact_sessions_json(data: dict, *, username: str | None = None) -> dict:
    """Walk a parsed ``sessions.json`` dict and redact paths + system prompt.

    Schema: map of ``thread_id -> {session_id, project_path, ..., system_prompt?}``.
    """
    if username is None:
        username = _current_username()
    out: dict = {}
    for k, v in data.items():
        if isinstance(v, dict):
            redacted_v = dict(v)
            if "project_path" in redacted_v and isinstance(redacted_v["project_path"], str):
                redacted_v["project_path"] = redact_path(redacted_v["project_path"], username=username)
            # system_prompt is sensitive — replace verbatim text with provenance marker.
            if "system_prompt" in redacted_v and isinstance(redacted_v["system_prompt"], str):
                redacted_v["system_prompt"] = _digest_marker(redacted_v["system_prompt"])
            out[k] = redacted_v
        else:
            out[k] = v
    return out
