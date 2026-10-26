"""Build complete source-local projections before canonical publication."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from io import BytesIO
from typing import Literal, NamedTuple, NoReturn

from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.adapters.sqlite.graph import read_graph_build
from tychora.adapters.sqlite.publication import (
    check_publication_target,
    register_fresh,
)
from tychora.application.collection_policy import (
    require_active_collection,
)
from tychora.configuration.indexed_representation import pinned_runtime_compatible
from tychora.configuration.models import ResolvedConfig
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import ChunkRow, ExtractionBundle
from tychora.contracts.embeddings import (
    EmbeddingCapabilities,
    EmbeddingRecord,
    VisualInput,
    VisualResizeDerivation,
    stable_capability_json,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import (
    GraphBatchReceipt,
    GraphBuildInput,
    GraphExtraction,
    GraphGenerationSpec,
    GraphManifest,
)
from tychora.contracts.identity import (
    canonical_bytes,
    digest,
    new_family_nonce,
    vector_record_id,
)
from tychora.contracts.identity import family_id as derive_family_id
from tychora.contracts.indexing import (
    ContextGroup,
    FamilySpec,
    PreparedExtraction,
    RepresentationFacts,
    SearchWindow,
)
from tychora.contracts.operations import JobHandle
from tychora.contracts.publication import (
    LexicalBatchReceipt,
    LexicalRecord,
    NewBatch,
    PublicationPlan,
    SnapshotMember,
    SnapshotView,
    VectorRecordBinding,
    snapshot_root_document,
)
from tychora.contracts.snapshots import (
    IndexFamily,
    MembershipPage,
    PageBatch,
    PageMember,
    SlotFamily,
    SnapshotBinding,
    bucket_of,
    compute_page_id,
    snapshot_id_v2,
)
from tychora.contracts.vector_batches import (
    VECTOR_BATCH_MEDIA_TYPE,
    VectorBatchRow,
    encode_vector_batch,
)
from tychora.contracts.vectors import BatchReceipt, VectorBatch, VectorOwner
from tychora.indexing.groups import build_groups, group_facts
from tychora.indexing.planning import member_count_after, next_page
from tychora.indexing.receipts import receipt_digest
from tychora.indexing.representations import (
    ChunkSpan,
    prepare_extraction,
    representation_facts,
)
from tychora.indexing.windows import window_unit
from tychora.ports.artifacts import Artifacts
from tychora.ports.embeddings import OwnerBoundVisualEmbedder, TextEmbedder
from tychora.ports.execution import ExecutionContext
from tychora.ports.graph import GraphStore
from tychora.ports.repositories import BatchRow, VectorRecordRow, WriteTransaction
from tychora.ports.tokens import TokenCounter
from tychora.ports.vectors import VectorIndex


def _fail(message: str, reason: str = "canonical.binding_mismatch") -> NoReturn:
    raise TychoraError("INTEGRITY_ERROR", message, reason=reason)


def _text_bindings(
    stored: list[EmbeddingRecord],
    owners: dict[str, tuple[str, ContextGroup]],
    positions: dict[str, int],
) -> list[VectorRecordBinding]:
    """Bind each stored text record, in record order, to its window, group and row."""
    return [
        VectorRecordBinding(
            record_id=row.record_id,
            chunk_id=owners[row.record_id][0],
            input_sha=row.input_sha,
            value_sha=row.value_sha,
            group_id=owners[row.record_id][1].group_id,
            dependency_ids=owners[row.record_id][1].dependency_ids,
            row_index=positions[row.record_id],
        )
        for row in sorted(stored, key=lambda row: row.record_id)
    ]


def _text_owners(
    bundle: ExtractionBundle,
    windows: tuple[SearchWindow, ...],
    chunk_ids: dict[str, str],
) -> tuple[VectorOwner, ...]:
    """Own each text vector record, keyed by ID, by the unit of its window."""
    units = {window.chunk_id: window.unit_id for window in windows}
    return tuple(
        VectorOwner(
            record_id=record_id,
            revision_id=bundle.revision.revision_id,
            extraction_id=bundle.extraction.extraction_id,
            owner_kind="unit",
            owner_id=units[chunk_id],
        )
        for record_id, chunk_id in sorted(chunk_ids.items())
    )


def _asset_owners(
    bundle: ExtractionBundle, asset_ids: dict[str, str]
) -> tuple[VectorOwner, ...]:
    """Own each image vector record, keyed by ID, by its asset."""
    return tuple(
        VectorOwner(
            record_id=record_id,
            revision_id=bundle.revision.revision_id,
            extraction_id=bundle.extraction.extraction_id,
            owner_kind="asset",
            owner_id=asset_id,
        )
        for record_id, asset_id in sorted(asset_ids.items())
    )


def _input_manifest(
    artifacts: Artifacts,
    entries: tuple[str, ...],
    representations: tuple[dict[str, object], ...] | None = None,
) -> ArtifactRef:
    return artifacts.put_stream(
        BytesIO(
            canonical_bytes(
                {
                    "type": "index_input",
                    "schema_version": "1",
                    "data": {
                        "entries": entries,
                        **(
                            {"representations": representations}
                            if representations is not None
                            else {}
                        ),
                    },
                }
            )
        ),
        "application/json",
    )


def _ignore_stage(stage: str) -> None:
    """Drop a job-stage marker when no job records stages."""


def _require_not_pruned(row: BatchRow | None) -> None:
    """Refuse a batch whose native records were pruned; it is rebuilt natively first."""
    if row is not None and row.state == "pending":
        raise TychoraError(
            "INDEX_INCOMPLETE",
            "The batch's native records were pruned; rebuild the native index",
            reason="batch.missing",
        )


def _batch_id(family_id: str, extraction_id: str | None, ref: ArtifactRef) -> str:
    return digest("index-batch-v1", family_id, extraction_id, ref.sha256)


@dataclass(frozen=True)
class PreparedProjection:
    """Everything an add resolved before its provider calls and batch writes."""

    bundle: ExtractionBundle
    base_snapshot_id: str | None
    base: SnapshotView | None
    base_page: MembershipPage | None
    job: JobHandle
    config_ref: ArtifactRef
    windows: tuple[SearchWindow, ...]
    old_families: dict[str, SlotFamily]
    lexical_family: IndexFamily
    text_family: IndexFamily | None
    text_spec: FamilySpec | None
    text_groups: tuple[ContextGroup, ...]
    image_family: IndexFamily | None
    image_spec: FamilySpec | None
    visual_inputs: tuple[VisualInput, ...]
    reused_batch_ids: tuple[str, ...] = ()


class _FamilyRequest(NamedTuple):
    channel: Literal["text", "image"]
    workspace_id: str
    config_sha: str
    family_config: str
    creation_uuid: str
    created_at: str


class _VectorSlot(NamedTuple):
    """One vector slot of an add: its family, specification and provider records."""

    slot: str
    family: IndexFamily
    spec: FamilySpec
    records: tuple[EmbeddingRecord, ...]


class _VectorOutput(NamedTuple):
    """A vector batch's native receipt, artifact and record bindings."""

    receipt: BatchReceipt
    vectors: ArtifactRef | None
    bindings: Sequence[VectorRecordBinding]


