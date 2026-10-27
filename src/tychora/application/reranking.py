"""Bounded canonical retained rows for owner-bound textual reranking."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Lock, get_ident
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NoReturn

from tychora.adapters.sqlite.repositories.errors import facts_missing
from tychora.adapters.sqlite.repositories.slices import retained_rows
from tychora.adapters.sqlite.retained_bindings import BindingScope
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.candidates import CandidateKey, ChannelCandidate
from tychora.contracts.corpus import (
    ExtractionBundle,
    RepresentationSlice,
    RetainedRepresentationRows,
)
from tychora.contracts.embeddings import QueryUploadAuthorization
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphPath
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.indexing import PreparedExtraction, RepresentationUnit
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext
from tychora.ports.repositories import ChunkRow, ReadTransaction
from tychora.ports.reranking import RankedIndex, RankingInput, RankingSpan
from tychora.ports.tokens import TokenCounter

if TYPE_CHECKING:
    from tokenizers import Tokenizer

    from tychora.adapters.voyage.rerank import VoyageReranker
    from tychora.adapters.voyage.runtime import VoyageRuntime
    from tychora.application.owner import WorkspaceOwner
    from tychora.retrieval.admission import Admission
    from tychora.retrieval.attempt import EligibilityAttempt
    from tychora.retrieval.fusion import FusedCandidate
    from tychora.retrieval.rerank import RerankFacts


@dataclass(slots=True)
class RetainedReadBudget:
    """One shared operation budget across all touched extractions."""

    units: int = 0
    rows: int = 0
    text_bytes: int = 0
    row_bytes: int = 0

    def accept(self, row: dict[str, Any], *, unit: bool = False) -> None:
        self.rows += 1
        self.row_bytes += len(canonical_bytes(row))
        if unit:
            self.units += 1
            self.text_bytes += len(row["text"].encode("utf-8"))
        if (
            self.units > 20_000
            or self.rows > 100_000
            or self.text_bytes > 16 * 1024 * 1024
            or self.row_bytes > 32 * 1024 * 1024
        ):
            raise TychoraError(
                "INVALID_REQUEST", "Rerank preparation exceeds its resource limit."
            )


def extraction_rows(
    db: sqlite3.Connection, extraction_id: str
) -> RetainedRepresentationRows:
    """One whole extraction's retained rows; interim until visuals read slices."""
    row = db.execute(
        "SELECT revision_id FROM extractions WHERE extraction_id=?", (extraction_id,)
    ).fetchone()
    if row is None:
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Retained extraction is unavailable.",
            reason="canonical.row_invalid",
        )
    return retained_rows(db, BindingScope(extraction_id, row[0]))


class RerankAttestationError(TychoraError):
    """An invalid canonical representation, distinct from upload policy denial."""


def _deny_ranking() -> NoReturn:
    raise RerankAttestationError(
        "FORBIDDEN_SCOPE", "Ranking input is not canonical in this scope."
    )


def _span_dependencies(span: RankingSpan, unit: RepresentationUnit) -> frozenset[str]:
    if (
        span.char_end > len(unit.unit.text)
        or unit.unit.text[span.char_start : span.char_end] != span.text
    ):
        _deny_ranking()
    dependencies: set[str] = set()
    cursor = span.char_start
    for interval in unit.ownership:
        if interval.char_end <= cursor or interval.char_start >= span.char_end:
            continue
        if interval.char_start > cursor:
            _deny_ranking()
        dependencies.update(interval.dependency_ids)
        cursor = min(span.char_end, interval.char_end)
        if cursor == span.char_end:
            break
    if cursor != span.char_end or span.dependency_ids != dependencies:
        _deny_ranking()
    return frozenset(dependencies)


