"""Sole workspace owner and thread-affine canonical resources."""

import asyncio
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from threading import Lock, get_ident
from typing import BinaryIO, NoReturn, Protocol, cast, overload
from uuid import UUID, uuid4

from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.configuration.models import ResolvedConfig
from tychora.configuration.registry import Registry
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.embeddings import QueryUploadAuthorization
from tychora.contracts.errors import TychoraError
from tychora.contracts.extraction import extraction_document
from tychora.contracts.identity import (
    JsonValue,
    canonical_bytes,
    digest,
    normalize_source_key,
    revision_id,
    source_id,
)
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext
from tychora.ports.vision import (
    CapturedOriginalRegionProof,
    CapturedRegionAuthorization,
    CapturedSourceRegistration,
    RetainedDocumentRegionProof,
    pixel_identity,
)

from .leases import LeaseSet
from .runtime import (
    BoundedWorker,
    RuntimeIdentity,
    drain_result,
    parser_process,
    private_directory,
)


class CloseableRuntime(Protocol):
    def close(self) -> None: ...


MAX_PARSER_OUTPUT_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class StagedOutput:
    name: str
    media_type: str
    size_bytes: int


def _sync_result[T](future: Future[T], ctx: ExecutionContext) -> T:
    try:
        result = future.result()
    except BaseException:
        ctx.cancelled.set()
        # Never abandon resource cleanup, even for external KeyboardInterrupt.
        while not future.done():
            with suppress(BaseException):
                future.result()
        raise
    ctx.checkpoint()
    return result


