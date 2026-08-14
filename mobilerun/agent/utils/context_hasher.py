"""Lightweight state hashing for change detection in LLM context injection.

Used by FastAgent and ManagerAgent to skip re-injecting device_state
when the UI tree hasn't changed between consecutive turns.
"""
import hashlib


def state_hash(text: str) -> str:
    """Return a short hash of the formatted device state text.

    Uses MD5 truncated to 12 hex chars. Collision probability for
    UI tree comparison is negligible (~1/2^48 per pair).
    """
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
