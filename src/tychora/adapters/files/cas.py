"""Atomic verified CAS with owner-process guards and payload leases.

All canonical readers and writers run in the sole workspace owner. Deletion
services must hold payload_guard through their reference/lease recheck and
unlink; pins remain held until the actual filesystem worker exits.
"""

import os
import re
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import BinaryIO
from uuid import uuid4
from weakref import WeakValueDictionary

from tychora.contracts.artifacts import ArtifactRef, ArtifactStat
from tychora.contracts.errors import TychoraError

_BLOCK_SIZE = 1024 * 1024


@dataclass
class _Guard:
    lock: RLock = field(default_factory=RLock)
    users: int = 0


@dataclass
class _RootState:
    lock: RLock = field(default_factory=RLock)
    guards: dict[str, _Guard] = field(default_factory=dict)
    pins: dict[str, int] = field(default_factory=dict)


_STATES: WeakValueDictionary[tuple[int, int], _RootState] = WeakValueDictionary()
_STATES_LOCK = RLock()


def _check_sha(sha: str) -> None:
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
        raise TychoraError("INTEGRITY_ERROR", "Invalid artifact SHA-256")


class LocalArtifacts:
    def __init__(self, root: Path, *, max_size_bytes: int = 1024**3) -> None:
        if type(max_size_bytes) is not int or max_size_bytes < 0:
            raise ValueError("Artifact size limit must be a nonnegative integer")
        self.root = root.absolute()
        self.max_size_bytes = max_size_bytes
        with self._directory(create=True) as directory:
            info = os.fstat(directory)
            self._identity = (info.st_dev, info.st_ino)
        with _STATES_LOCK:
            state = _STATES.get(self._identity)
            if state is None:
                state = _RootState()
                _STATES[self._identity] = state
            self._state = state

    @contextmanager
    def _directory(self, *, create: bool = False) -> Iterator[int]:
        """Walk every component without following symlinks; operate via dirfd."""
        if ".." in self.root.parts:
            raise TychoraError("INTEGRITY_ERROR", "Invalid artifact root")
        fd = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.root.parts[1:]:
                try:
                    next_fd = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    os.fsync(fd)
                    next_fd = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                os.close(fd)
                fd = next_fd
            if not create:
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != self._identity:
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Artifact root identity changed"
                    )
        except OSError:
            os.close(fd)
            raise TychoraError(
                "INTEGRITY_ERROR", "Artifact root is unavailable or unsafe"
            ) from None
        except BaseException:
            os.close(fd)
            raise
        try:
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def payload_guard(self, sha: str) -> Iterator[None]:
        """Serialize publication/binding and deletion for this SHA in the owner."""
        _check_sha(sha)
        with self._state.lock:
            guard = self._state.guards.setdefault(sha, _Guard())
            guard.users += 1
        try:
            with guard.lock:
                yield
        finally:
            with self._state.lock:
                guard.users -= 1
                if not guard.users:
                    del self._state.guards[sha]

    @contextmanager
    def pin_payloads(self, shas: frozenset[str]) -> Iterator[None]:
        for sha in shas:
            _check_sha(sha)
        with ExitStack() as guards:
            for sha in sorted(shas):
                guards.enter_context(self.payload_guard(sha))
            with self._state.lock:
                for sha in shas:
                    self._state.pins[sha] = self._state.pins.get(sha, 0) + 1
        try:
            yield
        finally:
            with self._state.lock:
                for sha in shas:
                    remaining = self._state.pins[sha] - 1
                    if remaining:
                        self._state.pins[sha] = remaining
                    else:
                        del self._state.pins[sha]

    def payload_is_pinned(self, sha: str) -> bool:
        _check_sha(sha)
        with self._state.lock:
            return self._state.pins.get(sha, 0) > 0

    def put_stream(self, stream: BinaryIO, media_type: str) -> ArtifactRef:
        staging = ".staging-" + uuid4().hex
        with self._directory() as directory:
            try:
                fd = os.open(
                    staging,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                digest, size = sha256(), 0
                with os.fdopen(fd, "wb") as out:
                    while block := stream.read(_BLOCK_SIZE):
                        size += len(block)
                        if size > self.max_size_bytes:
                            raise TychoraError(
                                "INVALID_REQUEST", "Artifact exceeds size limit"
                            )
                        digest.update(block)
                        out.write(block)
                    out.flush()
                    os.fsync(out.fileno())
                ref = ArtifactRef(
                    sha256=digest.hexdigest(),
                    size_bytes=size,
                    media_type=media_type,
                    storage_key=digest.hexdigest(),
                )
                with self.payload_guard(ref.sha256):
                    try:
                        with self._verified(directory, ref) as existing:
                            os.fsync(existing.fileno())
                    except FileNotFoundError:
                        os.replace(
                            staging,
                            ref.storage_key,
                            src_dir_fd=directory,
                            dst_dir_fd=directory,
                        )
                    os.fsync(directory)
                return ref
            finally:
                try:
                    os.unlink(staging, dir_fd=directory)
                except FileNotFoundError:
                    pass

    @contextmanager
    def _verified(self, directory: int, ref: ArtifactRef) -> Iterator[BinaryIO]:
        _check_sha(ref.sha256)
        if ref.storage_key != ref.sha256:
            raise TychoraError(
                "INTEGRITY_ERROR", "Artifact storage key must match its hash"
            )
        try:
            fd = os.open(
                ref.storage_key,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
        except FileNotFoundError:
            raise
        except OSError:
            raise TychoraError("INTEGRITY_ERROR", "Artifact path is unsafe") from None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != ref.size_bytes:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Artifact payload failed verification"
                )
            digest, size = sha256(), 0
            while block := stream.read(_BLOCK_SIZE):
                size += len(block)
                if size > ref.size_bytes:
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Artifact payload failed verification"
                    )
                digest.update(block)
            if digest.hexdigest() != ref.sha256 or size != ref.size_bytes:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Artifact payload failed verification"
                )
            stream.seek(0)
            yield stream

    @contextmanager
    def open_verified(self, ref: ArtifactRef) -> Iterator[BinaryIO]:
        with (
            self.pin_payloads(frozenset({ref.sha256})),
            self._directory() as directory,
            ExitStack() as stack,
        ):
            try:
                stream = stack.enter_context(self._verified(directory, ref))
            except FileNotFoundError:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Required artifact payload is missing"
                ) from None
            yield stream

    def stat(self, ref: ArtifactRef) -> ArtifactStat:
        with self.pin_payloads(frozenset({ref.sha256})), self._directory() as directory:
            try:
                with self._verified(directory, ref):
                    pass
            except FileNotFoundError:
                return ArtifactStat(**ref.model_dump(), available=False)
            return ArtifactStat(
                **ref.model_dump(), available=True, payload_state="present"
            )