class WorkspaceOwner:
    def __init__(self, path: Path, config: ResolvedConfig, scope: RequestScope) -> None:
        self.path = path.resolve()
        self.config = config
        self.scope = scope
        self.leases = LeaseSet()
        self._worker = BoundedWorker()
        self._runtime = RuntimeIdentity(self.path)
        self._parsers = BoundedWorker(workers=2)
        self._providers: dict[str, BoundedWorker] = {}
        self._admission = Lock()
        self._closing = False
        self._handles = ExitStack()

    @classmethod
    @contextmanager
    def open(
        cls, path: Path, config: ResolvedConfig, scope: RequestScope
    ) -> Iterator["WorkspaceOwner"]:
        owner = cls(path, config, scope)
        try:
            owner.path.mkdir(parents=True, exist_ok=True)
            owner._runtime.acquire()
            owner._worker.submit(owner._initialize).result()
            owner._runtime.publish(owner.workspace_id)
            yield owner
        finally:
            try:
                with owner._admission:
                    owner._closing = True
                    owner._worker.stop_admission()
                    owner._parsers.stop_admission()
                    for lane in owner._providers.values():
                        lane.stop_admission()
                with ExitStack() as cleanup:
                    cleanup.callback(owner._worker.close, owner._close_storage)
                    cleanup.callback(owner._parsers.close)
                    for lane in owner._providers.values():
                        cleanup.callback(lane.close)
            finally:
                owner._runtime.close()

    def _initialize(self) -> None:
        self._thread_id = get_ident()
        self.path.mkdir(parents=True, exist_ok=True)
        self.artifacts = LocalArtifacts(self.path / "artifacts")
        private_directory(self.path / "staging")
        database = self.path / "corpus.sqlite3"
        if database.is_symlink() or (
            database.exists() and database.stat().st_nlink != 1
        ):
            raise TychoraError("INTEGRITY_ERROR", "Canonical database path is unsafe")
        try:
            self.db = open_corpus(self.path / "corpus.sqlite3", initialize=True)
        except sqlite3.DatabaseError:
            raise TychoraError(
                "INTEGRITY_ERROR", "Canonical database cannot be opened"
            ) from None
        rows = self.db.execute("SELECT workspace_id FROM workspaces").fetchall()
        if not rows:
            tables = self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            for (table,) in tables:
                if table == "schema_migrations" or table.startswith(
                    ("sqlite_", "lexical_fts")
                ):
                    continue
                # Names come from the already-verified installed schema.
                if self.db.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Existing corpus has no workspace identity"
                    )
            self.workspace_id = str(uuid4())
            self.db.execute(
                "INSERT INTO workspaces (workspace_id,schema_version,created_at) VALUES (?,1,?)",
                (self.workspace_id, datetime.now(UTC).isoformat()),
            )
            self.db.commit()
        elif len(rows) == 1:
            self.workspace_id = rows[0][0]
            try:
                if str(UUID(self.workspace_id)) != self.workspace_id:
                    raise ValueError
            except ValueError:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Invalid workspace identity"
                ) from None
        else:
            raise TychoraError("INTEGRITY_ERROR", "Workspace identity is ambiguous")
        registry = Registry.from_config(self.config)
        retained = {
            kind: json.loads(definition)
            for kind, definition, collection in self.db.execute(
                "SELECT DISTINCT k.kind_id,k.definition_json,c.collection_id FROM kinds k "
                "JOIN revisions r USING(kind_id) JOIN sources s USING(source_id) "
                "JOIN collections c USING(collection_id) WHERE c.workspace_id=?",
                (self.workspace_id,),
            )
            if collection in self.scope.collection_ids
        }
        registry.validate_activation(frozenset(retained), retained)
        self.upload_policy = CanonicalUploadPolicy(self)
        self.store = CorpusStore(self.db, registry=registry, artifacts=self.artifacts)

    def _close_storage(self) -> None:
        try:
            self._handles.close()
        finally:
            if hasattr(self, "db"):
                self.db.close()

    def register_close(self, close: Callable[[], None]) -> None:
        """Register an adapter's cleanup on its owning canonical thread."""
        if get_ident() != self._thread_id:
            raise TychoraError(
                "INTEGRITY_ERROR", "Register resources on the owner thread"
            )
        self._handles.callback(close)

    def register_runtime(self, runtime: CloseableRuntime) -> None:
        """Transfer a generic provider runtime's lifecycle to this owner."""
        self.register_close(runtime.close)

    def _sync_thread(self) -> None:
        if get_ident() == self._thread_id:
            raise TychoraError(
                "INVALID_REQUEST", "Sync dispatch cannot run on the owner thread"
            )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise TychoraError(
            "INVALID_REQUEST", "Use the async owner/parser path on an event-loop thread"
        )

    def _blocking_future[T](
        self, call: Callable[[], T], ctx: ExecutionContext
    ) -> Future[T]:
        ctx.checkpoint()

        def checked() -> T:
            ctx.checkpoint()
            return call()

        return self._worker.submit(checked)

    async def run_blocking[T](self, call: Callable[[], T], ctx: ExecutionContext) -> T:
        return await drain_result(self._blocking_future(call, ctx), ctx)

    def run_blocking_sync[T](self, call: Callable[[], T], ctx: ExecutionContext) -> T:
        self._sync_thread()
        return _sync_result(self._blocking_future(call, ctx), ctx)

    def _stage(
        self, refs: tuple[ArtifactRef, ...], ctx: ExecutionContext
    ) -> tuple[Path, tuple[Path, ...]]:
        ctx.checkpoint()
        directory = Path(tempfile.mkdtemp(prefix="parser-", dir=self.path / "staging"))
        try:
            copies = []
            with self.artifacts.pin_payloads(frozenset(ref.sha256 for ref in refs)):
                for index, ref in enumerate(refs):
                    ctx.checkpoint()
                    path = directory / str(index)
                    with (
                        self.artifacts.open_verified(ref) as source,
                        path.open("xb") as destination,
                    ):
                        path.chmod(0o600)
                        shutil.copyfileobj(source, destination, 1024 * 1024)
                    copies.append(path)
            return directory, tuple(copies)
        except BaseException:
            shutil.rmtree(directory)
            raise

    def _collect_outputs[T](
        self,
        directory: Path,
        result: object,
        collector: Callable[[tuple[tuple[StagedOutput, BinaryIO], ...]], T],
    ) -> T:
        if not isinstance(result, tuple) or len(result) > 4096:
            raise TychoraError("INTEGRITY_ERROR", "Invalid parser output descriptors")
        total, names = 0, set()
        with ExitStack() as stack:
            outputs = []
            for entry in result:
                if (
                    not isinstance(entry, StagedOutput)
                    or not entry.name
                    or Path(entry.name).name != entry.name
                    or entry.name in (".", "..")
                    or entry.name in names
                    or type(entry.size_bytes) is not int
                    or entry.size_bytes < 0
                ):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Unsafe parser output descriptor"
                    )
                names.add(entry.name)
                total += entry.size_bytes
                if total > MAX_PARSER_OUTPUT_BYTES:
                    raise TychoraError(
                        "INVALID_REQUEST", "Parser output exceeds256MiB limit"
                    )
                fd = os.open(
                    directory / entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                )
                stream = stack.enter_context(os.fdopen(fd, "rb"))
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size != entry.size_bytes
                    or info.st_uid != os.getuid()
                ):
                    raise TychoraError("INTEGRITY_ERROR", "Unsafe parser output file")
                outputs.append((entry, stream))
            return collector(tuple(outputs))

    def _parser_future[T](
        self,
        call: Callable[[tuple[Path, ...]], object],
        inputs: tuple[ArtifactRef, ...],
        ctx: ExecutionContext,
        collector: Callable[[tuple[tuple[StagedOutput, BinaryIO], ...]], T] | None,
    ) -> Future[T]:
        ctx.checkpoint()

        def parse() -> T:
            ctx.checkpoint()
            directory, copies = self._worker.submit(
                lambda: self._stage(inputs, ctx)
            ).result()
            try:
                ctx.checkpoint()
                result = parser_process(call, copies)
                if collector is not None:
                    # The child has been reaped; collect before deleting its files.
                    result = self._worker.submit(
                        lambda: self._collect_outputs(directory, result, collector)
                    ).result()
                return cast(T, result)
            finally:
                shutil.rmtree(directory)

        return self._parsers.submit(parse)

    @overload
    async def run_parser[T](
        self,
        call: Callable[[tuple[Path, ...]], T],
        inputs: tuple[ArtifactRef, ...],
        ctx: ExecutionContext,
    ) -> T: ...

    @overload
    async def run_parser[T](
        self,
        call: Callable[[tuple[Path, ...]], object],
        inputs: tuple[ArtifactRef, ...],
        ctx: ExecutionContext,
        *,
        collector: Callable[[tuple[tuple[StagedOutput, BinaryIO], ...]], T],
    ) -> T: ...

    async def run_parser[T](
        self,
        call: Callable[[tuple[Path, ...]], object],
        inputs: tuple[ArtifactRef, ...],
        ctx: ExecutionContext,
        *,
        collector: Callable[[tuple[tuple[StagedOutput, BinaryIO], ...]], T]
        | None = None,
    ) -> T:
        return await drain_result(
            self._parser_future(call, inputs, ctx, collector), ctx
        )

    @overload
    def run_parser_sync[T](
        self,
        call: Callable[[tuple[Path, ...]], T],
        inputs: tuple[ArtifactRef, ...],
        ctx: ExecutionContext,
    ) -> T: ...

    @overload
    def run_parser_sync[T](
        self,
        call: Callable[[tuple[Path, ...]], object],
        inputs: tuple[ArtifactRef, ...],
        ctx: ExecutionContext,
        *,
        collector: Callable[[tuple[tuple[StagedOutput, BinaryIO], ...]], T],
    ) -> T: ...

    def run_parser_sync[T](
        self,
        call: Callable[[tuple[Path, ...]], object],
        inputs: tuple[ArtifactRef, ...],
        ctx: ExecutionContext,
        *,
        collector: Callable[[tuple[tuple[StagedOutput, BinaryIO], ...]], T]
        | None = None,
    ) -> T:
        self._sync_thread()
        return _sync_result(self._parser_future(call, inputs, ctx, collector), ctx)

    async def run_provider[T](
        self,
        provider: str,
        call: Callable[[], T],
        ctx: ExecutionContext,
        *,
        scope: RequestScope,
        dependencies: frozenset[str],
        policy_epoch: int,
    ) -> T:
        """Run an already-injected provider after fresh queued authorization."""
        ctx.checkpoint()
        granted = self.scope.narrow(scope)
        if self.config.values.offline or provider not in granted.upload_providers:
            _deny()
        with self._admission:
            if self._closing:
                raise TychoraError(
                    "WORKSPACE_BUSY",
                    "Workspace is closing; retry shortly",
                    retryable=True,
                )
            lane = self._providers.get(provider)
            if lane is None:
                lane = BoundedWorker(workers=2)
                self._providers[provider] = lane

        def invoke() -> T:
            ctx.checkpoint()
            self._worker.submit(
                lambda: self.upload_policy.authorize(
                    scope, provider, dependencies, policy_epoch
                )
            ).result()
            ctx.checkpoint()
            return call()

        return await drain_result(lane.submit(invoke), ctx)

    async def run_captured_provider[T](
        self,
        provider: str,
        call: Callable[[], T],
        ctx: ExecutionContext,
        *,
        scope: RequestScope,
        authorization: CapturedRegionAuthorization,
        policy_epoch: int,
    ) -> T:
        """Construct/call a provider only after fresh captured-pixel authorization."""
        ctx.checkpoint()
        granted = self.scope.narrow(scope)
        if self.config.values.offline or provider not in granted.upload_providers:
            _deny()
        with self._admission:
            if self._closing:
                raise TychoraError(
                    "WORKSPACE_BUSY",
                    "Workspace is closing; retry shortly",
                    retryable=True,
                )
            lane = self._providers.get(provider)
            if lane is None:
                lane = BoundedWorker(workers=2)
                self._providers[provider] = lane

        def invoke() -> T:
            ctx.checkpoint()
            self._worker.submit(
                lambda: self.upload_policy.authorize_captured(
                    scope, provider, authorization, policy_epoch
                )
            ).result()
            ctx.checkpoint()
            return call()

        return await drain_result(lane.submit(invoke), ctx)

    async def run_query_provider[T](
        self,
        provider: str,
        call: Callable[[], T],
        ctx: ExecutionContext,
        *,
        scope: RequestScope,
        authorization: QueryUploadAuthorization,
    ) -> T:
        """Run a query upload miss after fresh bounded-lane authorization."""
        ctx.checkpoint()
        granted = self.scope.narrow(scope)
        if self.config.values.offline or provider not in granted.upload_providers:
            _deny()
        with self._admission:
            if self._closing:
                raise TychoraError(
                    "WORKSPACE_BUSY",
                    "Workspace is closing; retry shortly",
                    retryable=True,
                )
            lane = self._providers.get(provider)
            if lane is None:
                lane = BoundedWorker(workers=2)
                self._providers[provider] = lane

        def invoke() -> T:
            ctx.checkpoint()
            self._worker.submit(
                lambda: self.upload_policy.authorize_query(
                    scope, provider, authorization
                )
            ).result()
            ctx.checkpoint()
            return call()

        return await drain_result(lane.submit(invoke), ctx)


