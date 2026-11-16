"""Strict, canonical digest validation helpers."""

from __future__ import annotations

import hmac
import re


SHA256_PATTERN = re.compile(r"^sha256:[A-Fa-f0-9]{64}$")


def is_sha256_digest(value: object) -> bool:
    """Return whether a value is a full qualified SHA-256 digest."""
    return isinstance(value, str) and bool(SHA256_PATTERN.fullmatch(value))


def require_sha256_digest(value: object, field: str = "digest") -> str:
    """Validate and return a canonical lowercase SHA-256 digest."""
    if not is_sha256_digest(value):
        raise ValueError(f"{field} must be a sha256 digest")
    return str(value).lower()


def digests_equal(left: object, right: object) -> bool:
    """Compare qualified SHA-256 digests case-insensitively in constant time."""
    if not is_sha256_digest(left) or not is_sha256_digest(right):
        return False
    return hmac.compare_digest(str(left).lower(), str(right).lower())
