"""Private, verified directory archive for canonical SQLite and CAS payloads."""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, Self
from uuid import uuid4

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import JsonValue, canonical_bytes
from tychora.ports.execution import ExecutionContext


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_exclusive(source: Path, destination: Path) -> None:
    """Atomic directory publication with a kernel-enforced no-replace rule."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        result = rename(os.fsencode(source), os.fsencode(destination), 0x4)
    elif sys.platform.startswith("linux"):
        rename = libc.renameat2
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        result = rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    else:
        raise TychoraError(
            "CAPABILITY_MISSING", "Atomic exclusive archive publication is unavailable"
        )
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise TychoraError("INVALID_REQUEST", "Backup destination already exists")
        raise OSError(error, os.strerror(error))


@contextmanager
def _private_file(path: Path) -> Iterator[BinaryIO]:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        yield stream


class ArchiveWriter(AbstractContextManager["ArchiveWriter"]):
    def __init__(self, destination: Path) -> None:
        self.destination = destination.absolute()
        self.stage = self.destination.parent / (".backup-staging-" + uuid4().hex)
        self.published = False

    def __enter__(self) -> Self:
        if self.destination.exists() or self.destination.is_symlink():
            raise TychoraError("INVALID_REQUEST", "Backup destination already exists")
        if not self.destination.parent.is_dir():
            raise TychoraError("INVALID_REQUEST", "Backup parent directory is missing")
        self.stage.mkdir(mode=0o700)
        ready = False
        try:
            (self.stage / "artifacts").mkdir(mode=0o700)
            ready = True
        finally:
            if not ready:
                shutil.rmtree(self.stage, ignore_errors=True)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if not self.published:
            shutil.rmtree(self.stage, ignore_errors=True)

    def copy_artifact(self, source, ref: ArtifactRef, ctx: ExecutionContext) -> None:
        target = self.stage / "artifacts" / ref.sha256
        digest, count = sha256(), 0
        with source.open_verified(ref) as incoming, _private_file(target) as outgoing:
            while block := incoming.read(1024 * 1024):
                ctx.checkpoint()
                count += len(block)
                digest.update(block)
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if count != ref.size_bytes or digest.hexdigest() != ref.sha256:
            raise TychoraError("INTEGRITY_ERROR", "Copied artifact failed verification")

    def write_json(self, name: str, value: JsonValue) -> str:
        raw = canonical_bytes(value)
        with _private_file(self.stage / name) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return sha256(raw).hexdigest()

    def finish(self) -> None:
        _sync_directory(self.stage / "artifacts")
        _sync_directory(self.stage)
        _publish_exclusive(self.stage, self.destination)
        self.published = True
        _sync_directory(self.destination.parent)
