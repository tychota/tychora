"""Read an existing database without writing any file next to it."""

import fcntl
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from tychora.contracts.errors import TychoraError

_RECEIPT = "SELECT version, checksum FROM schema_migrations ORDER BY version"
_HAS_RECEIPT = (
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
)


def require_no_live_owner(root: Path, message: str) -> None:
    """Refuse with WORKSPACE_BUSY when another process holds the owner lock.

    The lock file is probed only when it already exists, so nothing is created
    in the workspace, and the probe releases the lock at once when it is free.
    """
    try:
        fd = os.open(root / ".runtime" / "owner.lock", os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TychoraError("WORKSPACE_BUSY", message, reason="owner.live") from None
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _stamp(path: Path) -> tuple[int, int] | None:
    """Return the size and modification time of a file, or None when it is absent."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_size, info.st_mtime_ns


def _copy_to(scratch: str, path: Path, wal: Path) -> Path:
    """Copy the database and its -wal into a directory and return the copy."""
    copy = Path(scratch) / path.name
    shutil.copyfile(path, copy)
    if wal.exists():
        shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
    return copy


def database_invalid() -> TychoraError:
    """Return the refusal for a database that cannot be read safely."""
    return TychoraError(
        "INTEGRITY_ERROR",
        "The workspace database cannot be inspected.",
        reason="workspace.database_invalid",
    )


def _connect_untouched(path: Path, stack: ExitStack) -> sqlite3.Connection:
    """Open the database read-only, through a private copy when sidecars exist."""
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    if wal.exists() or shm.exists():
        scratch = stack.enter_context(
            tempfile.TemporaryDirectory(prefix="tychora-untouched-")
        )
        before = (_stamp(path), _stamp(wal))
        copy = _copy_to(scratch, path, wal)
        if (_stamp(path), _stamp(wal)) != before:
            raise OSError("The database changed while it was copied")
        uri = copy.as_uri() + "?mode=ro"
    else:
        uri = path.as_uri() + "?mode=ro&immutable=1"
    db = sqlite3.connect(uri, uri=True)
    stack.callback(db.close)
    # Reading the schema here refuses a file that is not a database at open time.
    db.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return db


@contextmanager
def open_untouched(path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a read-only connection that sees every commit and writes nothing.

    A database with no -wal or -shm is opened read-only and immutable. When
    either exists, a crashed owner may have left committed pages in the -wal and
    even a read-only open would rewrite the -shm or create the missing sidecar,
    so the database and its -wal are copied into a private directory and the
    copy is read. No concurrent writer is assumed: the files are compared before
    and after the copy and the read is refused when either changed. A failure to
    copy or open refuses with a typed error; the caller never falls back to a
    read-write open. Errors raised by the caller's own reads are left to it.
    """
    with ExitStack() as stack:
        try:
            db = _connect_untouched(path, stack)
        except (OSError, sqlite3.DatabaseError):
            raise database_invalid() from None
        yield db


def read_receipt_untouched(path: Path) -> list[tuple[int, str]]:
    """Return the migration receipt rows of an existing database, leaving it as is."""
    with open_untouched(path) as db:
        try:
            if not db.execute(_HAS_RECEIPT).fetchone():
                return []
            return db.execute(_RECEIPT).fetchall()
        except sqlite3.DatabaseError:
            raise database_invalid() from None
