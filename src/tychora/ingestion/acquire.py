"""Bounded immutable acquisition; callers persist capture before any parser stage."""

import os
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryFile
from typing import Any, BinaryIO

from tychora.adapters.code.capture import RepositoryCapture
from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.files.references import MAX_MANIFEST_BYTES, required_refs
from tychora.configuration.models import AcquisitionCeilings, ResolvedConfig
from tychora.contracts import SourceInput
from tychora.contracts.acquisition import CapturedFile, CaptureManifest
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes, digest, normalize_source_key
from tychora.contracts.requests import (
    BytesSource,
    FileSource,
    FolderSource,
    RepositorySource,
    TextSource,
    UploadSource,
)
from tychora.ports.acquisition import UrlReader
from tychora.ports.execution import ExecutionContext


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _limit() -> TychoraError:
    return TychoraError(
        "INVALID_REQUEST",
        "Capture exceeds acquisition or manifest capacity; reduce input or lower batch size",
    )


class Acquirer:
    def __init__(
        self,
        artifacts: LocalArtifacts,
        config: ResolvedConfig,
        *,
        urls: UrlReader | None = None,
    ):
        self.artifacts = artifacts
        self.config = config
        self.urls = urls
        self._repositories = RepositoryCapture(artifacts)
        ceiling = config.values.acquisition.limits
        if (
            max(ceiling.max_file_bytes, ceiling.max_inline_bytes)
            > artifacts.max_size_bytes
        ):
            raise TychoraError(
                "INVALID_REQUEST",
                "Acquisition byte ceiling exceeds artifact storage capacity",
            )
        try:
            self._roots = {
                Path(root): _stamp(Path(root).stat(follow_symlinks=False))[:2]
                for root in config.values.acquisition.roots
            }
        except OSError:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Registered acquisition root is unavailable"
            ) from None

    def _limits(self, source: SourceInput) -> AcquisitionCeilings:
        limits = self.config.values.acquisition.limits.model_dump()
        if source.root.acquisition_limits is not None:
            for name, value in source.root.acquisition_limits.model_dump(
                exclude_unset=True
            ).items():
                if value is None or value > limits[name]:
                    raise TychoraError(
                        "INVALID_REQUEST",
                        "Acquisition request exceeds its configured ceiling",
                    )
                limits[name] = value
        return AcquisitionCeilings.model_validate(limits)

    def _path(self, name: str, base: Path | None = None) -> Path:
        candidate = Path(name)
        if not self._roots or ".." in candidate.parts or "\x00" in name:
            raise TychoraError("FORBIDDEN_SCOPE", "Source escapes its registered root")
        candidates = (
            [candidate]
            if candidate.is_absolute()
            else [root / candidate for root in ([base] if base else self._roots)]
        )
        found = []
        for path in candidates:
            roots = [root for root in self._roots if path.is_relative_to(root)]
            if not roots:
                raise TychoraError(
                    "FORBIDDEN_SCOPE", "Source escapes its registered root"
                )
            try:
                resolved = path.resolve(strict=True)
            except FileNotFoundError:
                continue
            except (OSError, RuntimeError):
                raise TychoraError("FORBIDDEN_SCOPE", "Source path is unsafe") from None
            if not any(resolved.is_relative_to(root) for root in roots):
                raise TychoraError(
                    "FORBIDDEN_SCOPE", "Source escapes its registered root"
                )
            found.append(path)
        found = list(dict.fromkeys(found))
        if not found:
            raise TychoraError("NOT_FOUND", "Source or sidecar is missing")
        if len(found) != 1:
            raise TychoraError(
                "INVALID_REQUEST",
                "Relative source is ambiguous across registered roots",
            )
        return found[0]

    @contextmanager
    def _open(self, path: Path):
        root = max(
            (root for root in self._roots if path.is_relative_to(root)),
            key=lambda root: len(root.parts),
        )
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in root.parts[1:]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = child
            if _stamp(os.fstat(fd))[:2] != self._roots[root]:
                raise TychoraError("FORBIDDEN_SCOPE", "Registered source root changed")
            parts = path.relative_to(root).parts
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index < len(parts) - 1:
                    flags |= os.O_DIRECTORY
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        except FileNotFoundError:
            raise TychoraError("NOT_FOUND", "Source or sidecar is missing") from None
        except OSError:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Source path is unsafe or unavailable"
            ) from None
        finally:
            os.close(fd)

    def _store(
        self, stream: BinaryIO, media: str, maximum: int, ctx: ExecutionContext
    ) -> ArtifactRef:
        # Disk-backed staging bounds memory independently of the configured byte cap.
        with TemporaryFile() as staged:
            size = 0
            while True:
                ctx.checkpoint()
                block = stream.read(min(1024 * 1024, maximum - size + 1))
                if not block:
                    break
                size += len(block)
                if size > maximum:
                    raise _limit()
                staged.write(block)
            ctx.checkpoint()
            staged.seek(0)
            return self.artifacts.put_stream(staged, media)

    def _file(
        self, path: Path, media: str, maximum: int, ctx: ExecutionContext
    ) -> tuple[ArtifactRef, tuple[int, ...]]:
        with self._open(path) as fd:
            before = _stamp(os.fstat(fd))
            if not stat.S_ISREG(before[2]):
                raise TychoraError("FORBIDDEN_SCOPE", "Source must be a regular file")
            if before[3] > maximum:
                raise _limit()
            with os.fdopen(os.dup(fd), "rb") as stream:
                ref = self._store(stream, media, maximum, ctx)
            after = _stamp(os.fstat(fd))
        with self._open(path) as current:
            current_stamp = _stamp(os.fstat(current))
        if before != after or before != current_stamp or ref.size_bytes != before[3]:
            raise _MovingSource
        return ref, before

    def _inventory(self, folder: Path, ctx: ExecutionContext, max_files: int):
        files: list[Path] = []
        stamps = {}
        pending = [folder]
        while pending:
            ctx.checkpoint()
            path = pending.pop()
            if len(path.relative_to(folder).parts) > 64 or len(stamps) >= 180001:
                raise _limit()
            with self._open(path) as fd:
                info = os.fstat(fd)
                stamps[path] = _stamp(info)
                if stat.S_ISDIR(info.st_mode):
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            if len(pending) + len(stamps) > 180001:
                                raise _limit()
                            if entry.is_symlink():
                                raise TychoraError(
                                    "FORBIDDEN_SCOPE",
                                    "Capture contains a symbolic link",
                                )
                            pending.append(path / entry.name)
                elif stat.S_ISREG(info.st_mode):
                    files.append(path)
                    if len(files) > max_files:
                        raise _limit()
                else:
                    raise TychoraError(
                        "FORBIDDEN_SCOPE", "Capture contains a nonregular source"
                    )
        return sorted(files, key=lambda file: str(file.relative_to(folder))), stamps

    def capture_acquired(self, source, acquired, ctx) -> CaptureManifest:
        """Capture only a service-authorized immutable upload binding."""
        ctx.checkpoint()
        value = source.root
        original = acquired.original_ref
        if (
            not isinstance(value, UploadSource)
            or value.content_sha256 != original.sha256
            or value.size_bytes != original.size_bytes
            or value.pdf_processing != acquired.policy
            or source != acquired.source
        ):
            raise TychoraError(
                "INTEGRITY_ERROR", "Acquired original and source intent differ."
            )
        limits = self._limits(source)
        if original.size_bytes > min(limits.max_file_bytes, limits.max_total_bytes):
            raise _limit()
        captured = CaptureManifest(
            source_key=value.source_key,
            original_ref=original,
            original_sha=original.sha256,
            captured_at=datetime.now(UTC),
            acquire_fingerprint=digest(
                "acquired-pdf-v1",
                self.config.acquire_fingerprint,
                acquired.receipt_binding_sha256,
                value.pdf_processing.model_dump(mode="json"),
            ),
            config_sha=self.config.config_sha,
            manifest_ref=original,
            source=FrozenJsonMap(value.model_dump(mode="json")),
        )
        return captured.model_copy(
            update={"manifest_ref": self._manifest(captured, [original])}
        )

    def capture(self, source: SourceInput, ctx: ExecutionContext) -> CaptureManifest:
        ctx.checkpoint()
        limits = self._limits(source)
        value = source.root
        if isinstance(value, UploadSource):
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Upload requires a trusted receipt binding."
            )
        if value.type == "url" and (
            self.config.values.offline
            or not self.config.values.acquisition.url.enabled
            or self.urls is None
        ):
            raise TychoraError(
                "CAPABILITY_MISSING",
                "Online URL acquisition requires an enabled injected adapter",
            )
        path = (
            self._path(value.path)
            if isinstance(value, (FileSource, FolderSource, RepositorySource))
            else None
        )
        base = path if value.type == "folder" else path.parent if path else None
        sidecars = [self._path(name, base) for name in value.sidecars]
        if len(set(sidecars)) != len(sidecars) or path in sidecars:
            raise TychoraError("INVALID_REQUEST", "Duplicate source key in capture")
        if isinstance(value, RepositorySource) and sidecars:
            raise TychoraError(
                "INVALID_REQUEST", "Repository sources do not accept sidecars"
            )
        repository_stamp = None
        if isinstance(value, RepositorySource):
            assert path is not None
            with self._open(path) as descriptor:
                repository_info = os.fstat(descriptor)
                repository_stamp = (repository_info.st_dev, repository_info.st_ino)
            if not stat.S_ISDIR(repository_info.st_mode):
                raise TychoraError(
                    "INVALID_REQUEST", "Repository source must be a directory"
                )
        for _attempt in range(3):
            try:
                if isinstance(value, RepositorySource):
                    assert path is not None and repository_stamp is not None
                    return self._capture_repository(
                        source, path, repository_stamp, limits, ctx
                    )
                return self._capture(source, path, sidecars, limits, ctx)
            except _MovingSource:
                ctx.checkpoint()
            except ValueError:
                raise TychoraError(
                    "INVALID_REQUEST", "Source content or logical identity is invalid"
                ) from None
        raise TychoraError(
            "INTEGRITY_ERROR", "Source remained unstable after three capture attempts"
        )

    def _capture_repository(
        self,
        source: SourceInput,
        path: Path,
        root_stamp: tuple[int, int],
        limits: AcquisitionCeilings,
        ctx: ExecutionContext,
    ) -> CaptureManifest:
        value = source.root
        assert isinstance(value, RepositorySource)
        with self._open(path) as descriptor:
            if _stamp(os.fstat(descriptor))[:2] != root_stamp:
                raise _MovingSource
        snapshot = self._repositories.snapshot(
            path, value.excludes, ctx, max_files=limits.max_files
        )
        entries = []
        originals = []
        total = 0
        for relative in snapshot.present_paths:
            ref, stamp = self._file(
                path / relative,
                value.media_type or "application/octet-stream",
                min(limits.max_file_bytes, limits.max_total_bytes - total),
                ctx,
            )
            total += ref.size_bytes
            originals.append(ref)
            entries.append(
                CapturedFile(
                    relative_path=relative,
                    source_key=normalize_source_key(f"{value.source_key}/{relative}"),
                    mode=stat.S_IMODE(stamp[2]),
                    blob_sha=ref.sha256,
                    artifact_ref=ref,
                )
            )
        captured_entries = tuple(entries)
        dirty = self._repositories.dirty(snapshot, captured_entries, ctx)
        current = self._repositories.snapshot(
            path, value.excludes, ctx, max_files=limits.max_files
        )
        if current != snapshot:
            raise _MovingSource
        with self._open(path) as descriptor:
            if _stamp(os.fstat(descriptor))[:2] != root_stamp:
                raise _MovingSource
        original = self._repository_manifest(captured_entries, snapshot.commit, dirty)
        captured = CaptureManifest(
            source_key=value.source_key,
            original_ref=original,
            original_sha=original.sha256,
            entries=captured_entries,
            commit=snapshot.commit,
            dirty=dirty,
            captured_at=datetime.now(UTC),
            acquire_fingerprint=digest(
                "acquisition-request-v1",
                self.config.acquire_fingerprint,
                limits.model_dump(mode="json"),
                {"repository_excludes": value.excludes},
            ),
            config_sha=self.config.config_sha,
            manifest_ref=original,
            source=FrozenJsonMap(value.model_dump(mode="json")),
        )
        manifest = self._manifest(captured, [*originals, original])
        ctx.checkpoint()
        return captured.model_copy(update={"manifest_ref": manifest})

    def _repository_manifest(
        self,
        entries: tuple[CapturedFile, ...],
        commit: str | None,
        dirty: bool,
    ) -> ArtifactRef:
        def files(part: tuple[CapturedFile, ...]):
            return [
                {
                    "relative_path": entry.relative_path,
                    "mode": entry.mode,
                    "blob_sha": entry.blob_sha,
                    "artifact_ref": entry.artifact_ref.model_dump(mode="json"),
                }
                for entry in part
            ]

        data = {
            "files": files(entries),
            "commit": commit,
            "dirty": dirty,
            "dirty_scope": "captured_paths_vs_head",
        }
        repository = {
            "type": "repository_capture",
            "schema_version": "1",
            "data": data,
            "original_artifacts": [
                ref.model_dump(mode="json")
                for ref in dict.fromkeys(entry.artifact_ref for entry in entries)
            ],
        }
        if len(entries) <= 4000:
            required_refs(repository, capture_published=False)
            return self._json(repository)
        data["files"] = []
        data["files_partitioned"] = True
        repository["original_artifacts"] = []
        links = []
        for offset in range(0, len(entries), 4000):
            part = entries[offset : offset + 4000]
            child = {
                "type": "repository_capture",
                "schema_version": "1",
                "data": {"files": files(part)},
                "original_artifacts": [
                    ref.model_dump(mode="json")
                    for ref in dict.fromkeys(entry.artifact_ref for entry in part)
                ],
            }
            required_refs(child, capture_published=False)
            ref = self._json(child)
            links.append(
                {
                    "artifact": ref.model_dump(mode="json"),
                    "type": "repository_capture",
                    "schema_version": "1",
                }
            )
        repository["required_manifests"] = links
        required_refs(repository, capture_published=False)
        return self._json(repository)

    def _capture(
        self,
        source: SourceInput,
        path: Path | None,
        sidecars: list[Path],
        limits: AcquisitionCeilings,
        ctx: ExecutionContext,
    ) -> CaptureManifest:
        value = source.root
        media = value.media_type or (
            "text/plain" if value.type == "text" else "application/octet-stream"
        )
        entries = []
        observed = {}
        total = 0
        paths, initial = (
            self._inventory(path, ctx, limits.max_files)
            if value.type == "folder" and path
            else ([path] if path else [], {})
        )
        # Explicit folder sidecars are removed from document entries, without reading twice.
        sidecar_paths = frozenset(sidecars)
        paths = [candidate for candidate in paths if candidate not in sidecar_paths]
        document_paths = frozenset(paths)
        if len(paths) + len(sidecars) + (not path) > limits.max_files:
            raise _limit()
        originals = []
        for candidate in [*paths, *sidecars]:
            ref, stamp = self._file(
                candidate,
                media if candidate in document_paths else "application/octet-stream",
                min(limits.max_file_bytes, limits.max_total_bytes - total),
                ctx,
            )
            observed[candidate] = stamp
            total += ref.size_bytes
            originals.append(ref)
            if candidate in document_paths:
                relative = (
                    str(candidate.relative_to(path))
                    if value.type == "folder" and path
                    else candidate.name
                )
                key = (
                    normalize_source_key(f"{value.source_key}/{relative}")
                    if value.type == "folder"
                    else value.source_key
                )
                entries.append(
                    CapturedFile(
                        relative_path=relative,
                        source_key=key,
                        mode=stat.S_IMODE(stamp[2]),
                        blob_sha=ref.sha256,
                        artifact_ref=ref,
                    )
                )
        sidecar_refs = tuple(originals[len(paths) :])
        if isinstance(value, (TextSource, BytesSource)):
            if isinstance(value, TextSource):
                if len(value.text) > limits.max_inline_bytes:
                    raise _limit()
                payload = value.text.encode("utf-8")
            else:
                payload = value.data
            original = self._store(
                BytesIO(payload),
                media,
                min(
                    limits.max_file_bytes,
                    limits.max_inline_bytes,
                    limits.max_total_bytes - total,
                ),
                ctx,
            )
            originals.append(original)
        elif value.type == "url":
            assert self.urls is not None
            with self.urls.open(
                value.url, self.config.values.acquisition.url, limits, ctx
            ) as stream:
                original = self._store(
                    stream,
                    media,
                    min(limits.max_file_bytes, limits.max_total_bytes - total),
                    ctx,
                )
            originals.append(original)
        elif value.type == "folder":
            if path is None:
                raise TychoraError("INTEGRITY_ERROR", "Missing folder binding")
            if initial != self._inventory(path, ctx, limits.max_files)[1]:
                raise _MovingSource
            original = self._tree(tuple(entries), sidecar_refs, ctx)
            originals.append(original)
        else:
            original = originals[0]
        for candidate, stamp in observed.items():
            with self._open(candidate) as fd:
                if _stamp(os.fstat(fd)) != stamp:
                    raise _MovingSource
        ctx.checkpoint()
        captured = CaptureManifest(
            source_key=value.source_key,
            original_ref=original,
            original_sha=original.sha256,
            sidecar_refs=sidecar_refs,
            entries=tuple(entries),
            captured_at=datetime.now(UTC),
            acquire_fingerprint=digest(
                "acquisition-request-v1",
                self.config.acquire_fingerprint,
                limits.model_dump(mode="json"),
            ),
            config_sha=self.config.config_sha,
            manifest_ref=original,
            source=FrozenJsonMap(
                value.model_dump(mode="json", exclude={"text", "data"})
            ),
        )
        ref = self._manifest(captured, originals)
        ctx.checkpoint()
        return captured.model_copy(update={"manifest_ref": ref})

    def _manifest(
        self, captured: CaptureManifest, originals: list[ArtifactRef]
    ) -> ArtifactRef:
        data = captured.model_dump(mode="json", exclude={"manifest_ref", "entries"})
        manifest: dict[str, Any] = {
            "type": "job_capture",
            "schema_version": "1",
            "data": data,
        }
        if len(captured.entries) <= 4000 and len(originals) <= 4000:
            data["entries"] = [
                entry.model_dump(mode="json") for entry in captured.entries
            ]
            manifest["original_artifacts"] = [
                ref.model_dump(mode="json") for ref in originals
            ]
        else:
            links = []
            # Parent carries the aggregate identity; ordered children carry complete
            # entries and explicit retention edges, never implicit hashes in data.
            data["entries"] = []
            data["entries_partitioned"] = True
            for offset in range(0, len(captured.entries), 4000):
                entries = captured.entries[offset : offset + 4000]
                child = {
                    "type": "job_capture",
                    "schema_version": "1",
                    "data": {
                        "entries": [entry.model_dump(mode="json") for entry in entries]
                    },
                    "original_artifacts": [
                        ref.model_dump(mode="json")
                        for ref in dict.fromkeys(
                            entry.artifact_ref for entry in entries
                        )
                    ],
                }
                required_refs(child, capture_published=False)
                ref = self._json(child)
                links.append(
                    {
                        "artifact": ref.model_dump(mode="json"),
                        "type": "job_capture",
                        "schema_version": "1",
                    }
                )
            for offset in range(0, len(captured.sidecar_refs), 4000):
                child = {
                    "type": "job_capture",
                    "schema_version": "1",
                    "original_artifacts": [
                        ref.model_dump(mode="json")
                        for ref in dict.fromkeys(
                            captured.sidecar_refs[offset : offset + 4000]
                        )
                    ],
                }
                required_refs(child, capture_published=False)
                ref = self._json(child)
                links.append(
                    {
                        "artifact": ref.model_dump(mode="json"),
                        "type": "job_capture",
                        "schema_version": "1",
                    }
                )
            manifest["required_manifests"] = links
            manifest["original_artifacts"] = [
                captured.original_ref.model_dump(mode="json")
            ]
        if captured.source.get("type") == "repository":
            manifest.setdefault("required_manifests", []).append(
                {
                    "artifact": captured.original_ref.model_dump(mode="json"),
                    "type": "repository_capture",
                    "schema_version": "1",
                }
            )
        required_refs(manifest, capture_published=False)
        return self._json(manifest)

    def _tree(
        self,
        entries: tuple[CapturedFile, ...],
        sidecars: tuple[ArtifactRef, ...],
        ctx: ExecutionContext,
    ) -> ArtifactRef:
        """Write the opaque aggregate incrementally, bounded by physical CAS capacity."""
        with TemporaryFile() as stream:
            size = 0

            def write(block: bytes) -> None:
                nonlocal size
                ctx.checkpoint()
                size += len(block)
                if size > self.artifacts.max_size_bytes:
                    raise _limit()
                stream.write(block)

            write(b'{"entries":[')
            for index, entry in enumerate(entries):
                if index:
                    write(b",")
                write(canonical_bytes(entry.model_dump(mode="json")))
            write(b'],"sidecar_refs":[')
            for index, ref in enumerate(sidecars):
                if index:
                    write(b",")
                write(canonical_bytes(ref.model_dump(mode="json")))
            write(b"]}")
            stream.seek(0)
            ref = self.artifacts.put_stream(stream, "application/json")
            ctx.checkpoint()
            return ref

    def _json(self, data) -> ArtifactRef:
        encoded = canonical_bytes(data)
        if len(encoded) > min(MAX_MANIFEST_BYTES, self.artifacts.max_size_bytes):
            raise _limit()
        return self.artifacts.put_stream(BytesIO(encoded), "application/json")


class _MovingSource(Exception):
    """Private retry signal; no partial capture has been returned."""
