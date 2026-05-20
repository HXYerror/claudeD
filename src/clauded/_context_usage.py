"""#263 — compute global context occupancy from Free space supplement.

Shared by ``discord_renderer._compute`` (footer 🧠) and
``cogs/context._build_context_embed`` (``/context`` title + progress).
"""
from __future__ import annotations

_FREE_SPACE_NAMES = frozenset({"Free space", "Available", "Remaining"})


def compute_global_context_pct(
    cu: dict | None,
) -> tuple[float, float, float] | None:
    """Return ``(global_used, max_tokens, percentage)`` or ``None``.

    Uses the **Free space supplement** (``maxTokens - free_space``) to
    derive true buffer occupancy.  ``totalTokens`` is only the last-turn
    input footprint and is used as a fallback when categories are absent.

    Returns ``None`` when the input is missing or unparseable.
    """
    if cu is None:
        return None

    max_t = cu.get("maxTokens")
    if not isinstance(max_t, (int, float)) or max_t <= 0:
        # No maxTokens → try SDK percentage as last resort
        if "percentage" not in cu:
            return None
        try:
            pct = float(cu["percentage"])
            return (0.0, 0.0, pct)
        except (TypeError, ValueError):
            return None

    max_t = float(max_t)

    # Primary: Free space supplement
    categories = cu.get("categories") or []
    free_space = next(
        (c["tokens"] for c in categories
         if isinstance(c, dict) and c.get("name") in _FREE_SPACE_NAMES),
        None,
    )
    if free_space is not None and isinstance(free_space, (int, float)):
        global_used = max(0.0, max_t - float(free_space))
        return (global_used, max_t, (global_used / max_t) * 100.0)

    # Fallback: totalTokens (pre-#263 behaviour)
    total = cu.get("totalTokens")
    if isinstance(total, (int, float)):
        return (float(total), max_t, (float(total) / max_t) * 100.0)

    # Last resort: SDK percentage
    if "percentage" in cu:
        try:
            return (0.0, max_t, float(cu["percentage"]))
        except (TypeError, ValueError):
            pass

    return None
