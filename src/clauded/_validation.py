"""Shared input-validation helpers for cog/manager layers."""
from __future__ import annotations


def validate_identifier(value: str, label: str = "identifier") -> None:
    """Reject empty, whitespace-only, or control-char identifiers.

    Raises ValueError with a user-friendly message on invalid input.
    """
    if not value or not value.strip():
        raise ValueError(f"{label} must not be empty or whitespace-only")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label} must not contain newlines")


def validate_env_key(key: str) -> None:
    """Validate an environment variable key (no empty, whitespace, newline, or '=')."""
    validate_identifier(key, "env key")
    if "=" in key:
        raise ValueError("env key must not contain '='")
