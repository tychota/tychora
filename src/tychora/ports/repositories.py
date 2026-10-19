"""Store-agnostic repository ports and the keyset cursor they page with.

Ports speak in contracts, identifiers, scalars, tuples and cursors. Each store
adapter binds lists and transactions its own way, so a PostgreSQL adapter can
implement the same protocols as the SQLite one.
"""

from __future__ import annotations

import base64
import json
from typing import Protocol

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import Contract, NonEmptyStr
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.snapshots import IndexFamily

MAX_CURSOR_CHARS = 1024


def _invalid_cursor() -> TychoraError:
    return TychoraError(
        "INVALID_REQUEST", "Cursor is invalid.", reason="cursor.invalid"
    )


class Cursor(Contract):
    """Keyset position: the sort key of the last returned row and its ID."""

    sort_key: tuple[str | int, ...]
    last_id: NonEmptyStr

    def encode(self) -> str:
        """Return an opaque URL-safe token for this position."""
        raw = canonical_bytes({"id": self.last_id, "k": list(self.sort_key)})
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, token: str) -> Cursor:
        """Parse a token produced by ``encode``; anything else is refused."""
        try:
            if not isinstance(token, str) or not 0 < len(token) <= MAX_CURSOR_CHARS:
                raise ValueError("cursor length")
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {"id", "k"}:
                raise ValueError("cursor shape")
            if not isinstance(value["k"], list):
                raise TypeError("cursor key")
            cursor = cls(sort_key=tuple(value["k"]), last_id=value["id"])
            if cursor.encode() != token:
                raise ValueError("cursor is not canonical")
            return cursor
        except (ValueError, TypeError):
            raise _invalid_cursor() from None


class WorkspaceRepository(Protocol):
    """The single workspace row: identity, epochs and decision watermark (read side)."""

    def workspace_id(self) -> str:
        """Return the workspace identifier."""
        ...

    def policy_epoch(self) -> int:
        """Return the current policy epoch."""
        ...

    def lexical_epoch(self) -> int:
        """Return the current lexical statistics epoch."""
        ...

    def decision_watermark(self) -> int:
        """Return the highest recorded decision sequence number."""
        ...


class ArtifactRepository(Protocol):
    """Registered content-addressed artifacts."""

    def get(self, sha256: str) -> ArtifactRef | None:
        """Return the registered artifact, or ``None`` when absent."""
        ...

    def register(self, ref: ArtifactRef) -> bool:
        """Register an artifact; return whether it was newly added."""
        ...


class FamilyRepository(Protocol):
    """Index families and the creation nonces bound into their identifiers."""

    def get(self, family_id: str) -> IndexFamily | None:
        """Return the stored family, or ``None`` when absent."""
        ...

    def nonce(self, channel: str, identity_sha: str) -> str | None:
        """Return the nonce of the earliest family created for this identity."""
        ...

    def create(self, family: IndexFamily, nonce: str, identity_sha: str) -> None:
        """Store a family once; an identical repeat is a no-op, a different row fails."""
        ...
