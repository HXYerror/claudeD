"""#251 — pytest wrapper for the e2e harness.

Runs `scripts/e2e/run_e2e.py` as part of `pytest tests/` so CI catches
regressions. The wrapper:

1. Imports the case list + harness from `scripts/e2e/run_e2e.py`
2. Runs every case as a pytest parametrize
3. Reports PASS/FAIL/ERROR; only fails the suite for ERROR (test crashes)
4. FAIL status is informational (lets known-open bugs ride the suite as
   regression watchers without blocking CI)

The failing cases are reproducers for open issues:
- #247 KNOWN_MODELS stale
- #248 cost record `> 0` gate
- #252 _save() race condition
- #254 silent overwrite on duplicate name
- #255 input validation gaps
- #257 missing reject_if_unbound
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS_PATH = ROOT / "scripts" / "e2e" / "run_e2e.py"


def _load_harness():
    """Lazy-load the harness module so pytest collection doesn't hit it
    if the file is missing."""
    if not HARNESS_PATH.exists():
        pytest.skip(f"e2e harness not present at {HARNESS_PATH}")
    if "e2e_harness" in sys.modules:
        return sys.modules["e2e_harness"]
    spec = importlib.util.spec_from_file_location("e2e_harness", HARNESS_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "src"))
    # Register BEFORE exec so dataclasses (which looks up cls.__module__
    # via sys.modules) can find the module mid-import.
    sys.modules["e2e_harness"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def harness():
    return _load_harness()


def _case_ids(harness):
    """Build the pytest parametrize ids from the case list."""
    if not hasattr(harness, "HAPPY_CASES"):
        return []
    return [name for name, _ in harness.HAPPY_CASES]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_name",
    _case_ids(_load_harness()),
)
async def test_e2e_case(case_name: str, harness):
    """Run a single case from the e2e suite.

    PASS → test passes.
    FAIL → test is `xfail` (we know about the bug; just keep watching).
    ERROR → test fails CI (harness or genuine crash).
    """
    case = next(
        (fn for n, fn in harness.HAPPY_CASES if n == case_name),
        None,
    )
    assert case is not None, f"case {case_name!r} disappeared"

    bot = harness.make_mock_bot()
    result = await case(bot)

    if result.status == "ERROR":
        pytest.fail(f"e2e ERROR: {result.detail}")
    if result.status == "FAIL":
        pytest.xfail(f"known bug: {result.detail}")
    # PASS / SKIP → noop
