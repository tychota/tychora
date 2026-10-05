"""Private, verified directory archive for canonical SQLite and CAS payloads."""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import stat
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


def _open_directory_nofollow(path: Path) -> int:
    absolute = path.absolute()
    if not absolute.is_absolute() or any(
        part in {".", ".."} for part in absolute.parts
    ):
        raise TychoraError("INTEGRITY_ERROR", "Unsafe archive directory path")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            following = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = following
        return directory
    except OSError:
        os.close(directory)
        raise TychoraError("INTEGRITY_ERROR", "Unsafe archive directory path") from None


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


class ArchiveReader:
    """Copy a v1 directory archive without following or accepting extra members."""

    def __init__(
        self,
        source: Path,
        *,
        max_entries: int = 100_000,
        max_expanded_bytes: int = 20 * 1024**3,
    ) -> None:
        self.source = source.absolute()
        self.max_entries = max_entries
        self.max_expanded_bytes = max_expanded_bytes

    def copy_to(self, stage: Path, ctx: ExecutionContext) -> frozenset[str]:
        if self.max_entries < 4 or self.max_expanded_bytes < 1:
            raise TychoraError("INVALID_REQUEST", "Archive limits are too small")
        root = _open_directory_nofollow(self.source)
        copied: set[str] = set()
        count = 0
        total = 0
        try:
            with os.scandir(root) as entries:
                root_names = {entry.name for entry in entries}
            required_root = {
                "corpus.sqlite3",
                "backup-manifest.json",
                "backup-receipt.json",
                "artifacts",
            }
            if root_names not in (
                required_root,
                required_root | {"resolved-config.json"},
            ):
                raise TychoraError("INTEGRITY_ERROR", "Archive members differ")
            artifact_dir = os.open(
                "artifacts", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
            )
            try:
                with os.scandir(artifact_dir) as entries:
                    artifacts = [entry.name for entry in entries]
                if len(artifacts) != len(set(artifacts)):
                    raise TychoraError("INTEGRITY_ERROR", "Duplicate archive member")
                for name in artifacts:
                    if len(name) != 64 or any(
                        c not in "0123456789abcdef" for c in name
                    ):
                        raise TychoraError("INTEGRITY_ERROR", "Unsafe artifact name")
                members = [
                    (root, "corpus.sqlite3", stage / "corpus.sqlite3"),
                    (root, "backup-manifest.json", stage / "backup-manifest.json"),
                    (root, "backup-receipt.json", stage / "backup-receipt.json"),
                    *(
                        (root, "resolved-config.json", stage / "resolved-config.json")
                        for _ in range("resolved-config.json" in root_names)
                    ),
                    *(
                        (artifact_dir, name, stage / "artifacts" / name)
                        for name in sorted(artifacts)
                    ),
                ]
                for directory, name, target in members:
                    ctx.checkpoint()
                    count += 1
                    if count > self.max_entries:
                        raise TychoraError(
                            "INTEGRITY_ERROR", "Archive entry limit exceeded"
                        )
                    try:
                        incoming_fd = os.open(
                            name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory
                        )
                    except OSError:
                        raise TychoraError(
                            "INTEGRITY_ERROR", "Unsafe archive member"
                        ) from None
                    try:
                        info = os.fstat(incoming_fd)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise TychoraError(
                                "INTEGRITY_ERROR", "Unsafe archive member"
                            )
                        total += info.st_size
                        if total > self.max_expanded_bytes:
                            raise TychoraError(
                                "INTEGRITY_ERROR", "Archive byte limit exceeded"
                            )
                        with (
                            os.fdopen(os.dup(incoming_fd), "rb") as incoming,
                            _private_file(target) as outgoing,
                        ):
                            remaining = info.st_size
                            while remaining:
                                block = incoming.read(min(1024 * 1024, remaining))
                                if not block:
                                    raise TychoraError(
                                        "INTEGRITY_ERROR", "Archive member changed"
                                    )
                                outgoing.write(block)
                                remaining -= len(block)
                                ctx.checkpoint()
                            if incoming.read(1):
                                raise TychoraError(
                                    "INTEGRITY_ERROR", "Archive member changed"
                                )
                            outgoing.flush()
                            os.fsync(outgoing.fileno())
                        copied.add(name if directory == root else f"artifacts/{name}")
                    finally:
                        os.close(incoming_fd)
            finally:
                os.close(artifact_dir)
        finally:
            os.close(root)
        return frozenset(copied)