def _permitted_code_context(
    span: RankingSpan,
    matched_unit_id: str,
    rows: ExtractionBundle | RetainedRepresentationRows,
) -> bool:
    for record in rows.records:
        if record.unit_id != matched_unit_id or record.record_type != "code_symbol":
            continue
        contexts = record.data_json.get("context_spans", ())
        if not isinstance(contexts, tuple):
            return False
        for context in contexts:
            if not isinstance(context, FrozenJsonMap):
                return False
            start, end = context.get("char_start"), context.get("char_end")
            if (
                context["source_unit_id"] == span.unit_id
                and type(start) is int
                and type(end) is int
                and start <= span.char_start
                and span.char_end <= end
                and context["reason"] in {"parent_declaration", "referenced_import"}
            ):
                return True
    return False


def attest_ranking_input(
    item: RankingInput,
    rows: ExtractionBundle | RetainedRepresentationRows,
    prepared: PreparedExtraction,
) -> frozenset[str]:
    """Check that every rendered span and dependency belongs to the canonical owner of its extraction."""
    try:
        if (
            type(item) is not RankingInput
            or type(item.spans) is not tuple
            or any(type(span) is not RankingSpan for span in item.spans)
            or type(item.anchors) is not tuple
            or type(item.dependency_ids) is not frozenset
        ):
            _deny_ranking()
        if RankingInput.model_validate_json(item.model_dump_json()) != item:
            _deny_ranking()
        kind, candidate_id = item.key
        matched = tuple(span for span in item.spans if span.role == "match")
        if not matched:
            _deny_ranking()
        primary = prepared.representation(matched[0].unit_id)
        if (
            item.source_kind != rows.revision.kind_id
            or item.title != rows.revision.title
            or item.heading_path != primary.unit.heading_path
        ):
            _deny_ranking()
        if kind == "unit":
            prepared.representation(candidate_id)
            if any(span.unit_id != candidate_id for span in matched):
                _deny_ranking()
        else:
            if candidate_id not in {asset.asset_id for asset in rows.assets}:
                _deny_ranking()
            links = {
                link.unit_id
                for link in rows.unit_assets
                if link.asset_id == candidate_id and link.role in {"source", "caption"}
            }
            links.update(
                proxy.proxy_unit_id
                for proxy in rows.proxies
                if proxy.asset_id == candidate_id
            )
            if not links or any(span.unit_id not in links for span in matched):
                _deny_ranking()
        dependencies: set[str] = {candidate_id} if kind == "asset" else set()
        for span in item.spans:
            unit = prepared.representation(span.unit_id)
            dependencies.update(_span_dependencies(span, unit))
            if span.role == "context" and not _permitted_code_context(
                span, matched[0].unit_id, rows
            ):
                _deny_ranking()
        if item.dependency_ids != dependencies:
            _deny_ranking()
        for anchor in item.anchors:
            unit = prepared.representation(anchor.unit_id)
            if anchor.char_end > len(unit.unit.text) or not any(
                span.role == "match"
                and span.unit_id == anchor.unit_id
                and span.char_start <= anchor.char_start
                and anchor.char_end <= span.char_end
                for span in matched
            ):
                _deny_ranking()
        return frozenset(dependencies)
    except (ValueError, TypeError, KeyError, AttributeError):
        _deny_ranking()


def _by_extraction(
    tx: ReadTransaction, representations: tuple[RankingInput, ...]
) -> tuple[tuple[str, tuple[RankingInput, ...]], ...]:
    """The inputs grouped by the extraction their candidate belongs to."""
    facts = tx.eligibility.owner_facts(tuple(item.key[1] for item in representations))
    extraction = {(fact.kind, fact.owner_id): fact.extraction_id for fact in facts}
    by_extraction: dict[str, list[RankingInput]] = defaultdict(list)
    for item in representations:
        if item.key not in extraction:
            _deny_ranking()
        by_extraction[extraction[item.key]].append(item)
    return tuple((key, tuple(group)) for key, group in by_extraction.items())


def _rendered_units(group: tuple[RankingInput, ...]) -> tuple[str, ...]:
    """Every unit the inputs' spans and anchors render."""
    return tuple(
        sorted(
            {span.unit_id for item in group for span in item.spans}
            | {anchor.unit_id for item in group for anchor in item.anchors}
        )
    )


