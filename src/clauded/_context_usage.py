"""#263 — compute global context occupancy from Free space supplement.

Shared by ``discord_renderer._compute`` (footer 🧠) and
``cogs/context._build_context_embed`` (``/context`` title + progress).
"""
from __future__ import annotations

_FREE_SPACE_NAMES = frozenset({"Free space", "Available", "Remaining"})


def compute_global_context_pct(
    cu: dict | None,
    max_tokens_override: int | float | None = None,
) -> tuple[float, float, float] | None:
    """Return ``(global_used, max_tokens, percentage)`` or ``None``.

    Uses the **Free space supplement** (``maxTokens - free_space``) to
    derive true buffer occupancy.  ``totalTokens`` is only the last-turn
    input footprint and is used as a fallback when categories are absent.

    Returns ``None`` when the input is missing or unparseable.

    Parameters
    ----------
    cu:
        The raw ``ContextUsageResponse`` dict from the SDK.
    max_tokens_override:
        #280 — When the user runs ``/model switch opus`` mid-session,
        the SDK keeps returning the previous model's ``maxTokens`` (e.g.
        sonnet's 200k) until the next turn refreshes its metadata.
        Callers that know the *real* window (e.g. via ``KNOWN_MODELS``
        lookup cached on the bridge) can pass it here; when it differs
        from ``cu["maxTokens"]`` the supplement and percentage are
        recomputed against the override. We still derive ``global_used``
        from the SDK's stale ``maxTokens`` ↔ ``free_space`` pair (those
        two are internally consistent) so the *used* number stays
        truthful — only the denominator (and resulting percentage)
        switches to the override. Pass ``None`` (default) to preserve
        legacy behaviour.
    """
    if cu is None:
        return None

    sdk_max = cu.get("maxTokens")
    if not isinstance(sdk_max, (int, float)) or sdk_max <= 0:
        # No maxTokens → try SDK percentage as last resort
        if "percentage" not in cu:
            return None
        try:
            pct = float(cu["percentage"])
            return (0.0, 0.0, pct)
        except (TypeError, ValueError):
            return None

    sdk_max = float(sdk_max)

    # #280: use override iff it's valid AND differs from the SDK's value.
    # When they match (post-refresh) keep SDK's value to avoid silently
    # diverging once the SDK catches up.
    if (
        max_tokens_override is not None
        and isinstance(max_tokens_override, (int, float))
        and max_tokens_override > 0
        and float(max_tokens_override) != sdk_max
    ):
        effective_max = float(max_tokens_override)
    else:
        effective_max = sdk_max

    # Primary: Free space supplement. ``free_space`` is reported by the
    # SDK against its own (possibly stale) ``maxTokens``, so we compute
    # ``global_used`` against ``sdk_max`` (the matching pair) then
    # express the percentage against ``effective_max``.
    categories = cu.get("categories") or []
    free_space = next(
        (c["tokens"] for c in categories
         if isinstance(c, dict) and c.get("name") in _FREE_SPACE_NAMES),
        None,
    )
    if free_space is not None and isinstance(free_space, (int, float)):
        global_used = max(0.0, sdk_max - float(free_space))
        return (global_used, effective_max, (global_used / effective_max) * 100.0)

    # Fallback: totalTokens (pre-#263 behaviour)
    total = cu.get("totalTokens")
    if isinstance(total, (int, float)):
        return (float(total), effective_max, (float(total) / effective_max) * 100.0)

    # Last resort: SDK percentage
    if "percentage" in cu:
        try:
            return (0.0, effective_max, float(cu["percentage"]))
        except (TypeError, ValueError):
            pass

    return None
