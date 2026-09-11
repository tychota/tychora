"""Sole workspace owner and thread-affine canonical resources."""

import json
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, get_ident
from typing import NoReturn
from uuid import UUID, uuid4

from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.configuration.models import ResolvedConfig
from tychora.configuration.registry import Registry
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import JsonValue, digest
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext

from .leases import LeaseSet
from .runtime import (
    BoundedWorker,
    RuntimeIdentity,
    drain_result,
    parser_process,
    private_directory,
)


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

    async def run_blocking[T](self, call: Callable[[], T], ctx: ExecutionContext) -> T:
        ctx.checkpoint()

        def checked() -> T:
            ctx.checkpoint()
            return call()

        return await drain_result(self._worker.submit(checked), ctx)

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

    async def run_parser[T](
        self,
        call: Callable[[tuple[Path, ...]], T],
        inputs: tuple[ArtifactRef, ...],
        ctx: ExecutionContext,
    ) -> T:
        ctx.checkpoint()

        def parse() -> T:
            ctx.checkpoint()
            directory, copies = self._worker.submit(
                lambda: self._stage(inputs, ctx)
            ).result()
            try:
                ctx.checkpoint()
                return parser_process(call, copies)
            finally:
                shutil.rmtree(directory)

        return await drain_result(self._parsers.submit(parse), ctx)

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
            epoch = owner.db.execute(
                "SELECT policy_epoch FROM workspaces WHERE workspace_id=?",
                (owner.workspace_id,),
            ).fetchone()
            if (
                epoch is None
                or type(policy_epoch) is not int
                or epoch[0] != policy_epoch
            ):
                raise TychoraError(
                    "SCOPE_CHANGED",
                    "Workspace policy changed; retry authorization",
                    retryable=True,
                )
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
                self._check_revision(revision, provider, granted)
                if len(pending) + len(children) > 4096:
                    _deny()
                pending.extend((child, False) for child in children)
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
