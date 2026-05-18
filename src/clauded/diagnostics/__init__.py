"""#224 diagnostics package — /log dump bundle generator.

Public API:

- :mod:`clauded.diagnostics.redact` — pure-function redaction primitives
- :mod:`clauded.diagnostics.bundle` — :func:`generate_bundle` end-to-end
"""
from . import redact, bundle  # noqa: F401
