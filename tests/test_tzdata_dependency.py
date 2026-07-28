"""#320: guard that the tzdata fallback dependency stays declared.

`zoneinfo.ZoneInfo` raises `ZoneInfoNotFoundError` on any host without a
system IANA tz database (Windows always; some minimal Linux containers),
which surfaced as "invalid timezone" for every `schedule_*` MCP call. The
`tzdata` PyPI package is CPython's documented fallback. A future dependency
cleanup that drops it would silently re-introduce the Windows breakage, so
pin its presence here (mirrors the #232 plist-shape guard).
"""
from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def _declared_dependencies() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text())
    return data["project"]["dependencies"]


def test_tzdata_is_a_declared_dependency() -> None:
    """#320: tzdata must be declared so zoneinfo works on tzdata-less hosts."""
    deps = _declared_dependencies()
    assert any(d.split(";")[0].strip().lower().startswith("tzdata") for d in deps), (
        f"#320: 'tzdata' must stay in project dependencies so ZoneInfo has a "
        f"fallback DB on Windows / minimal containers. Got: {deps}"
    )


def test_tzdata_actually_importable() -> None:
    """The fallback package resolves in this environment (sanity)."""
    import importlib.util

    assert importlib.util.find_spec("tzdata") is not None, (
        "tzdata not installed — run `pip install -e .` to pick up the #320 dep"
    )