def _attest_windows(
    tx: ReadTransaction, item: RankingInput, extraction_id: str
) -> None:
    """Each indexed anchor lies inside its stored window of the same extraction."""
    for anchor in item.anchors:
        if anchor.indexed_window_id is None:
            continue
        stored = tx.representations.chunks((anchor.indexed_window_id,))
        if (
            len(stored) != 1
            or stored[0].extraction_id != extraction_id
            or stored[0].unit_id != anchor.unit_id
            or not stored[0].char_start
            <= anchor.char_start
            < anchor.char_end
            <= stored[0].char_end
        ):
            _deny_ranking()


class OwnerRerankAccess:
    """Operation-local canonical gate bound to one owner and narrowed scope."""

    def __init__(self, owner: WorkspaceOwner, scope: RequestScope, epoch: int) -> None:
        self.owner = owner
        self.scope = scope
        self.epoch = epoch
        self._lock = Lock()
        self._expected: tuple[str, tuple[RankingInput, ...], str] | None = None
        self._dependencies: frozenset[str] = frozenset()
        self._input_shas: tuple[str, ...] = ()

    def _on_owner[T](self, call: Callable[[], T], ctx: ExecutionContext) -> T:
        if get_ident() == self.owner._thread_id:
            ctx.checkpoint()
            return call()
        return self.owner.run_write_sync(call, ctx, "interactive")

    async def prepare(
        self,
        query: str,
        representations: tuple[RankingInput, ...],
        model: str,
        ctx: ExecutionContext,
    ) -> frozenset[str]:
        from tychora.adapters.voyage.rerank import build_rerank_body

        if not self._lock.acquire(blocking=False):
            raise TychoraError("WORKSPACE_BUSY", "Reranker operation is active.")
        try:
            if not representations or len(representations) > 64:
                raise TychoraError(
                    "INVALID_REQUEST", "Rerank candidate count is invalid."
                )
            dependencies = await self.owner.run_write(
                lambda: self._prepare_canonical(representations, ctx),
                ctx,
                "interactive",
            )
            body_sha = sha256(
                build_rerank_body(model, query, representations)
            ).hexdigest()
            self._expected = (query, representations, body_sha)
            self._dependencies = dependencies
            self._input_shas = tuple(
                digest("rerank-input-v1", item.model_dump(mode="json"))
                for item in representations
            )
            return dependencies
        except BaseException:
            self._lock.release()
            raise

    def close_operation(self) -> None:
        self._expected = None
        self._dependencies = frozenset()
        self._input_shas = ()
        self._lock.release()

    def _prepare_canonical(
        self, representations: tuple[RankingInput, ...], ctx: ExecutionContext
    ) -> frozenset[str]:
        """Attest each input against the persisted slice of the units it renders."""
        dependencies: set[str] = set()
        with self.owner.store.uow.read() as tx:
            for extraction_id, group in _by_extraction(tx, representations):
                ctx.checkpoint()
                piece = tx.representations.slice(
                    extraction_id, unit_ids=_rendered_units(group)
                )
                for item in group:
                    dependencies.update(
                        attest_ranking_input(item, piece.rows, piece.prepared)
                    )
                    _attest_windows(tx, item, extraction_id)
        if len(dependencies) > 2048:
            raise TychoraError("INVALID_REQUEST", "Rerank dependency limit exceeded.")
        return frozenset(dependencies)

    def require_prepared(
        self, query: str, representations: tuple[RankingInput, ...], body_sha: str
    ) -> None:
        if self._expected != (query, representations, body_sha):
            _deny_ranking()

    def authorize(self, payload_sha: str, ctx: ExecutionContext) -> None:
        def check() -> None:
            if self._expected is None or self._expected[2] != payload_sha:
                _deny_ranking()
            authorization = QueryUploadAuthorization(
                payload_sha=payload_sha,
                input_shas=self._input_shas,
                collection_ids=self.scope.collection_ids,
                dependency_ids=self._dependencies,
                policy_epoch=self.epoch,
            )
            self.owner.upload_policy.authorize_query(
                self.scope, "voyage", authorization
            )

        self._on_owner(check, ctx)