def _deny() -> NoReturn:
    raise TychoraError(
        "FORBIDDEN_SCOPE", "Current upload permissions do not allow this input"
    )


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result = {}
    for key, value in pairs:
        if key in result:
            _deny()
        result[key] = value
    return result


def _strings(value: object) -> frozenset[str]:
    if not isinstance(value, list):
        _deny()
    result: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            _deny()
        result.add(item)
    return frozenset(result)


class CanonicalUploadPolicy:
    """Authorize only canonical dependencies using a fresh owner-thread read."""

    def __init__(self, owner: WorkspaceOwner) -> None:
        self._owner = owner

    def _check_epoch(self, policy_epoch: int) -> None:
        epoch = self._owner.db.execute(
            "SELECT policy_epoch FROM workspaces WHERE workspace_id=?",
            (self._owner.workspace_id,),
        ).fetchone()
        if epoch is None or type(policy_epoch) is not int or epoch[0] != policy_epoch:
            raise TychoraError(
                "SCOPE_CHANGED",
                "Workspace policy changed; retry authorization",
                retryable=True,
            )

    def register_captured_source(
        self,
        registration: CapturedSourceRegistration,
        scope: RequestScope,
        *,
        policy_epoch: int,
    ) -> None:
        """Register immutable first-source metadata under an existing collection."""
        owner = self._owner
        granted = owner.scope.narrow(scope)
        source = registration.source
        if (
            "manage" not in granted.capabilities
            or source.collection_id not in granted.collection_ids
            or source.source_key != normalize_source_key(source.source_key)
        ):
            _deny()
        try:
            if source.source_id != source_id(
                UUID(source.collection_id), source.source_key
            ):
                _deny()
        except ValueError:
            _deny()
        if owner.db.in_transaction:
            raise TychoraError(
                "INTEGRITY_ERROR", "Captured source registration requires a fresh write"
            )
        owner.db.execute("BEGIN IMMEDIATE")
        try:
            self._check_epoch(policy_epoch)
            collection = owner.db.execute(
                "SELECT workspace_id,access_floor FROM collections WHERE collection_id=?",
                (source.collection_id,),
            ).fetchone()
            if (
                collection is None
                or collection[0] != owner.workspace_id
                or max(collection[1], source.access_floor) > granted.access_ceiling
            ):
                _deny()
            definitions = owner.store.registry.historical_definitions()
            if registration.kind_id not in definitions:
                _deny()
            envelope = definitions[registration.kind_id]
            if not isinstance(envelope, FrozenJsonMap):
                _deny()
            kind = envelope["kind"]
            if not isinstance(kind, FrozenJsonMap):
                _deny()
            kind_row = (
                registration.kind_id,
                envelope["envelope_digest"],
                kind["label"],
                kind["description"],
                kind["role"],
                canonical_bytes(envelope).decode("utf-8"),
            )
            existing_kind = owner.db.execute(
                "SELECT kind_id,definition_hash,label,description,role,definition_json "
                "FROM kinds WHERE kind_id=?",
                (registration.kind_id,),
            ).fetchone()
            if existing_kind is None:
                owner.db.execute(
                    "INSERT INTO kinds "
                    "(kind_id,definition_hash,label,description,role,definition_json) "
                    "VALUES (?,?,?,?,?,?)",
                    kind_row,
                )
            elif tuple(existing_kind) != kind_row:
                _deny()
            source_row = (
                source.source_id,
                source.collection_id,
                source.source_key,
                source.source_type,
                canonical_bytes(source.registration_json).decode("utf-8"),
                source.access_floor,
                None,
                source.created_at,
            )
            existing_source = owner.db.execute(
                "SELECT source_id,collection_id,source_key,source_type,registration_json,"
                "access_floor,withdrawn_at,created_at FROM sources "
                "WHERE source_id=? OR (collection_id=? AND source_key=?)",
                (source.source_id, source.collection_id, source.source_key),
            ).fetchall()
            if not existing_source:
                owner.db.execute(
                    "INSERT INTO sources "
                    "(source_id,collection_id,source_key,source_type,registration_json,"
                    "access_floor,withdrawn_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    source_row,
                )
            elif len(existing_source) != 1 or tuple(existing_source[0]) != source_row:
                _deny()
            owner.db.commit()
        except BaseException:
            owner.db.rollback()
            raise

    def authorize_captured(
        self,
        scope: RequestScope,
        provider: str,
        authorization: CapturedRegionAuthorization,
        policy_epoch: int,
    ) -> None:
        """Authorize exact retained image pixels for an uncommitted revision."""
        owner = self._owner
        granted = owner.scope.narrow(scope)
        if owner.config.values.offline or provider not in granted.upload_providers:
            _deny()
        if owner.db.in_transaction:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Upload authorization requires a fresh canonical read",
            )
        self._verify_captured_pixels(authorization)
        owner.db.execute("BEGIN")
        try:
            self._check_epoch(policy_epoch)
            self._check_captured_revision(authorization, provider, granted)
            self._check_candidate_context(authorization)
        except (ValueError, TypeError, KeyError):
            _deny()
        finally:
            owner.db.rollback()

    def _verify_captured_pixels(
        self, authorization: CapturedRegionAuthorization
    ) -> None:
        from PIL import Image, ImageOps

        capture, region = authorization.capture, authorization.region
        try:
            manifest = capture.model_dump(
                mode="json", exclude={"manifest_ref", "entries"}
            )
            manifest["entries"] = [
                entry.model_dump(mode="json") for entry in capture.entries
            ]
            expected = {
                "type": "job_capture",
                "schema_version": "1",
                "data": manifest,
                "original_artifacts": [
                    ref.model_dump(mode="json")
                    for ref in (capture.original_ref, *capture.sidecar_refs)
                ],
            }
            with self._owner.artifacts.open_verified(capture.manifest_ref) as stream:
                if stream.read(capture.manifest_ref.size_bytes + 1) != canonical_bytes(
                    expected
                ):
                    _deny()
            proof = authorization.proof
            if isinstance(proof, CapturedOriginalRegionProof):
                original_ref = capture.original_ref
                expected_original = (
                    "PNG" if capture.source.get("format") == "png" else "JPEG"
                )
            elif isinstance(proof, RetainedDocumentRegionProof):
                extraction = proof.extraction
                data = extraction.model_dump(
                    mode="json", exclude={"document_json", "raw_document_ref"}
                )
                with self._owner.artifacts.open_verified(
                    extraction.raw_document_ref
                ) as stream:
                    if stream.read(extraction.raw_document_ref.size_bytes + 1) != (
                        extraction_document(data)
                    ):
                        _deny()
                if (
                    extraction.docling_json_ref is None
                    or extraction.document_json is None
                ):
                    _deny()
                with self._owner.artifacts.open_verified(
                    extraction.docling_json_ref
                ) as stream:
                    if stream.read(extraction.docling_json_ref.size_bytes + 1) != (
                        canonical_bytes(extraction.document_json)
                    ):
                        _deny()
                original_ref = proof.asset_ref
                expected_original = (
                    "PNG" if original_ref.media_type == "image/png" else "JPEG"
                )
                if original_ref.media_type not in ("image/png", "image/jpeg"):
                    _deny()
            else:
                _deny()
            with (
                self._owner.artifacts.open_verified(original_ref) as stream,
                Image.open(stream) as encoded,
            ):
                if (
                    encoded.format != expected_original
                    or encoded.width * encoded.height > 40_000_000
                ):
                    _deny()
                original = ImageOps.exif_transpose(encoded).convert("RGB")
                original.load()
            with self._owner.artifacts.open_verified(region.image_ref) as stream:
                retained_region = stream.read(region.image_ref.size_bytes + 1)
            if retained_region != region.image:
                _deny()
            with Image.open(BytesIO(region.image)) as encoded_region:
                expected_region = "PNG" if region.media_type == "image/png" else "JPEG"
                if encoded_region.format != expected_region:
                    _deny()
                decoded_region = ImageOps.exif_transpose(encoded_region).convert("RGB")
                decoded_region.load()
            coords = (
                region.geometry.x0,
                region.geometry.y0,
                region.geometry.x1,
                region.geometry.y1,
            )
            if any(int(value) != value for value in coords):
                _deny()
            box = cast(tuple[int, int, int, int], tuple(int(value) for value in coords))
            cropped = original.crop(box)
            expected_transform = "exif-transpose-rgb-v1:crop:" + ",".join(
                str(value) for value in box
            )
            if (
                region.original_width != original.width
                or region.original_height != original.height
                or region.width != decoded_region.width
                or region.height != decoded_region.height
                or decoded_region.size != cropped.size
                or decoded_region.tobytes() != cropped.tobytes()
                or region.color_mode != "RGB"
                or region.original_pixel_transform != "exif-transpose-rgb-v1"
                or region.region_pixel_transform != expected_transform
                or region.original_pixel_sha256
                != pixel_identity(
                    original.tobytes(),
                    original.width,
                    original.height,
                    "RGB",
                    region.original_pixel_transform,
                )
                or region.region_pixel_sha256
                != pixel_identity(
                    decoded_region.tobytes(),
                    decoded_region.width,
                    decoded_region.height,
                    "RGB",
                    region.region_pixel_transform,
                )
            ):
                _deny()
        except TychoraError:
            _deny()
        except (Image.DecompressionBombError, KeyError, OSError, TypeError, ValueError):
            _deny()

    def _check_candidate_context(
        self, authorization: CapturedRegionAuthorization
    ) -> None:
        from tychora.ports.vision import (
            RetainedDocumentRegionProof,
            verify_retained_source_context,
        )

        region = authorization.region
        if not region.candidate_context:
            return
        if not isinstance(authorization.proof, RetainedDocumentRegionProof):
            _deny()
        try:
            verify_retained_source_context(
                authorization.proof.extraction, region.source_context
            )
        except ValueError:
            _deny()

    def _authorize_dependencies(
        self, dependencies: frozenset[str], provider: str, scope: RequestScope
    ) -> None:
        pending = [(dependency, False) for dependency in dependencies]
        complete: set[str] = set()
        visiting: set[str] = set()
        while pending:
            identity, finishing = pending.pop()
            if finishing:
                visiting.remove(identity)
                complete.add(identity)
                continue
            if identity in visiting:
                _deny()
            if identity in complete:
                continue
            if len(complete) + len(visiting) >= 2048:
                _deny()
            visiting.add(identity)
            pending.append((identity, True))
            revision, children = self._resolve(identity)
            self._check_revision(revision, provider, scope)
            if len(pending) + len(children) > 4096:
                _deny()
            pending.extend((child, False) for child in children)

    def _check_captured_revision(
        self,
        authorization: CapturedRegionAuthorization,
        provider: str,
        scope: RequestScope,
    ) -> None:
        registration = authorization.registration
        source = registration.source
        revision = authorization.revision
        row = self._owner.db.execute(
            "SELECT c.workspace_id,c.collection_id,c.access_floor,s.source_id,s.source_key,"
            "s.source_type,s.registration_json,s.access_floor,s.withdrawn_at,c.policy_json,"
            "k.definition_json,k.definition_hash FROM sources s "
            "JOIN collections c USING(collection_id) JOIN kinds k ON k.kind_id=? "
            "WHERE s.source_id=?",
            (revision.kind_id, source.source_id),
        ).fetchone()
        if (
            row is None
            or row[0] != self._owner.workspace_id
            or row[1] != source.collection_id
            or row[1] not in scope.collection_ids
            or row[3:8]
            != (
                source.source_id,
                source.source_key,
                source.source_type,
                canonical_bytes(source.registration_json).decode("utf-8"),
                source.access_floor,
            )
            or max(row[2], row[7], revision.access_rank) > scope.access_ceiling
            or row[8] is not None
            or revision.revision_id
            != revision_id(
                source.source_id,
                revision.original_sha,
                revision.edition_key,
                revision.metadata_sha,
            )
            or revision.metadata_sha != digest("metadata-v1", revision.metadata_json)
        ):
            _deny()
        try:
            self._owner.store.registry.validate_metadata(
                revision.kind_id, revision.metadata_json
            )
        except TychoraError:
            _deny()
        policy = json.loads(row[9], object_pairs_hook=_unique_object)
        if (
            not isinstance(policy, dict)
            or set(policy) != {"schema_version", "upload_providers"}
            or policy.get("schema_version") != 1
            or provider not in _strings(policy.get("upload_providers"))
        ):
            _deny()
        envelope = json.loads(row[10], object_pairs_hook=_unique_object)
        if not isinstance(envelope, dict):
            _deny()
        recorded = envelope.pop("envelope_digest")
        if recorded != row[11] or digest("historical-kind-v1", envelope) != recorded:
            _deny()
        if not (
            _strings(envelope["kind"]["allowed_uses"])
            & frozenset(revision.allowed_uses_json)
            & scope.uses
        ):
            _deny()

    def authorize(
        self,
        scope: RequestScope,
        provider: str,
        dependencies: frozenset[str],
        policy_epoch: int,
    ) -> None:
        owner = self._owner
        granted = owner.scope.narrow(scope)
        if (
            owner.config.values.offline
            or provider not in granted.upload_providers
            or not dependencies
            or len(dependencies) > 2048
        ):
            _deny()
        if owner.db.in_transaction:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Upload authorization requires a fresh canonical read",
            )
        owner.db.execute("BEGIN")
        try:
            self._check_epoch(policy_epoch)
            self._authorize_dependencies(dependencies, provider, granted)
        except (ValueError, TypeError, KeyError):
            _deny()
        finally:
            owner.db.rollback()

    def authorize_query(
        self,
        scope: RequestScope,
        provider: str,
        authorization: QueryUploadAuthorization,
    ) -> None:
        """Authorize caller query text against explicit current collections."""
        owner = self._owner
        granted = owner.scope.narrow(scope)
        if (
            owner.config.values.offline
            or provider not in granted.upload_providers
            or not authorization.collection_ids
            or not authorization.collection_ids <= granted.collection_ids
        ):
            _deny()
        if owner.db.in_transaction:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Upload authorization requires a fresh canonical read",
            )
        owner.db.execute("BEGIN")
        try:
            self._check_epoch(authorization.policy_epoch)
            for collection_id in sorted(authorization.collection_ids):
                row = owner.db.execute(
                    "SELECT workspace_id,access_floor,policy_json "
                    "FROM collections WHERE collection_id=?",
                    (collection_id,),
                ).fetchone()
                if (
                    row is None
                    or row[0] != owner.workspace_id
                    or row[1] > granted.access_ceiling
                ):
                    _deny()
                policy = json.loads(row[2], object_pairs_hook=_unique_object)
                if (
                    not isinstance(policy, dict)
                    or set(policy) != {"schema_version", "upload_providers"}
                    or type(policy["schema_version"]) is not int
                    or policy["schema_version"] != 1
                    or provider not in _strings(policy["upload_providers"])
                ):
                    _deny()
            if authorization.dependency_ids:
                self._authorize_dependencies(
                    authorization.dependency_ids, provider, granted
                )
        except (ValueError, TypeError, KeyError):
            _deny()
        finally:
            owner.db.rollback()

    def _resolve(self, identity: str) -> tuple[str, tuple[str, ...]]:
        db = self._owner.db
        matches = db.execute(
            "SELECT revision_id,'revision' FROM revisions WHERE revision_id=? "
            "UNION ALL SELECT e.revision_id,'unit' FROM evidence_units u JOIN extractions e USING(extraction_id) WHERE u.unit_id=? "
            "UNION ALL SELECT e.revision_id,'asset' FROM assets a JOIN extractions e USING(extraction_id) WHERE a.asset_id=?",
            (identity, identity, identity),
        ).fetchall()
        if len(matches) != 1:
            _deny()
        revision, kind = matches[0]
        children: tuple[str, ...] = ()
        if kind == "unit":
            proxy = db.execute(
                "SELECT asset_id FROM asset_proxies WHERE proxy_unit_id=?", (identity,)
            ).fetchone()
            if proxy:
                rows = db.execute(
                    "SELECT required_unit_id FROM asset_proxy_dependencies WHERE proxy_unit_id=? LIMIT 2049",
                    (identity,),
                ).fetchall()
                if not rows or len(rows) > 2048:
                    _deny()
                children = (proxy[0], *(row[0] for row in rows))
        return revision, children

    def _check_revision(
        self, revision: str, provider: str, scope: RequestScope
    ) -> None:
        row = self._owner.db.execute(
            "SELECT c.workspace_id,c.collection_id,c.access_floor,s.access_floor,r.access_rank,s.withdrawn_at,c.policy_json,r.allowed_uses_json,k.definition_json,k.definition_hash "
            "FROM revisions r JOIN sources s USING(source_id) JOIN collections c USING(collection_id) JOIN kinds k USING(kind_id) WHERE r.revision_id=?",
            (revision,),
        ).fetchone()
        if (
            row is None
            or row[0] != self._owner.workspace_id
            or row[1] not in scope.collection_ids
            or max(row[2:5]) > scope.access_ceiling
            or row[5] is not None
        ):
            _deny()
        policy = json.loads(row[6], object_pairs_hook=_unique_object)
        if (
            not isinstance(policy, dict)
            or set(policy) != {"schema_version", "upload_providers"}
            or type(policy["schema_version"]) is not int
            or policy["schema_version"] != 1
        ):
            _deny()
        if provider not in _strings(policy["upload_providers"]):
            _deny()
        envelope = json.loads(row[8], object_pairs_hook=_unique_object)
        if not isinstance(envelope, dict):
            _deny()
        recorded = envelope.pop("envelope_digest")
        if recorded != row[9] or digest("historical-kind-v1", envelope) != recorded:
            _deny()
        kind_uses = _strings(envelope["kind"]["allowed_uses"])
        revision_uses = _strings(json.loads(row[7], object_pairs_hook=_unique_object))
        if not (kind_uses & revision_uses & scope.uses):
            _deny()