@dataclass(frozen=True)
class _PlanInputs:
    """The bindings a publication plan is built from."""

    job: JobHandle
    workspace_id: str
    base: SnapshotView | None
    base_page: MembershipPage | None
    config_ref: ArtifactRef
    families: tuple[SlotFamily, ...]
    page: MembershipPage
    written: tuple[NewBatch, ...]
    root_batches: tuple[PageBatch, ...] = ()
    watermark: int | None = None


def create_family(
    tx: WriteTransaction, family: IndexFamily, nonce: str, identity_sha: str
) -> None:
    """Create a family unless another nonce already claimed its identity.

    The nonce lookup repeats inside the write transaction, so two writers that
    picked different nonces cannot both create a family for one identity.
    """
    claimed = tx.families.nonce(family.channel, identity_sha)
    if claimed not in (None, nonce):
        raise TychoraError(
            "WORKSPACE_BUSY",
            "A concurrent index family creation claimed this identity first.",
            reason="family.claimed",
        )
    tx.families.create(family, nonce, identity_sha)


class ProjectionCoordinator:
    """Use real canonical, provider and native ports; return a verified plan."""

    @staticmethod
    def _missing_family_proof() -> TychoraError:
        return TychoraError(
            "CAPABILITY_MISSING",
            "Retained vector family predates capability proof; create a new workspace "
            "and reingest available originals to publish a new family and snapshot",
        )

    def _require_family_proof(
        self, family_id: str, channel: str, config_sha: str
    ) -> None:
        rows = self.store.db.execute(
            "SELECT json_type(config_json,'$.capabilities') FROM index_families "
            "WHERE family_id=? OR (channel=? AND "
            "json_extract(config_json,'$.effective_config_sha')=?)",
            (family_id, channel, config_sha),
        ).fetchall()
        if any(row[0] != "object" for row in rows):
            raise self._missing_family_proof()

    def _retained_vector_spec(
        self, slot: SlotFamily | None, channel: str
    ) -> FamilySpec | None:
        if slot is None:
            return None
        embedder = self.text_embedder if channel == "text" else self.visual_embedder
        index = self.text_index if channel == "text" else self.image_index
        if embedder is None or index is None:
            return None
        if not isinstance(slot.family.config_json.get("capabilities"), FrozenJsonMap):
            raise self._missing_family_proof()
        origin_sha = slot.family.config_json.get("effective_config_sha")
        raw = slot.family.config_json.get("spec")
        if not isinstance(origin_sha, str) or not isinstance(raw, FrozenJsonMap):
            _fail("Retained vector family origin is invalid")
        retained = self.store.retained_config_sha(origin_sha)
        if not pinned_runtime_compatible(retained, self.config):
            return None
        try:
            spec = FamilySpec.model_validate_json(canonical_bytes(raw))
        except (TypeError, ValueError):
            _fail("Retained vector family specification is invalid")
        caps = embedder.capabilities
        raw_capabilities = slot.family.config_json["capabilities"]
        try:
            retained_caps = EmbeddingCapabilities.model_validate_json(
                canonical_bytes(raw_capabilities)
            )
        except (TypeError, ValueError):
            _fail("Retained vector family capability proof is invalid")
        if retained_caps != caps:
            _fail("Retained vector family differs from provider")
        settings = getattr(retained.values.providers, channel)
        expected = digest(
            f"{channel}-family-config-v1",
            origin_sha,
            settings.model_dump(mode="json"),
            raw_capabilities,
        )
        if (
            spec.family_id != slot.family.family_id
            or spec.config_sha != expected
            or spec.provider != caps.provider
            or spec.model != caps.model
            or spec.dimensions != caps.dimensions
            or spec.representation_fingerprint != caps.representation_fingerprint
        ):
            _fail("Retained vector family differs from provider")
        handle = index.create_family(spec)
        if handle.config_sha != slot.family.config_sha:
            _fail("Retained native vector configuration differs")
        self.store.vector_indexes[spec.family_id] = index
        return spec

    def __init__(
        self,
        *,
        store: CorpusStore,
        artifacts: Artifacts,
        config: ResolvedConfig,
        counter: TokenCounter,
        text_embedder: TextEmbedder | None = None,
        local_text_embedder: TextEmbedder | None = None,
        text_index: VectorIndex | None = None,
        visual_embedder: OwnerBoundVisualEmbedder | None = None,
        image_index: VectorIndex | None = None,
        graph_factory: Callable[[GraphBuildInput], GraphStore] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.config = config
        self.counter = counter
        self.text_embedder = text_embedder
        self.local_text_embedder = local_text_embedder
        self.text_index = text_index
        self.visual_embedder = visual_embedder
        self.image_index = image_index
        self.graph_factory = graph_factory

    def _config_ref(self, job: JobHandle) -> ArtifactRef:
        row = self.store.db.execute(
            "SELECT a.artifact_sha,a.size_bytes,a.media_type,a.storage_key,j.state "
            "FROM jobs j JOIN artifacts a ON a.artifact_sha=j.effective_config_sha "
            "WHERE j.job_id=?",
            (job.id,),
        ).fetchone()
        if row is None or row[4] != "running":
            _fail("Projection job is not running")
        return ArtifactRef.from_row(row)

    def _windows(
        self, prepared: PreparedExtraction, kind_id: str
    ) -> tuple[SearchWindow, ...]:
        level = self.store.registry.context_level(kind_id)
        return tuple(
            window
            for item in prepared.units
            for window in window_unit(item, self.counter, context_level=level)
        )

    def _canonical_bundle(
        self, bundle: ExtractionBundle, windows: tuple[SearchWindow, ...]
    ) -> ExtractionBundle:
        chunks = tuple(
            ChunkRow(
                chunk_id=window.chunk_id,
                unit_id=window.unit_id,
                extraction_id=window.extraction_id,
                ordinal_in_unit=ordinal,
                char_start=window.char_start,
                char_end=window.char_end,
                context_group_key=window.context_group_key,
                chunker_fingerprint=window.chunker_fingerprint,
                token_count=window.token_count,
                text=window.text,
            )
            for ordinal, window in enumerate(windows)
        )
        if bundle.chunks and bundle.chunks != chunks:
            _fail("Canonical chunk rows differ from exact representation windows")
        return bundle.model_copy(update={"chunks": chunks})

    def _commit_or_reuse(
        self, bundle: ExtractionBundle, facts: RepresentationFacts
    ) -> None:
        db = self.store.db
        row = db.execute(
            "SELECT revision_id,parser_fingerprint,normalizer_fingerprint,document_artifact_sha,assertions_artifact_sha,diagnostics_artifact_sha "
            "FROM extractions WHERE extraction_id=?",
            (bundle.extraction.extraction_id,),
        ).fetchone()
        if row is not None:
            expected = (
                bundle.extraction.revision_id,
                bundle.extraction.parser_fingerprint,
                bundle.extraction.normalizer_fingerprint,
                bundle.extraction.document_artifact_sha,
                bundle.extraction.assertions_artifact_sha,
                bundle.extraction.diagnostics_artifact_sha,
            )
            if row != expected:
                _fail(
                    "Existing canonical extraction conflicts with fresh parser output"
                )
            self.store._check_membership(bundle)
            with self.store.uow.write() as tx:
                tx.representations.write(facts)
            # Fresh capture timestamps and registered path observations do not
            # replace an already committed canonical source/revision/extraction.
            return
        source = db.execute(
            "SELECT collection_id,source_key,source_type,registration_json,access_floor,created_at "
            "FROM sources WHERE source_id=?",
            (bundle.source.source_id,),
        ).fetchone()
        revision = db.execute(
            "SELECT source_id,kind_id,original_sha,edition_key,metadata_sha,title,published_on,effective_from,effective_until,access_rank,declared_review_state,allowed_uses_json,metadata_json,captured_at "
            "FROM revisions WHERE revision_id=?",
            (bundle.revision.revision_id,),
        ).fetchone()
        if source is not None:
            actual = (
                bundle.source.collection_id,
                bundle.source.source_key,
                bundle.source.source_type,
                canonical_bytes(bundle.source.registration_json).decode(),
                bundle.source.access_floor,
            )
            if source[:5] != actual:
                _fail("Existing source registration conflicts with immutable identity")
            bundle = bundle.model_copy(
                update={
                    "source": bundle.source.model_copy(update={"created_at": source[5]})
                }
            )
        if revision is not None:
            actual = (
                bundle.revision.source_id,
                bundle.revision.kind_id,
                bundle.revision.original_sha,
                bundle.revision.edition_key,
                bundle.revision.metadata_sha,
                bundle.revision.title,
                bundle.revision.published_on,
                bundle.revision.effective_from,
                bundle.revision.effective_until,
                bundle.revision.access_rank,
                bundle.revision.declared_review_state,
                canonical_bytes(bundle.revision.allowed_uses_json).decode(),
                canonical_bytes(bundle.revision.metadata_json).decode(),
            )
            if revision[:13] != actual:
                _fail("Existing revision conflicts with immutable identity")
            bundle = bundle.model_copy(
                update={
                    "revision": bundle.revision.model_copy(
                        update={"captured_at": revision[13]}
                    )
                }
            )
        self.store.commit_extraction(bundle, facts)

    def _family_nonce(self, channel: str, identity_sha: str) -> str:
        """Return the nonce of the family already created for this identity, or a new one."""
        with self.store.uow.read() as tx:
            existing = tx.families.nonce(channel, identity_sha)
        return existing or new_family_nonce()

    def _persist_family(
        self, family: IndexFamily, nonce: str, identity_sha: str
    ) -> None:
        """Store the family row before any batch references it."""
        with self.store.uow.write() as tx:
            create_family(tx, family, nonce, identity_sha)

    def _lexical_family(
        self, workspace_id: str, config_sha: str, created_at: str
    ) -> IndexFamily:
        """Return the lexical family of this configuration, creating it once."""
        nonce = self._family_nonce("lexical", config_sha)
        family = IndexFamily(
            family_id=derive_family_id(workspace_id, config_sha, nonce),
            channel="lexical",
            backend="sqlite-fts5",
            config_sha=config_sha,
            config_json=FrozenJsonMap(
                {"tokenizer": "unicode61", "effective_config_sha": config_sha}
            ),
            backend_namespace="lexical",
            created_at=created_at,
        )
        self._persist_family(family, nonce, config_sha)
        return family

    @staticmethod
    def _lexical_records(
        bundle: ExtractionBundle, windows: tuple[SearchWindow, ...]
    ) -> tuple[LexicalRecord, ...]:
        """Return the searchable fields of every window of one extraction."""
        return tuple(
            LexicalRecord(
                chunk_id=window.chunk_id,
                identifiers=" ".join(
                    sorted(
                        row.normalized_value
                        for row in bundle.identifiers
                        if row.unit_id == window.unit_id
                    )
                ),
                title=window.title,
                headings=" ".join(window.heading_path),
                body=window.text,
                context=" ".join(window.symbol_path),
            )
            for window in windows
        )

    def _ready_batch(
        self, family: IndexFamily, bundle: ExtractionBundle
    ) -> BatchRow | None:
        """Return the extraction's ready batch in ``family``, or ``None`` to build it.

        A batch whose native records were pruned must be rebuilt natively first.
        """
        with self.store.uow.read() as tx:
            row = tx.batches.find(family.family_id, bundle.extraction.extraction_id)
        _require_not_pruned(row)
        return row if row is not None and row.state == "ready" else None

    def _store_batch(
        self, batch: NewBatch, rows: Callable[[WriteTransaction], None]
    ) -> None:
        """Write one batch, its rows and its ready state in one transaction."""
        with self.store.uow.write() as tx:
            for ref in (batch.input_manifest, batch.vectors):
                if ref is not None:
                    register_fresh(self.store, tx, ref)
            tx.batches.begin(batch)
            rows(tx)
            tx.batches.mark_ready(batch.batch_id, batch.count)

    def _lexical_page_batch(
        self, prepared: PreparedProjection, written: list[NewBatch]
    ) -> PageBatch:
        """Reuse the extraction's ready lexical batch or write it once."""
        family, bundle = prepared.lexical_family, prepared.bundle
        existing = self._ready_batch(family, bundle)
        if existing is not None:
            return existing.page_batch()
        records = self._lexical_records(bundle, prepared.windows)
        entries = tuple(row.chunk_id for row in records)
        manifest = _input_manifest(self.artifacts, entries)
        batch = NewBatch(
            slot="lexical",
            batch_id=_batch_id(
                family.family_id, bundle.extraction.extraction_id, manifest
            ),
            family_id=family.family_id,
            extraction_id=bundle.extraction.extraction_id,
            input_manifest=manifest,
            expected_count=len(records),
            receipt=LexicalBatchReceipt(
                family_id=family.family_id,
                count=len(records),
                entry_ids_sha=digest("index-entries-v1", entries),
            ),
        )
        self._store_batch(
            batch, lambda tx: tx.batches.write_lexical(batch.batch_id, records)
        )
        written.append(batch)
        return batch.page_batch()

    def _vector_batch(
        self,
        slot: str,
        bundle: ExtractionBundle,
        manifest: ArtifactRef,
        out: _VectorOutput,
        facts: RepresentationFacts | None = None,
    ) -> NewBatch:
        """Store a vector batch's record rows and mark it ready in one transaction."""
        batch = NewBatch(
            slot=slot,
            batch_id=_batch_id(
                out.receipt.family_id, bundle.extraction.extraction_id, manifest
            ),
            family_id=out.receipt.family_id,
            extraction_id=bundle.extraction.extraction_id,
            input_manifest=manifest,
            expected_count=len(out.bindings),
            receipt=out.receipt,
            vectors=out.vectors,
        )
        rows = tuple(
            VectorRecordRow(
                record_id=b.record_id,
                chunk_id=b.chunk_id,
                asset_id=b.asset_id,
                input_sha=b.input_sha,
                row_index=b.row_index,
            )
            for b in out.bindings
        )

        def write(tx: WriteTransaction) -> None:
            tx.batches.write_vectors(batch.batch_id, rows)
            if facts is not None:
                tx.representations.write(facts)

        self._store_batch(batch, write)
        return batch

    def _text_family(
        self, workspace_id: str, config_sha: str, created_at: str
    ) -> tuple[IndexFamily, FamilySpec]:
        embedder = self.text_embedder
        index = self.text_index
        settings = self.config.values.providers.text
        if embedder is None or index is None:
            raise TychoraError(
                "CAPABILITY_MISSING",
                "Configured text projection lacks a qualified provider or native index",
            )
        capabilities = embedder.capabilities
        if (
            capabilities.provider != settings.provider
            or capabilities.model != settings.model
            or capabilities.dimensions != settings.dimensions
            or capabilities.tokenizer_id != self.counter.tokenizer_id
            or capabilities.modality != "text"
        ):
            _fail("Text provider differs from configured representation space")
        family_config = digest(
            "text-family-config-v1",
            config_sha,
            settings.model_dump(mode="json"),
            stable_capability_json(capabilities),
        )
        creation_uuid = self._family_nonce("text", family_config)
        return self._register_vector_family(
            _FamilyRequest(
                "text",
                workspace_id,
                config_sha,
                family_config,
                creation_uuid,
                created_at,
            ),
            capabilities,
            index,
        )

    def _register_vector_family(
        self, request: _FamilyRequest, capabilities, index
    ) -> tuple[IndexFamily, FamilySpec]:
        """Create one vector family natively, persist it and register its index."""
        channel, workspace_id, config_sha = (
            request.channel,
            request.workspace_id,
            request.config_sha,
        )
        family_config, creation_uuid = request.family_config, request.creation_uuid
        created_at = request.created_at
        modality = "text" if channel == "text" else "visual"
        spec = FamilySpec(
            family_id=derive_family_id(workspace_id, family_config, creation_uuid),
            workspace_id=workspace_id,
            config_sha=family_config,
            creation_uuid=creation_uuid,
            modality=modality,
            provider=capabilities.provider,
            model=capabilities.model,
            dimensions=capabilities.dimensions,
            metric="cosine",
            normalization="unit-l2",
            representation_fingerprint=capabilities.representation_fingerprint,
            backend_namespace=channel,
            backend_space="ip",
        )
        self._require_family_proof(spec.family_id, channel, config_sha)
        handle = index.create_family(spec)
        family = IndexFamily(
            family_id=spec.family_id,
            channel=channel,
            backend="chroma",
            provider=spec.provider,
            model=spec.model,
            dimension=spec.dimensions,
            config_sha=handle.config_sha,
            config_json=FrozenJsonMap(
                {
                    "effective_config_sha": config_sha,
                    "spec": spec.model_dump(mode="json"),
                    "capabilities": stable_capability_json(capabilities),
                    "namespace_hash": handle.namespace_hash,
                }
            ),
            backend_namespace=spec.backend_namespace,
            created_at=created_at,
        )
        self._persist_family(family, creation_uuid, family_config)
        self.store.vector_indexes[spec.family_id] = index
        return family, spec

    def _text_batch(
        self,
        bundle: ExtractionBundle,
        windows: tuple[SearchWindow, ...],
        family: IndexFamily,
        spec: FamilySpec,
        groups: tuple[ContextGroup, ...],
        records: tuple[EmbeddingRecord, ...],
        ctx: ExecutionContext,
    ) -> NewBatch:
        assert self.text_embedder is not None and self.text_index is not None
        expected = {
            digest(
                "embedding-record-v1",
                window_id,
                group.input_sha,
                self.text_embedder.capabilities.space_id,
            ): (window_id, group)
            for group in groups
            for window_id in group.window_ids
        }
        ctx.checkpoint()
        if len(records) != len(expected) or {item.record_id for item in records} != set(
            expected
        ):
            _fail("Text provider returned incomplete or foreign window vectors")
        stored = []
        owners = {}
        for item in records:
            window_id, group = expected[item.record_id]
            if (
                item.input_sha != group.input_sha
                or item.space_id != self.text_embedder.capabilities.space_id
                or len(item.values) != spec.dimensions
            ):
                _fail("Text provider vector differs from exact requested input")
            record_id = vector_record_id(family.family_id, window_id, item.input_sha)
            stored.append(item.model_copy(update={"record_id": record_id}))
            owners[record_id] = (window_id, group)
        vectors, positions = self._vector_artifact(family, spec.dimensions, stored)
        bindings = _text_bindings(stored, owners, positions)
        receipt = self._native_receipt(
            self.text_index,
            spec,
            stored,
            ctx,
            _text_owners(
                bundle, windows, {key: value[0] for key, value in owners.items()}
            ),
        )
        entries = tuple(row.record_id for row in bindings)
        manifest = _input_manifest(
            self.artifacts,
            entries,
            tuple(
                {
                    "record_id": binding.record_id,
                    "group_id": binding.group_id,
                    "dependency_ids": sorted(binding.dependency_ids),
                }
                for binding in bindings
            ),
        )
        return self._vector_batch(
            "text",
            bundle,
            manifest,
            _VectorOutput(receipt, vectors, bindings),
            group_facts(bundle.extraction.extraction_id, groups),
        )

    def _vector_artifact(
        self,
        family: IndexFamily,
        dimension: int,
        records: Sequence[EmbeddingRecord],
    ) -> tuple[ArtifactRef | None, dict[str, int]]:
        """Store the batch's float32 rows once; return the artifact and row positions."""
        if not records:
            return None, {}
        rows = tuple(
            VectorBatchRow(
                record_id=record.record_id,
                input_sha=record.input_sha,
                values=record.values,
            )
            for record in sorted(records, key=lambda record: record.record_id)
        )
        ref = self.artifacts.put_stream(
            BytesIO(encode_vector_batch(family.family_id, dimension, rows)),
            VECTOR_BATCH_MEDIA_TYPE,
        )
        return ref, {row.record_id: index for index, row in enumerate(rows)}

    @staticmethod
    def _native_receipt(
        index: VectorIndex,
        spec: FamilySpec,
        records: list[EmbeddingRecord],
        ctx: ExecutionContext,
        owners: tuple[VectorOwner, ...] = (),
    ) -> BatchReceipt:
        """Upsert family-keyed records in ID order; an empty batch proves the family."""
        if records:
            ordered = tuple(sorted(records, key=lambda item: item.record_id))
            return index.upsert_batch(
                VectorBatch(family_id=spec.family_id, records=ordered, owners=owners),
                ctx,
            )
        handle = index.create_family(spec)
        return BatchReceipt(
            family_id=spec.family_id,
            namespace_hash=handle.namespace_hash,
            config_sha=handle.config_sha,
            records=(),
            digest=receipt_digest(()),
            count=0,
        )

    def _image_family(
        self, workspace_id: str, config_sha: str, created_at: str
    ) -> tuple[IndexFamily, FamilySpec]:
        embedder = self.visual_embedder
        index = self.image_index
        settings = self.config.values.providers.image
        if embedder is None or index is None:
            raise TychoraError(
                "CAPABILITY_MISSING",
                "Configured image projection lacks a qualified provider or native index",
            )
        capabilities = embedder.capabilities
        if (
            capabilities.provider != settings.provider
            or capabilities.model != settings.model
            or capabilities.dimensions != settings.dimensions
            or capabilities.modality != "visual"
        ):
            _fail("Image provider differs from configured representation space")
        family_config = digest(
            "image-family-config-v1",
            config_sha,
            settings.model_dump(mode="json"),
            stable_capability_json(capabilities),
        )
        creation_uuid = self._family_nonce("image", family_config)
        return self._register_vector_family(
            _FamilyRequest(
                "image",
                workspace_id,
                config_sha,
                family_config,
                creation_uuid,
                created_at,
            ),
            capabilities,
            index,
        )

    def _visual_inputs(self, bundle: ExtractionBundle) -> tuple[VisualInput, ...]:
        inputs = []
        for asset in sorted(bundle.assets, key=lambda item: item.asset_id):
            links = self.store.db.execute(
                "SELECT unit_id FROM unit_assets WHERE asset_id=? AND role='source' ORDER BY unit_id",
                (asset.asset_id,),
            ).fetchall()
            if not links:
                _fail("Canonical image asset has no source ownership")
            artifact = self.store.db.execute(
                "SELECT size_bytes,media_type,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
                (asset.artifact_sha,),
            ).fetchone()
            if artifact is None or artifact[3] != "present":
                _fail("Canonical image artifact is unavailable")
            ref = ArtifactRef.from_row((asset.artifact_sha, *artifact[:3]))
            try:
                from PIL import Image, ImageOps
            except ImportError:
                raise TychoraError(
                    "CAPABILITY_MISSING",
                    "Image operations require the documents extra.",
                ) from None
            with (
                self.artifacts.open_verified(ref) as stream,
                Image.open(stream) as encoded,
            ):
                if encoded.width * encoded.height > 40_000_000:
                    _fail("Canonical image exceeds the qualified pixel ceiling")
                pixels = ImageOps.exif_transpose(encoded).convert("RGB")
                pixels.load()
            width, height = pixels.size
            resize = VisualResizeDerivation(
                source_width=width,
                source_height=height,
                width=width,
                height=height,
                mode="none",
            )
            inputs.append(
                VisualInput.from_registered(
                    asset=asset,
                    artifact_ref=ref,
                    revision_id=bundle.revision.revision_id,
                    context="",
                    dependency_ids=frozenset(
                        {asset.asset_id, *(row[0] for row in links)}
                    ),
                    pixel_sha256=sha256(pixels.tobytes()).hexdigest(),
                    width=width,
                    height=height,
                    resize=resize,
                )
            )
        return tuple(inputs)

    def _image_batch(
        self,
        bundle: ExtractionBundle,
        family: IndexFamily,
        spec: FamilySpec,
        inputs: tuple[VisualInput, ...],
        records: tuple[EmbeddingRecord, ...],
        ctx: ExecutionContext,
    ) -> NewBatch:
        assert self.visual_embedder is not None and self.image_index is not None
        expected = {
            digest(
                "visual-embedding-record-v1",
                digest(
                    "visual-embedding-input-v1",
                    "document",
                    item.input_sha,
                    self.visual_embedder.capabilities.space_id,
                    "exif-transpose-rgb-lossless-png-v1",
                ),
                self.visual_embedder.capabilities.space_id,
            ): item
            for item in inputs
        }
        if len(records) != len(expected) or {
            record.record_id for record in records
        } != set(expected):
            _fail("Visual provider returned incomplete or foreign asset vectors")
        stored = []
        assets = {}
        for record in records:
            item = expected[record.record_id]
            input_sha = digest(
                "visual-embedding-input-v1",
                "document",
                item.input_sha,
                self.visual_embedder.capabilities.space_id,
                "exif-transpose-rgb-lossless-png-v1",
            )
            if (
                record.input_sha != input_sha
                or record.space_id != self.visual_embedder.capabilities.space_id
                or len(record.values) != spec.dimensions
            ):
                _fail("Visual provider vector differs from exact requested asset")
            record_id = vector_record_id(
                family.family_id, item.asset.asset_id, record.input_sha
            )
            stored.append(record.model_copy(update={"record_id": record_id}))
            assets[record_id] = item.asset.asset_id
        vectors, positions = self._vector_artifact(family, spec.dimensions, stored)
        bindings = [
            VectorRecordBinding(
                record_id=row.record_id,
                asset_id=assets[row.record_id],
                input_sha=row.input_sha,
                value_sha=row.value_sha,
                row_index=positions[row.record_id],
            )
            for row in sorted(stored, key=lambda row: row.record_id)
        ]
        receipt = self._native_receipt(
            self.image_index, spec, stored, ctx, _asset_owners(bundle, assets)
        )
        manifest = _input_manifest(
            self.artifacts, tuple(row.record_id for row in bindings)
        )
        return self._vector_batch(
            "image", bundle, manifest, _VectorOutput(receipt, vectors, bindings)
        )

    def _graph_family(
        self, workspace_id: str, config_sha: str, created_at: str
    ) -> IndexFamily:
        """Return the graph family of this configuration and registry, creating it once."""
        graph_config = digest(
            "graph-family-config-v2",
            config_sha,
            self.store.registry.registry_digest,
            self.config.values.retrieval.graph.model_dump(mode="json"),
        )
        nonce = self._family_nonce("graph", graph_config)
        family = IndexFamily(
            family_id=derive_family_id(workspace_id, graph_config, nonce),
            channel="graph",
            backend="ladybug",
            config_sha=graph_config,
            config_json=FrozenJsonMap(
                {
                    "effective_config_sha": config_sha,
                    "registry_digest": self.store.registry.registry_digest,
                }
            ),
            backend_namespace="graph",
            created_at=created_at,
        )
        self._persist_family(family, nonce, graph_config)
        return family

    def _graph_members(
        self, members: tuple[SnapshotMember, ...]
    ) -> list[GraphExtraction]:
        """Bind every selected extraction to its assertion root and evidence units."""
        selected = []
        for member in members:
            row = self.store.db.execute(
                "SELECT assertions_artifact_sha FROM extractions WHERE extraction_id=? AND revision_id=?",
                (member.extraction_id, member.revision_id),
            ).fetchone()
            if row is None:
                _fail("Graph member is not a registered extraction")
            root = None
            if row[0] is not None:
                artifact = self.store.db.execute(
                    "SELECT artifact_sha,size_bytes,media_type,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
                    (row[0],),
                ).fetchone()
                if artifact is None or artifact[4] != "present":
                    _fail("Graph assertion root is unavailable", "payload.missing")
                root = ArtifactRef.from_row(artifact)
            unit_ids = tuple(
                row[0]
                for row in self.store.db.execute(
                    "SELECT unit_id FROM evidence_units WHERE extraction_id=? ORDER BY unit_id",
                    (member.extraction_id,),
                )
            )
            selected.append(
                GraphExtraction(
                    extraction_id=member.extraction_id,
                    revision_id=member.revision_id,
                    assertion_root=root,
                    unit_ids=unit_ids,
                )
            )
        return selected

    def _graph_batch(
        self,
        members: tuple[SnapshotMember, ...],
        workspace_id: str,
        config_sha: str,
        created_at: str,
        ctx: ExecutionContext,
    ) -> tuple[IndexFamily, NewBatch]:
        """Seal the corpus-wide graph generation and store it as one root batch.

        The batch input carries the generation manifest so read paths and
        publication load it without the snapshot root.
        """
        if self.graph_factory is None:
            raise TychoraError(
                "CAPABILITY_MISSING",
                "Configured graph projection lacks a native generation factory",
                reason="native.unavailable",
            )
        family = self._graph_family(workspace_id, config_sha, created_at)
        selected = self._graph_members(members)
        with self.store.uow.read() as tx:
            watermark = tx.workspace.decision_watermark()
        spec = GraphGenerationSpec(
            generation_id=digest(
                "graph-generation-v2",
                family.family_id,
                family.config_sha,
                [item.model_dump(mode="json") for item in selected],
                watermark,
            ),
            family_id=family.family_id,
            config_sha=family.config_sha,
            registry_digest=self.store.registry.registry_digest,
            workspace_id=workspace_id,
            decision_watermark=watermark,
            memberships=tuple(selected),
            projection_version="stable-v2",
        )
        build = read_graph_build(self.store, spec, ctx)
        manifest = GraphManifest.from_build(build)
        graph = self.graph_factory(build)
        graph.begin_generation(spec)
        if graph.sealed_receipt(spec.generation_id) is None:
            graph.upsert_batch(
                spec.generation_id,
                tuple(item.assertion for item in build.assertions),
                tuple(item.unit for item in build.units),
                ctx,
            )
        receipt = graph.seal_generation(spec.generation_id, manifest, ctx)
        self.store.graph_indexes[spec.generation_id] = graph
        return family, self._graph_root_batch(manifest, receipt)

    def _graph_root_batch(
        self, manifest: GraphManifest, receipt: GraphBatchReceipt
    ) -> NewBatch:
        """Store the generation's input manifest and its ready batch once."""
        input_ref = self.artifacts.put_stream(
            BytesIO(
                canonical_bytes(
                    {
                        "type": "index_input",
                        "schema_version": "1",
                        "data": {
                            "entries": sorted(manifest.evidence_ids),
                            "graph_manifest": manifest.model_dump(mode="json"),
                        },
                    }
                )
            ),
            "application/json",
        )
        batch = NewBatch(
            slot="graph",
            batch_id=_batch_id(receipt.family_id, None, input_ref),
            family_id=receipt.family_id,
            extraction_id=None,
            input_manifest=input_ref,
            expected_count=receipt.evidence_count,
            receipt=receipt,
        )
        with self.store.uow.read() as tx:
            stored = tx.batches.get(batch.batch_id)
        _require_not_pruned(stored)
        if stored is None or stored.state != "ready":
            self._store_batch(batch, lambda tx: None)
        return batch

    def _require_active_source(self, source_id: str) -> None:
        row = self.store.db.execute(
            "SELECT c.policy_json FROM sources s JOIN collections c USING(collection_id) WHERE s.source_id=? AND s.withdrawn_at IS NULL",
            (source_id,),
        ).fetchone()
        if row is None:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Source is unavailable for publication."
            )
        require_active_collection(row[0])

    def prepare(
        self,
        bundle: ExtractionBundle,
        base_snapshot_id: str | None,
        job: JobHandle,
        ctx: ExecutionContext,
        *,
        on_stage: Callable[[str], None] | None = None,
    ) -> PreparedProjection:
        """Commit the canonical extraction and resolve the families an add needs.

        ``on_stage`` marks ``windowing``, ``normalize_commit`` and, once the
        families are resolved, ``embedding``, so each stage's duration ends
        where the next begins.
        """
        mark = on_stage or _ignore_stage
        ctx.checkpoint()
        self._require_collection_open(bundle.collection.collection_id)
        config_ref = self._config_ref(job)
        mark("windowing")
        extraction = prepare_extraction(bundle)
        windows = self._windows(extraction, bundle.revision.kind_id)
        canonical = self._canonical_bundle(bundle, windows)
        mark("normalize_commit")
        facts = representation_facts(
            canonical,
            extraction,
            (
                ChunkSpan(row.chunk_id, row.unit_id, row.char_start, row.char_end)
                for row in canonical.chunks
            ),
        )
        self._commit_or_reuse(canonical, facts)
        base = (
            self.store.resolve_snapshot(base_snapshot_id) if base_snapshot_id else None
        )
        base_page = None
        if base_snapshot_id is not None:
            with self.store.uow.read() as tx:
                base_page = tx.snapshots.page(
                    base_snapshot_id, bucket_of(canonical.source.source_id)
                )
        old = {item.slot: item for item in base.families} if base else {}
        text = self._prepare_text(old.get("text"), canonical, windows, config_ref)
        image = self._prepare_image(old.get("image"), canonical, config_ref)
        mark("embedding")
        return PreparedProjection(
            bundle=canonical,
            base_snapshot_id=base_snapshot_id,
            base=base,
            base_page=base_page,
            job=job,
            config_ref=config_ref,
            windows=windows,
            old_families=old,
            lexical_family=self._select_lexical_family(
                old.get("lexical"), canonical, config_ref
            ),
            text_family=text[0],
            text_spec=text[1],
            text_groups=text[2],
            image_family=image[0],
            image_spec=image[1],
            visual_inputs=image[2],
            reused_batch_ids=text[3] + image[3],
        )

    def _require_collection_open(self, collection_id: str) -> None:
        row = self.store.db.execute(
            "SELECT policy_json FROM collections WHERE collection_id=?",
            (collection_id,),
        ).fetchone()
        if row is not None:
            require_active_collection(row[0])

    def _select_lexical_family(
        self,
        old: SlotFamily | None,
        bundle: ExtractionBundle,
        config_ref: ArtifactRef,
    ) -> IndexFamily:
        """Keep the base lexical family while its configuration stays compatible."""
        origin = old.family.config_json.get("effective_config_sha") if old else None
        if (
            old is not None
            and isinstance(origin, str)
            and (
                origin == config_ref.sha256
                or pinned_runtime_compatible(
                    self.store.retained_config_sha(origin), self.config
                )
            )
        ):
            return old.family
        return self._lexical_family(
            bundle.workspace.workspace_id,
            config_ref.sha256,
            bundle.workspace.created_at,
        )

    def _prepare_vector(self, old, channel, canonical, make_family, inputs):
        """Reuse or create the family; ``inputs`` runs only when no batch is ready."""
        spec = self._retained_vector_spec(old, channel)
        if spec is not None and old is not None:
            family = old.family
        else:
            family, spec = make_family()
        ready = self._ready_batch(family, canonical)
        if ready is not None:
            return family, spec, (), (ready.batch_id,)
        return family, spec, inputs(), ()

    def _prepare_text(
        self,
        old: SlotFamily | None,
        canonical: ExtractionBundle,
        windows: tuple[SearchWindow, ...],
        config_ref: ArtifactRef,
    ) -> tuple[
        IndexFamily | None, FamilySpec | None, tuple[ContextGroup, ...], tuple[str, ...]
    ]:
        """Resolve the text family; groups are built only when no batch is ready."""
        if self.config.values.index.text.backend == "disabled":
            return None, None, (), ()
        return self._prepare_vector(
            old,
            "text",
            canonical,
            lambda: self._text_family(
                canonical.workspace.workspace_id,
                config_ref.sha256,
                canonical.workspace.created_at,
            ),
            lambda: build_groups(windows, self.config, self.counter),
        )

    def _prepare_image(
        self,
        old: SlotFamily | None,
        canonical: ExtractionBundle,
        config_ref: ArtifactRef,
    ) -> tuple[
        IndexFamily | None, FamilySpec | None, tuple[VisualInput, ...], tuple[str, ...]
    ]:
        """Resolve the image family; inputs are read only when no batch is ready."""
        if self.config.values.index.image.backend == "disabled":
            return None, None, (), ()
        return self._prepare_vector(
            old,
            "image",
            canonical,
            lambda: self._image_family(
                canonical.workspace.workspace_id,
                config_ref.sha256,
                canonical.workspace.created_at,
            ),
            lambda: self._visual_inputs(canonical),
        )

    def _vector_page_batch(
        self,
        prepared: PreparedProjection,
        item: _VectorSlot,
        written: list[NewBatch],
        ctx: ExecutionContext,
    ) -> PageBatch:
        """Reuse the extraction's ready vector batch or embed and write it once."""
        existing = self._ready_batch(item.family, prepared.bundle)
        if existing is not None:
            if item.records:
                _fail(
                    "Unrequested provider records cannot replace an immutable source batch"
                )
            return existing.page_batch()
        batch = (
            self._text_batch(
                prepared.bundle,
                prepared.windows,
                item.family,
                item.spec,
                prepared.text_groups,
                item.records,
                ctx,
            )
            if item.slot == "text"
            else self._image_batch(
                prepared.bundle,
                item.family,
                item.spec,
                prepared.visual_inputs,
                item.records,
                ctx,
            )
        )
        written.append(batch)
        return batch.page_batch()

    def _source_batches(
        self,
        prepared: PreparedProjection,
        records: dict[str, tuple[EmbeddingRecord, ...]],
        written: list[NewBatch],
        ctx: ExecutionContext,
    ) -> tuple[list[SlotFamily], list[PageBatch]]:
        """Return the add's source-local families and the page batches it binds."""
        families = [SlotFamily(slot="lexical", family=prepared.lexical_family)]
        batches = [self._lexical_page_batch(prepared, written)]
        for slot, family, spec in (
            ("text", prepared.text_family, prepared.text_spec),
            ("image", prepared.image_family, prepared.image_spec),
        ):
            if family is None or spec is None:
                if records[slot]:
                    _fail(
                        f"{slot.capitalize()} records were supplied to a disabled slot"
                    )
                continue
            families.append(SlotFamily(slot=slot, family=family))
            batches.append(
                self._vector_page_batch(
                    prepared,
                    _VectorSlot(slot, family, spec, records[slot]),
                    written,
                    ctx,
                )
            )
        return families, batches

    def finalize(
        self,
        prepared: PreparedProjection,
        text_records: tuple[EmbeddingRecord, ...],
        ctx: ExecutionContext,
        visual_records: tuple[EmbeddingRecord, ...] = (),
    ) -> PublicationPlan:
        """Write the add's new batches and plan its one rewritten page."""
        ctx.checkpoint()
        if self._config_ref(prepared.job) != prepared.config_ref:
            _fail("Projection configuration changed before finalization")
        self._require_active_source(prepared.bundle.source.source_id)
        written: list[NewBatch] = []
        families, batches = self._source_batches(
            prepared, {"text": text_records, "image": visual_records}, written, ctx
        )
        page = self._next_page(prepared, tuple(batches))
        for item in families:
            self._require_rebase_free(prepared, item, page)
        inputs = _PlanInputs(
            job=prepared.job,
            workspace_id=prepared.bundle.workspace.workspace_id,
            base=prepared.base,
            base_page=prepared.base_page,
            config_ref=prepared.config_ref,
            families=tuple(families),
            page=page,
            written=tuple(written),
        )
        return self._plan(
            self._with_graph(inputs, prepared.bundle.workspace.created_at, ctx)
        )

    def _with_graph(
        self, inputs: _PlanInputs, created_at: str, ctx: ExecutionContext
    ) -> _PlanInputs:
        """Add the corpus-wide graph generation as the plan's root batch."""
        if self.config.values.index.graph == "disabled":
            return inputs
        family, batch = self._graph_batch(
            self._snapshot_members(inputs.base, inputs.page),
            inputs.workspace_id,
            inputs.config_ref.sha256,
            created_at,
            ctx,
        )
        receipt = batch.receipt
        if not isinstance(receipt, GraphBatchReceipt):
            _fail("Graph batch receipt is malformed", "canonical.row_invalid")
        return replace(
            inputs,
            families=(
                *(item for item in inputs.families if item.slot != "graph"),
                SlotFamily(slot="graph", family=family),
            ),
            written=(*inputs.written, batch),
            root_batches=(batch.page_batch(),),
            watermark=receipt.decision_watermark,
        )

    def _kept_base_page(
        self, base_page: MembershipPage | None, slots: frozenset[str]
    ) -> tuple[MembershipPage | None, dict[str, str | None]]:
        """Keep the base page's active sources with the batches of ``slots`` only.

        Withdrawn sources leave the page, and a slot the configuration no longer
        serves leaves every kept member. Return the page and member editions.
        """
        if base_page is None:
            return None, {}
        rows = self.store.db.execute(
            "SELECT r.revision_id,r.edition_key,s.withdrawn_at IS NULL FROM revisions r "
            "JOIN sources s USING(source_id) "
            "WHERE r.revision_id IN (SELECT value FROM json_each(?))",
            (json.dumps([m.revision_id for m in base_page.members]),),
        ).fetchall()
        active = {row[0] for row in rows if row[2]}
        members = tuple(
            m.model_copy(
                update={"batches": tuple(b for b in m.batches if b.slot in slots)}
            )
            for m in base_page.members
            if m.revision_id in active
        )
        page = MembershipPage(
            bucket=base_page.bucket,
            members=members,
            page_id=compute_page_id(base_page.bucket, members),
        )
        return page, {row[0]: row[1] for row in rows}

    def _next_page(
        self, prepared: PreparedProjection, batches: tuple[PageBatch, ...]
    ) -> MembershipPage:
        """Rewrite the add's bucket: keep its other sources, supersede or keep editions."""
        bundle = prepared.bundle
        member = PageMember(
            source_id=bundle.source.source_id,
            revision_id=bundle.revision.revision_id,
            extraction_id=bundle.extraction.extraction_id,
            batches=tuple(sorted(batches, key=lambda batch: batch.slot)),
        )
        kept, editions = self._kept_base_page(
            prepared.base_page, frozenset(batch.slot for batch in batches)
        )
        return next_page(kept, member, editions, bundle.revision.edition_key)

    def _require_rebase_free(
        self, prepared: PreparedProjection, item: SlotFamily, page: MembershipPage
    ) -> None:
        """Earlier members must already have a batch in the family this add publishes."""
        old = prepared.old_families.get(item.slot)
        if old is not None and old.family.family_id == item.family.family_id:
            return
        survivors = (
            member_count_after(
                prepared.base.member_count if prepared.base else 0,
                prepared.base_page,
                page,
            )
            - 1
        )
        if survivors > 0:
            raise TychoraError(
                "INDEX_INCOMPLETE",
                f"Earlier {item.slot} source batches are unavailable for rebase",
                reason="batch.missing",
            )

    def _snapshot_members(
        self, base: SnapshotView | None, page: MembershipPage
    ) -> tuple[SnapshotMember, ...]:
        """The members of the planned snapshot: the base outside the page's bucket."""
        kept: list[SnapshotMember] = []
        if base is not None:
            cursor = None
            with self.store.uow.read() as tx:
                while True:
                    rows, cursor = tx.snapshots.members(base.snapshot_id, after=cursor)
                    kept.extend(
                        m for m in rows if bucket_of(m.source_id) != page.bucket
                    )
                    if cursor is None:
                        break
        added = [
            SnapshotMember(
                source_id=m.source_id,
                revision_id=m.revision_id,
                extraction_id=m.extraction_id,
                is_default=m.is_default,
            )
            for m in page.members
        ]
        return tuple(
            sorted(
                kept + added,
                key=lambda m: (m.source_id, m.revision_id, m.extraction_id),
            )
        )

    def _plan(self, inputs: _PlanInputs) -> PublicationPlan:
        """Store the rewritten page and the snapshot root, then return the plan."""
        base = inputs.base
        replaced = inputs.base_page.page_id if inputs.base_page else None
        reused = tuple(p for p in (base.page_ids if base else ()) if p != replaced)
        manifests = {}
        if reused:
            with self.store.uow.read() as tx:
                manifests = dict(tx.snapshots.page_manifests(reused))
        manifests[inputs.page.page_id] = self.artifacts.put_stream(
            BytesIO(inputs.page.manifest_bytes()), "application/json"
        )
        binding = SnapshotBinding(
            workspace_id=inputs.workspace_id,
            base_snapshot_id=base.snapshot_id if base else None,
            job_id=inputs.job.id,
            config_sha=inputs.config_ref.sha256,
            families=inputs.families,
            root_batch_ids=tuple(batch.batch_id for batch in inputs.root_batches),
        )
        try:
            draft = PublicationPlan(
                job_id=inputs.job.id,
                snapshot_id=snapshot_id_v2(binding, (*reused, inputs.page.page_id)),
                workspace_id=inputs.workspace_id,
                base_snapshot_id=binding.base_snapshot_id,
                config_sha=inputs.config_ref.sha256,
                families=inputs.families,
                member_count=member_count_after(
                    base.member_count if base else 0, inputs.base_page, inputs.page
                ),
                new_pages=(inputs.page,),
                reused_page_ids=reused,
                new_batches=inputs.written,
                root_batches=inputs.root_batches,
                graph_watermark=inputs.watermark,
                # The validator does not read the root; the stored root replaces it.
                manifest=inputs.config_ref,
            )
        except ValueError:
            _fail(
                "Planned pages and snapshot bindings are inconsistent",
                "invariant.unexpected",
            )
        root = self.artifacts.put_stream(
            BytesIO(canonical_bytes(snapshot_root_document(draft, manifests))),
            "application/json",
        )
        return draft.model_copy(update={"manifest": root})

    def reuse_existing(
        self,
        source_id: str,
        revision_id: str,
        base_snapshot_id: str,
        job: JobHandle,
        ctx: ExecutionContext,
    ) -> PublicationPlan:
        """A fresh capture of an active immutable revision republishes its retained batches."""
        ctx.checkpoint()
        self._require_active_source(source_id)
        base = self.store.resolve_snapshot(base_snapshot_id)
        with self.store.uow.read() as tx:
            base_page = tx.snapshots.page(base_snapshot_id, bucket_of(source_id))
        if base_page is None or not any(
            m.source_id == source_id and m.revision_id == revision_id
            for m in base_page.members
        ):
            _fail("Captured revision is not the selected canonical source")
        kept, _ = self._kept_base_page(
            base_page,
            frozenset(item.slot for item in base.families if item.slot != "graph"),
        )
        members = tuple(
            m.model_copy(update={"is_default": m.revision_id == revision_id})
            if m.source_id == source_id
            else m
            for m in (kept.members if kept else ())
        )
        page = MembershipPage(
            bucket=base_page.bucket,
            members=members,
            page_id=compute_page_id(base_page.bucket, members),
        )
        config_ref = self._config_ref(job)
        if base.config_sha != config_ref.sha256 and not pinned_runtime_compatible(
            self.store.retained_config(base), self.config
        ):
            raise TychoraError(
                "INDEX_INCOMPLETE",
                "Existing projection configuration differs",
                reason="batch.missing",
            )
        inputs = _PlanInputs(
            job=job,
            workspace_id=base.workspace_id,
            base=base,
            base_page=base_page,
            config_ref=config_ref,
            families=tuple(item for item in base.families if item.slot != "graph"),
            page=page,
            written=(),
        )
        created_at = self.store.db.execute(
            "SELECT created_at FROM workspaces"
        ).fetchone()[0]
        return self._plan(self._with_graph(inputs, created_at, ctx))

    def attach(self, plan: PublicationPlan, ctx: ExecutionContext) -> None:
        """Reopen configured native ports for a retained plan before verification."""
        check_publication_target(self.store, plan)
        for item in plan.families:
            ctx.checkpoint()
            if item.slot in {"text", "image"}:
                self._attach_vector(item)
        for batch in plan.root_batches:
            self._attach_graph(batch, ctx)

    def _attach_vector(self, item: SlotFamily) -> None:
        """Open the configured native index of a retained vector family."""
        if item.family.family_id in self.store.vector_indexes:
            return
        index = self.text_index if item.slot == "text" else self.image_index
        if index is None:
            raise TychoraError(
                "CAPABILITY_MISSING",
                "Required native vector index is unavailable",
                reason="native.unavailable",
            )
        try:
            spec = FamilySpec.model_validate_json(
                canonical_bytes(item.family.config_json.get("spec"))
            )
        except (TypeError, ValueError):
            _fail("Retained vector family specification is malformed")
        if spec.family_id != item.family.family_id:
            _fail("Retained vector family identity differs")
        if index.create_family(spec).config_sha != item.family.config_sha:
            _fail("Retained native vector family configuration differs")
        self.store.vector_indexes[item.family.family_id] = index

    def _attach_graph(self, batch: PageBatch, ctx: ExecutionContext) -> None:
        """Rebuild the retained graph generation's native store from canonical rows."""
        if self.graph_factory is None:
            raise TychoraError(
                "CAPABILITY_MISSING",
                "Required graph generation factory is unavailable",
                reason="native.unavailable",
            )
        with self.artifacts.open_verified(batch.input_manifest) as stream:
            data = json.load(stream).get("data", {})
        try:
            manifest = GraphManifest.model_validate_json(
                canonical_bytes(data["graph_manifest"])
            )
        except (KeyError, TypeError, ValueError):
            _fail("Retained graph manifest is unavailable", "canonical.row_invalid")
        if manifest.spec.generation_id in self.store.graph_indexes:
            return
        build = read_graph_build(self.store, manifest.spec, ctx)
        if GraphManifest.from_build(build) != manifest:
            _fail("Retained graph canonical build differs")
        self.store.graph_indexes[manifest.spec.generation_id] = self.graph_factory(
            build
        )

    def build(
        self,
        bundle: ExtractionBundle,
        base_snapshot_id: str | None,
        job: JobHandle,
        ctx: ExecutionContext,
    ) -> PublicationPlan:
        """Compose an explicitly local injected provider without an owner upload path."""
        prepared = self.prepare(bundle, base_snapshot_id, job, ctx)
        if prepared.text_groups:
            if (
                self.local_text_embedder is None
                or self.local_text_embedder is not self.text_embedder
            ):
                raise TychoraError(
                    "CAPABILITY_MISSING",
                    "Remote text projection requires the owner-authorized provider phase",
                )
            records = self.local_text_embedder.embed_groups(prepared.text_groups, ctx)
        else:
            records = ()
        if prepared.visual_inputs:
            raise TychoraError(
                "CAPABILITY_MISSING",
                "Visual projection requires the owner-authorized provider phase",
            )
        return self.finalize(prepared, records, ctx)