@dataclass(slots=True)
class VerifiedRerankCounter:
    """Count with the model-bound tokenizer loaded from verified local bytes."""

    model: str
    tokenizer_id: str
    tokenizer: Tokenizer

    def count(self, text: str) -> int:
        return len(self.tokenizer.encode(text).ids)


async def create_reranker(
    owner: WorkspaceOwner,
    *,
    runtime: VoyageRuntime,
    scope: RequestScope,
    cache_root: Path,
    ctx: ExecutionContext,
) -> VoyageReranker:
    """Standard composition requires the exact verified local tokenizer."""
    from tychora.application.setup import SetupService, load_voyage_tokenizer

    def verified_counter() -> VerifiedRerankCounter:
        settings = owner.config.values.providers.rerank
        if settings.model != "rerank-2.5":
            raise TychoraError("CAPABILITY_MISSING", "Reranker is unavailable.")
        (asset,) = SetupService(cache_root, offline=True).plan((settings.model,)).assets
        tokenizer = load_voyage_tokenizer(cache_root, settings.model)
        return VerifiedRerankCounter(
            model=settings.model,
            tokenizer_id=f"{settings.model}:{asset.revision}",
            tokenizer=tokenizer,
        )

    if owner.config.values.offline:
        raise TychoraError("CAPABILITY_MISSING", "Reranker is unavailable.")
    counter = await owner.run_write(verified_counter, ctx)
    return await _create_injected_reranker(
        owner, runtime=runtime, scope=scope, counter=counter, ctx=ctx
    )


async def _create_injected_reranker(
    owner: WorkspaceOwner,
    *,
    runtime: VoyageRuntime,
    scope: RequestScope,
    counter: TokenCounter,
    ctx: ExecutionContext,
) -> VoyageReranker:
    """Privileged explicit injection for synthetic tests and trusted extensions."""
    from tychora.adapters.voyage.rerank import VoyageReranker
    from tychora.adapters.voyage.validation import qualify_voyage_rerank

    def create() -> VoyageReranker:
        settings = owner.config.values.providers.rerank
        if (
            owner.config.values.offline
            or settings.provider != "voyage"
            or settings.model is None
            or settings.credential_ref is None
        ):
            raise TychoraError("CAPABILITY_MISSING", "Reranker is unavailable.")
        granted = owner.scope.narrow(scope)
        epoch = owner.policy_epoch()
        if runtime.workspace_id != owner.workspace_id:
            raise TychoraError("INTEGRITY_ERROR", "Reranker runtime workspace differs.")
        capabilities = qualify_voyage_rerank(settings.model, counter.tokenizer_id)
        access = OwnerRerankAccess(owner, granted, epoch)
        reranker = VoyageReranker(
            capabilities=capabilities, counter=counter, runtime=runtime, access=access
        )
        owner.register_runtime(runtime)
        return reranker

    return await owner.run_write(create, ctx)


async def rank_with_owner(
    owner: WorkspaceOwner,
    reranker: VoyageReranker,
    query: str,
    representations: tuple[RankingInput, ...],
    ctx: ExecutionContext,
) -> tuple[RankedIndex, ...]:
    """Run one complete scored pool in the owner's bounded provider lane."""
    access = reranker._access
    if access.owner is not owner:
        _deny_ranking()
    dependencies = await access.prepare(
        query, representations, reranker.capabilities.model, ctx
    )
    try:
        return await owner.run_provider(
            "voyage",
            lambda: reranker.rank(query, representations, ctx),
            ctx,
            scope=access.scope,
            dependencies=dependencies,
            policy_epoch=access.epoch,
        )
    finally:
        access.close_operation()


NEIGHBOUR_RADIUS = 2


def _window_ids(
    candidate: FusedCandidate, channels: Mapping[str, tuple[ChannelCandidate, ...]]
) -> tuple[str, ...]:
    """The candidate's producer windows across every channel, first seen first."""
    windows = tuple(
        dict.fromkeys(
            window_id
            for hits in channels.values()
            for hit in hits
            if hit.key == candidate.key
            for window_id in hit.matching_window_ids
        )
    )
    if len(windows) > 2048:
        raise TychoraError(
            "INVALID_REQUEST",
            "Rerank window limit exceeded.",
            reason="request.limit_exceeded",
        )
    return windows


