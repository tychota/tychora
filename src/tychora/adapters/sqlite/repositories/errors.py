"""Typed errors the SQLite repositories and their readers share."""

from tychora.contracts.errors import TychoraError


def facts_missing() -> TychoraError:
    """A selected extraction has no persisted representation facts."""
    return TychoraError(
        "INTEGRITY_ERROR",
        "A selected extraction has no persisted representation facts.",
        reason="canonical.facts_missing",
    )
