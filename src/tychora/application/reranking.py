"""Bounded canonical retained rows for owner-bound textual reranking."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Lock, get_ident
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NoReturn

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.candidates import CandidateKey, ChannelCandidate
from tychora.contracts.corpus import (
    AssetProxyRow,
    AssetRow,
    ChunkRow,
    ExtractionBundle,
    ExtractionRow,
    IdentifierRow,
    LocatorRow,
    RetainedRepresentationRows,
    RevisionArtifactRow,
    RevisionRow,
    SourceRow,
    StructuredRecordRow,
    UnitAssetRow,
)
from tychora.contracts.embeddings import QueryUploadAuthorization
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.indexing import PreparedExtraction, RepresentationUnit
from tychora.contracts.requests import RequestScope
from tychora.indexing.representations import prepare_extraction
from tychora.ports.execution import ExecutionContext
from tychora.ports.reranking import RankedIndex, RankingInput, RankingSpan
from tychora.ports.tokens import TokenCounter

if TYPE_CHECKING:
    from tokenizers import Tokenizer

    from tychora.adapters.voyage.rerank import VoyageReranker
    from tychora.adapters.voyage.runtime import VoyageRuntime
    from tychora.application.owner import WorkspaceOwner
    from tychora.retrieval.admission import Admission
    from tychora.retrieval.eligibility import EligibilityAttempt
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


def _rows(
    db: sqlite3.Connection,
    sql: str,
    parameters: tuple[str, ...],
    budget: RetainedReadBudget,
    ctx: ExecutionContext,
    *,
    units: bool = False,
) -> tuple[dict[str, Any], ...]:
    cursor = db.execute(sql, parameters)
    columns = tuple(column[0] for column in cursor.description)
    result = []
    for raw in cursor:
        ctx.checkpoint()
        row = dict(zip(columns, raw, strict=True))
        budget.accept(row, unit=units)
        result.append(row)
    return tuple(result)


def _one(
    db: sqlite3.Connection,
    sql: str,
    key: str,
    budget: RetainedReadBudget,
    ctx: ExecutionContext,
) -> dict[str, Any]:
    rows = _rows(db, sql, (key,), budget, ctx)
    if len(rows) != 1:
        raise TychoraError("NOT_FOUND", "Retained extraction is unavailable.")
    return rows[0]


def load_retained_representation(
    db: sqlite3.Connection,
    extraction_id: str,
    budget: RetainedReadBudget,
    ctx: ExecutionContext,
) -> RetainedRepresentationRows:
    """Read exact stored representation facts in one short bounded transaction."""
    if db.in_transaction:
        raise TychoraError(
            "INTEGRITY_ERROR", "Retained read requires a fresh transaction."
        )
    ctx.checkpoint()
    db.execute("BEGIN")
    try:
        extraction_raw = _one(
            db,
            "SELECT * FROM extractions WHERE extraction_id=?",
            extraction_id,
            budget,
            ctx,
        )
        revision_raw = _one(
            db,
            "SELECT * FROM revisions WHERE revision_id=?",
            extraction_raw["revision_id"],
            budget,
            ctx,
        )
        source_raw = _one(
            db,
            "SELECT * FROM sources WHERE source_id=?",
            revision_raw["source_id"],
            budget,
            ctx,
        )
        unit_raw = _rows(
            db,
            "SELECT * FROM evidence_units WHERE extraction_id=? ORDER BY ordinal,unit_id",
            (extraction_id,),
            budget,
            ctx,
            units=True,
        )
        locator_raw = _rows(
            db,
            "SELECT l.* FROM locators l JOIN evidence_units u USING(unit_id) "
            "WHERE u.extraction_id=? ORDER BY l.unit_id,l.fragment_ordinal",
            (extraction_id,),
            budget,
            ctx,
        )
        asset_raw = _rows(
            db,
            "SELECT * FROM assets WHERE extraction_id=? ORDER BY asset_id",
            (extraction_id,),
            budget,
            ctx,
        )
        link_raw = _rows(
            db,
            "SELECT ua.* FROM unit_assets ua JOIN evidence_units u USING(unit_id) "
            "WHERE u.extraction_id=? ORDER BY ua.unit_id,ua.asset_id,ua.role",
            (extraction_id,),
            budget,
            ctx,
        )
        proxy_raw = _rows(
            db,
            "SELECT p.* FROM asset_proxies p JOIN evidence_units u "
            "ON u.unit_id=p.proxy_unit_id WHERE u.extraction_id=? "
            "ORDER BY p.proxy_unit_id",
            (extraction_id,),
            budget,
            ctx,
        )
        proxy_dependency_raw = _rows(
            db,
            "SELECT d.* FROM asset_proxy_dependencies d JOIN evidence_units u "
            "ON u.unit_id=d.proxy_unit_id WHERE u.extraction_id=? "
            "ORDER BY d.proxy_unit_id,d.required_unit_id",
            (extraction_id,),
            budget,
            ctx,
        )
        record_raw = _rows(
            db,
            "SELECT r.* FROM structured_records r JOIN evidence_units u USING(unit_id) "
            "WHERE u.extraction_id=? ORDER BY r.unit_id",
            (extraction_id,),
            budget,
            ctx,
        )
        identifier_raw = _rows(
            db,
            "SELECT i.* FROM identifiers i JOIN evidence_units u USING(unit_id) "
            "WHERE u.extraction_id=? ORDER BY i.unit_id,i.namespace,i.normalized_value",
            (extraction_id,),
            budget,
            ctx,
        )
        revision_artifact_raw = _rows(
            db,
            "SELECT * FROM revision_artifacts WHERE revision_id=? "
            "ORDER BY role,source_ref",
            (revision_raw["revision_id"],),
            budget,
            ctx,
        )
        artifact_raw = _rows(
            db,
            "SELECT a.* FROM artifacts a WHERE a.artifact_sha IN ("
            "SELECT r.original_sha FROM revisions r JOIN extractions e USING(revision_id) "
            "WHERE e.extraction_id=? UNION "
            "SELECT e.document_artifact_sha FROM extractions e WHERE e.extraction_id=? UNION "
            "SELECT e.assertions_artifact_sha FROM extractions e WHERE e.extraction_id=? UNION "
            "SELECT e.diagnostics_artifact_sha FROM extractions e WHERE e.extraction_id=? UNION "
            "SELECT ra.artifact_sha FROM revision_artifacts ra JOIN extractions e USING(revision_id) "
            "WHERE e.extraction_id=? UNION "
            "SELECT l.artifact_sha FROM locators l JOIN evidence_units u USING(unit_id) "
            "WHERE u.extraction_id=? UNION "
            "SELECT a.artifact_sha FROM assets a WHERE a.extraction_id=? UNION "
            "SELECT p.derivation_sha FROM asset_proxies p JOIN evidence_units u "
            "ON u.unit_id=p.proxy_unit_id WHERE u.extraction_id=?"
            ") ORDER BY a.artifact_sha",
            (extraction_id,) * 8,
            budget,
            ctx,
        )
        locators_by_unit: dict[str, list[object]] = defaultdict(list)
        locators = []
        for row in locator_raw:
            locator = json.loads(row["details_json"])
            locators_by_unit[row["unit_id"]].append(locator)
            locators.append(
                LocatorRow.model_validate_json(
                    canonical_bytes(
                        {
                            "locator_id": row["locator_id"],
                            "unit_id": row["unit_id"],
                            "fragment_ordinal": row["fragment_ordinal"],
                            "locator": locator,
                        }
                    )
                )
            )
        units = []
        for raw in unit_raw:
            row = dict(raw)
            row["heading_path"] = json.loads(row.pop("heading_path_json"))
            row["attributes"] = json.loads(row.pop("attributes_json"))
            row["review_state"] = row.pop("declared_review_state")
            row["citable"] = bool(row["citable"])
            row["locators"] = locators_by_unit.get(row["unit_id"], [])
            units.append(EvidenceUnit.model_validate_json(canonical_bytes(row)))
        source_raw.pop("withdrawn_at")
        source_raw["registration_json"] = json.loads(source_raw["registration_json"])
        revision_raw["allowed_uses_json"] = json.loads(
            revision_raw["allowed_uses_json"]
        )
        revision_raw["metadata_json"] = json.loads(revision_raw["metadata_json"])
        proxy_dependencies: dict[str, set[str]] = defaultdict(set)
        for row in proxy_dependency_raw:
            proxy_dependencies[row["proxy_unit_id"]].add(row["required_unit_id"])
        result = RetainedRepresentationRows(
            source=SourceRow.model_validate_json(canonical_bytes(source_raw)),
            artifacts=tuple(
                ArtifactRef.model_validate(
                    {
                        "sha256": row["artifact_sha"],
                        "size_bytes": row["size_bytes"],
                        "media_type": row["media_type"],
                        "storage_key": row["storage_key"],
                    }
                )
                for row in artifact_raw
            ),
            revision=RevisionRow.model_validate_json(canonical_bytes(revision_raw)),
            revision_artifacts=tuple(
                RevisionArtifactRow.model_validate(row) for row in revision_artifact_raw
            ),
            extraction=ExtractionRow.model_validate(extraction_raw),
            units=tuple(units),
            locators=tuple(locators),
            assets=tuple(
                AssetRow.model_validate(
                    {
                        **{
                            key: value
                            for key, value in row.items()
                            if key != "locator_json"
                        },
                        "locator": json.loads(row["locator_json"]),
                    }
                )
                for row in asset_raw
            ),
            unit_assets=tuple(UnitAssetRow.model_validate(row) for row in link_raw),
            proxies=tuple(
                AssetProxyRow.model_validate(
                    {
                        **row,
                        "required_unit_ids": frozenset(
                            proxy_dependencies[row["proxy_unit_id"]]
                        ),
                    }
                )
                for row in proxy_raw
            ),
            records=tuple(
                StructuredRecordRow.model_validate(
                    {**row, "data_json": json.loads(row["data_json"])}
                )
                for row in record_raw
            ),
            identifiers=tuple(
                IdentifierRow.model_validate(row) for row in identifier_raw
            ),
        )
        return result
    except (ValueError, TypeError, KeyError, sqlite3.DatabaseError):
        raise TychoraError(
            "INTEGRITY_ERROR", "Retained representation is invalid."
        ) from None
    finally:
        db.rollback()


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
    """Bind every rendered span and dependency to Task19's canonical ownership."""
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
        return self.owner.run_blocking_sync(call, ctx)

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
            dependencies = await self.owner.run_blocking(
                lambda: self._prepare_canonical(representations, ctx), ctx
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
        owner = self.owner
        budget = RetainedReadBudget()
        by_extraction: dict[str, list[RankingInput]] = defaultdict(list)
        for item in representations:
            ctx.checkpoint()
            kind, candidate_id = item.key
            table, column = (
                ("evidence_units", "unit_id")
                if kind == "unit"
                else ("assets", "asset_id")
            )
            row = owner.db.execute(
                f"SELECT extraction_id FROM {table} WHERE {column}=?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                _deny_ranking()
            budget.accept({"extraction_id": row[0]})
            by_extraction[row[0]].append(item)
        dependencies: set[str] = set()
        for extraction_id, group in by_extraction.items():
            ctx.checkpoint()
            rows = load_retained_representation(owner.db, extraction_id, budget, ctx)
            try:
                prepared = prepare_extraction(rows)
            except (ValueError, TypeError, KeyError, AttributeError):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Retained ownership is invalid."
                ) from None
            ctx.checkpoint()
            for item in group:
                dependencies.update(attest_ranking_input(item, rows, prepared))
                for anchor in item.anchors:
                    if anchor.indexed_window_id is None:
                        continue
                    stored = owner.db.execute(
                        "SELECT unit_id,char_start,char_end FROM chunks "
                        "WHERE chunk_id=? AND extraction_id=?",
                        (anchor.indexed_window_id, extraction_id),
                    ).fetchone()
                    if (
                        stored is None
                        or stored[0] != anchor.unit_id
                        or not stored[1]
                        <= anchor.char_start
                        < anchor.char_end
                        <= stored[2]
                    ):
                        _deny_ranking()
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
    counter = await owner.run_blocking(verified_counter, ctx)
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
        row = owner.db.execute(
            "SELECT policy_epoch FROM workspaces WHERE workspace_id=?",
            (owner.workspace_id,),
        ).fetchone()
        if row is None or runtime.workspace_id != owner.workspace_id:
            raise TychoraError("INTEGRITY_ERROR", "Reranker runtime workspace differs.")
        capabilities = qualify_voyage_rerank(settings.model, counter.tokenizer_id)
        access = OwnerRerankAccess(owner, granted, row[0])
        reranker = VoyageReranker(
            capabilities=capabilities, counter=counter, runtime=runtime, access=access
        )
        owner.register_runtime(runtime)
        return reranker

    return await owner.run_blocking(create, ctx)


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


async def prepare_rerank_facts(
    owner: WorkspaceOwner,
    attempt: EligibilityAttempt,
    admission: Admission,
    channels: Mapping[str, tuple[ChannelCandidate, ...]],
    ctx: ExecutionContext,
) -> Mapping[CandidateKey, RerankFacts]:
    """Hydrate bounded retained source/window facts on the pinned owner's read lane."""
    from tychora.retrieval.rerank import RerankFacts, matches_from_channels

    if attempt.owner is not owner or len(admission.selected) > 64:
        raise TychoraError("INTEGRITY_ERROR", "Rerank attempt owner differs.")

    def load() -> Mapping[CandidateKey, RerankFacts]:
        budget = RetainedReadBudget()
        prepared_by_extraction: dict[
            str, tuple[RetainedRepresentationRows, PreparedExtraction]
        ] = {}
        output: dict[CandidateKey, RerankFacts] = {}
        for candidate in admission.selected:
            ctx.checkpoint()
            kind, candidate_id = candidate.key
            if kind == "asset":
                continue  # Native image ranking is independent of text reranking.
            if candidate_id not in attempt.view.source_unit_ids:
                raise TychoraError(
                    "FORBIDDEN_SCOPE", "Rerank candidate is not eligible."
                )
            row = owner.db.execute(
                "SELECT extraction_id FROM evidence_units WHERE unit_id=?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise TychoraError("INTEGRITY_ERROR", "Rerank candidate is absent.")
            extraction_id = row[0]
            budget.accept({"extraction_id": extraction_id})
            if extraction_id not in prepared_by_extraction:
                rows = load_retained_representation(
                    owner.db, extraction_id, budget, ctx
                )
                try:
                    prepared = prepare_extraction(rows)
                except (ValueError, TypeError, KeyError, AttributeError):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Retained ownership is invalid."
                    ) from None
                prepared_by_extraction[extraction_id] = rows, prepared
            rows, prepared = prepared_by_extraction[extraction_id]
            window_ids = tuple(
                dict.fromkeys(
                    window_id
                    for hits in channels.values()
                    for hit in hits
                    if hit.key == candidate.key
                    for window_id in hit.matching_window_ids
                )
            )
            if len(window_ids) > 2048:
                raise TychoraError("INVALID_REQUEST", "Rerank window limit exceeded.")
            chunks: dict[str, ChunkRow] = {}
            for window_id in window_ids:
                ctx.checkpoint()
                if window_id not in attempt.view.chunk_ids:
                    raise TychoraError(
                        "FORBIDDEN_SCOPE", "Ranking window is not eligible."
                    )
                dependencies = attempt.dependencies_for(window_id)
                allowed = (
                    attempt.view.revision_ids
                    | attempt.view.context_unit_ids
                    | attempt.view.structural_unit_ids
                    | attempt.view.asset_ids
                )
                if not dependencies <= allowed:
                    raise TychoraError(
                        "FORBIDDEN_SCOPE", "Ranking window is not eligible."
                    )
                raw = owner.db.execute(
                    "SELECT * FROM chunks WHERE chunk_id=?", (window_id,)
                ).fetchone()
                if raw is None:
                    raise TychoraError("INTEGRITY_ERROR", "Ranking window is absent.")
                columns = tuple(
                    column[1]
                    for column in owner.db.execute("PRAGMA table_info(chunks)")
                )
                stored = dict(zip(columns, raw, strict=True))
                budget.accept(stored)
                try:
                    unit = prepared.representation(stored["unit_id"]).unit
                    chunk = ChunkRow.model_validate(
                        {
                            **stored,
                            "text": unit.text[
                                stored["char_start"] : stored["char_end"]
                            ],
                        }
                    )
                except (ValueError, TypeError, KeyError, AttributeError):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Ranking window is invalid."
                    ) from None
                chunks[window_id] = chunk
            output[candidate.key] = RerankFacts(
                rows,
                prepared,
                matches_from_channels(
                    candidate, channels, chunks, prepared, attempt.view
                ),
            )
        return MappingProxyType(output)

    return await owner.run_blocking(load, ctx)