def _continuation_targets(unit_id: str, paths: tuple[GraphPath, ...]) -> set[str]:
    """The other ends of the continuation paths that touch the unit."""
    return {
        end
        for path in paths
        if unit_id in (path.source_unit_id, path.target_unit_id)
        for end in (path.source_unit_id, path.target_unit_id)
    } - {unit_id}


def _candidate_slice(
    tx: ReadTransaction,
    unit_id: str,
    chunks: Mapping[str, ChunkRow],
    paths: tuple[GraphPath, ...],
    files: dict[tuple[str, str], RepresentationSlice],
) -> RepresentationSlice:
    """The candidate's units, windows, neighbours and continuation targets; a code file whole.

    File slices are read once per ``(extraction, file)`` through ``files``.
    """
    targets = _continuation_targets(unit_id, paths)
    found = {row.unit_id: row for row in tx.representations.units((unit_id, *targets))}
    if unit_id not in found:
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Rerank candidate is absent.",
            reason="canonical.row_invalid",
        )
    extraction_id = found[unit_id].extraction_id
    relative_path = tx.representations.code_path(unit_id)
    if relative_path is not None:
        key = (extraction_id, relative_path)
        if key not in files:
            files[key] = tx.representations.slice(
                extraction_id, relative_path=relative_path
            )
        return files[key]
    needed = {unit_id, *(chunk.unit_id for chunk in chunks.values())}
    needed.update(t for t in targets if found[t].extraction_id == extraction_id)
    needed.update(
        row.unit_id for row in tx.representations.neighbours(unit_id, NEIGHBOUR_RADIUS)
    )
    return tx.representations.slice(extraction_id, unit_ids=tuple(sorted(needed)))


def _persisted_sets(
    tx: ReadTransaction, piece: RepresentationSlice, windows: tuple[str, ...]
) -> Mapping[str, frozenset[str]]:
    """The persisted sets of the slice's units and the windows; one missing is refused."""
    requested = (*(unit.unit_id for unit in piece.rows.units), *windows)
    dependencies = tx.representations.dependencies(requested)
    if not set(requested) <= dependencies.keys():
        raise facts_missing()
    return dependencies


async def prepare_rerank_facts(
    owner: WorkspaceOwner,
    attempt: EligibilityAttempt,
    admission: Admission,
    channels: Mapping[str, tuple[ChannelCandidate, ...]],
    ctx: ExecutionContext,
    *,
    paths: tuple[GraphPath, ...] = (),
) -> Mapping[CandidateKey, RerankFacts]:
    """Read each admitted text candidate's slice and persisted sets on the read pool."""
    from tychora.retrieval.rerank import RerankFacts, matches_from_channels

    if attempt.owner is not owner or len(admission.selected) > 64:
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Rerank attempt owner differs.",
            reason="invariant.unexpected",
        )
    view = attempt.view

    def load(tx: ReadTransaction) -> Mapping[CandidateKey, RerankFacts]:
        output: dict[CandidateKey, RerankFacts] = {}
        files: dict[tuple[str, str], RepresentationSlice] = {}
        for candidate in admission.selected:
            ctx.checkpoint()
            kind, candidate_id = candidate.key
            if kind == "asset":
                continue  # Native image ranking is independent of text reranking.
            windows = _window_ids(candidate, channels)
            chunks = {row.chunk_id: row for row in tx.representations.chunks(windows)}
            piece = _candidate_slice(tx, candidate_id, chunks, paths, files)
            dependencies = _persisted_sets(tx, piece, windows)
            output[candidate.key] = RerankFacts(
                piece.rows,
                piece.prepared,
                matches_from_channels(
                    candidate, channels, chunks, piece.prepared, view, dependencies
                ),
                dependencies=dependencies,
            )
        return MappingProxyType(output)

    return await owner.run_read(load, ctx)
