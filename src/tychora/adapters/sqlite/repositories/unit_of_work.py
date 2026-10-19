"""Read and write transactions over one SQLite connection."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from tychora.contracts.errors import TychoraError
from tychora.ports.repositories import (
    ArtifactRepository,
    BatchRepository,
    FamilyRepository,
    ReadTransaction,
    SnapshotRepository,
    WorkspaceRepository,
    WriteTransaction,
)

from .artifacts import SqliteArtifactRepository
from .batches import SqliteBatchRepository
from .families import SqliteFamilyRepository
from .snapshots import SqliteSnapshotRepository
from .workspace import SqliteWorkspaceRepository


@contextmanager
def typed_storage_errors() -> Iterator[None]:
    """Report a SQLite failure, a busy database included, as a typed error."""
    try:
        yield
    except sqlite3.Error as error:
        raise TychoraError(
            "INTEGRITY_ERROR",
            f"Storage operation failed: {type(error).__name__}",
            reason="invariant.unexpected",
        ) from error


class SqliteTransaction:
    """Repositories bound to the connection's current transaction."""

    workspace: WorkspaceRepository
    artifacts: ArtifactRepository
    families: FamilyRepository
    batches: BatchRepository
    snapshots: SnapshotRepository

    def __init__(self, db: sqlite3.Connection, *, writable: bool) -> None:
        self.workspace = SqliteWorkspaceRepository(db, writable=writable)
        self.artifacts = SqliteArtifactRepository(db, writable=writable)
        self.families = SqliteFamilyRepository(db, writable=writable)
        self.batches = SqliteBatchRepository(db, writable=writable)
        self.snapshots = SqliteSnapshotRepository(db, writable=writable)


class SqliteUnitOfWork:
    """Opens transactions on the owner's connection; the owner thread calls it."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    @contextmanager
    def read(self) -> Iterator[ReadTransaction]:
        """Join the open transaction, or read inside a deferred one and roll back."""
        if self._db.in_transaction:
            yield SqliteTransaction(self._db, writable=False)
            return
        self._db.execute("BEGIN")
        try:
            yield SqliteTransaction(self._db, writable=False)
        finally:
            self._db.rollback()

    @contextmanager
    def write(self) -> Iterator[WriteTransaction]:
        """Run one immediate transaction; commit on success, roll back otherwise."""
        if self._db.in_transaction:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "A write transaction cannot nest in another transaction.",
                reason="invariant.unexpected",
            )
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield SqliteTransaction(self._db, writable=True)
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
