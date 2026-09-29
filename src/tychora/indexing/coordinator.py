"""Build complete source-local projections before canonical publication."""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import NoReturn
from uuid import UUID, uuid5

from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.adapters.sqlite.graph import read_graph_build
from tychora.adapters.sqlite.publication import publication_manifest
from tychora.configuration.models import ResolvedConfig
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import ChunkRow, ExtractionBundle
from tychora.contracts.embeddings import (
    EmbeddingRecord,
    VisualInput,
    VisualResizeDerivation,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import (
    GraphBatchReceipt,
    GraphBuildInput,
    GraphExtraction,
    GraphGenerationSpec,
    GraphManifest,
)
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.identity import family_id as derive_family_id
from tychora.contracts.indexing import ContextGroup, FamilySpec, SearchWindow
from tychora.contracts.operations import JobHandle
from tychora.contracts.publication import (
    IndexFamily,
    LexicalBatchReceipt,
    LexicalRecord,
    PublicationBatch,
    PublicationPlan,
    PublicationSlot,
    SnapshotMember,
    VectorRecordBinding,
)
from tychora.contracts.vectors import BatchReceipt, VectorBatch
from tychora.indexing.groups import build_groups
from tychora.indexing.receipts import receipt_digest
from tychora.indexing.representations import prepare_extraction
from tychora.indexing.windows import window_unit
from tychora.ports.artifacts import Artifacts
from tychora.ports.embeddings import OwnerBoundVisualEmbedder, TextEmbedder
from tychora.ports.execution import ExecutionContext
from tychora.ports.graph import GraphStore
from tychora.ports.tokens import TokenCounter
from tychora.ports.vectors import VectorIndex


def _fail(message: str) -> NoReturn:
    raise TychoraError("INTEGRITY_ERROR", message)


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


def _batch_id(family_id: str, extraction_id: str | None, ref: ArtifactRef) -> str:
    return digest("index-batch-v1", family_id, extraction_id, ref.sha256)


@dataclass(frozen=True)
class PreparedProjection:
    bundle: ExtractionBundle
    base_snapshot_id: str | None
    job: JobHandle
    config_ref: ArtifactRef
    windows: tuple[SearchWindow, ...]
    members: tuple[SnapshotMember, ...]
    old_slots: dict[str, PublicationSlot]
    lexical_family: IndexFamily
    text_family: IndexFamily | None
    text_spec: FamilySpec | None
    text_groups: tuple[ContextGroup, ...]
    image_family: IndexFamily | None
    image_spec: FamilySpec | None
    visual_inputs: tuple[VisualInput, ...]


class ProjectionCoordinator:
    """Use real canonical, provider and native ports; return a verified plan."""

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
        return ArtifactRef(
            sha256=row[0], size_bytes=row[1], media_type=row[2], storage_key=row[3]
        )

    def _windows(self, bundle: ExtractionBundle) -> tuple[SearchWindow, ...]:
        prepared = prepare_extraction(bundle)
        return tuple(
            window
            for item in prepared.units
            for window in window_unit(item, self.counter)
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

    def _commit_or_reuse(self, bundle: ExtractionBundle) -> None:
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
        self.store.commit_extraction(bundle)

    @staticmethod
    def _lexical_family(
        workspace_id: str, config_sha: str, created_at: str
    ) -> IndexFamily:
        return IndexFamily(
            family_id=digest("lexical-family-v1", workspace_id, config_sha),
            channel="lexical",
            backend="sqlite-fts5",
            config_sha=config_sha,
            config_json=FrozenJsonMap(
                {"tokenizer": "unicode61", "effective_config_sha": config_sha}
            ),
            backend_namespace="lexical",
            created_at=created_at,
        )

    def _lexical_batch(
        self,
        bundle: ExtractionBundle,
        windows: tuple[SearchWindow, ...],
        family: IndexFamily,
    ) -> PublicationBatch:
        records = tuple(
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
        entries = tuple(row.chunk_id for row in records)
        manifest = _input_manifest(self.artifacts, entries)
        return PublicationBatch(
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
            lexical_records=records,
        )

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
            capabilities.model_dump(mode="json"),
        )
        creation_uuid = str(uuid5(UUID(workspace_id), "text:" + family_config))
        spec = FamilySpec(
            family_id=derive_family_id(workspace_id, family_config, creation_uuid),
            workspace_id=workspace_id,
            config_sha=family_config,
            creation_uuid=creation_uuid,
            modality="text",
            provider=capabilities.provider,
            model=capabilities.model,
            dimensions=capabilities.dimensions,
            metric="cosine",
            normalization="unit-l2",
            representation_fingerprint=capabilities.representation_fingerprint,
            backend_namespace="text",
            backend_space="ip",
        )
        handle = index.create_family(spec)
        family = IndexFamily(
            family_id=spec.family_id,
            channel="text",
            backend="chroma",
            provider=spec.provider,
            model=spec.model,
            dimension=spec.dimensions,
            config_sha=handle.config_sha,
            config_json=FrozenJsonMap(
                {
                    "effective_config_sha": config_sha,
                    "spec": spec.model_dump(mode="json"),
                    "namespace_hash": handle.namespace_hash,
                }
            ),
            backend_namespace=spec.backend_namespace,
            created_at=created_at,
        )
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
    ) -> PublicationBatch:
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
        bindings = []
        for item in records:
            window_id, group = expected[item.record_id]
            if (
                item.input_sha != group.input_sha
                or item.space_id != self.text_embedder.capabilities.space_id
                or len(item.values) != spec.dimensions
            ):
                _fail("Text provider vector differs from exact requested input")
            raw = struct.pack("<" + "f" * len(item.values), *item.values)
            cache = self.artifacts.put_stream(
                BytesIO(raw), "application/vnd.tychora.float32-le"
            )
            if cache.sha256 != item.value_sha:
                _fail("Text provider value digest differs from retained bytes")
            bindings.append(
                VectorRecordBinding(
                    record_id=item.record_id,
                    chunk_id=window_id,
                    input_sha=item.input_sha,
                    value_sha=item.value_sha,
                    group_id=group.group_id,
                    dependency_ids=group.dependency_ids,
                    embedding_cache=cache,
                )
            )
        ordered = tuple(sorted(records, key=lambda item: item.record_id))
        if ordered:
            receipt = self.text_index.upsert_batch(
                VectorBatch(family_id=family.family_id, records=ordered), ctx
            )
        else:
            handle = self.text_index.create_family(spec)
            receipt = BatchReceipt(
                family_id=family.family_id,
                namespace_hash=handle.namespace_hash,
                config_sha=handle.config_sha,
                records=(),
                digest=receipt_digest(()),
                count=0,
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
        return PublicationBatch(
            batch_id=_batch_id(
                family.family_id, bundle.extraction.extraction_id, manifest
            ),
            family_id=family.family_id,
            extraction_id=bundle.extraction.extraction_id,
            input_manifest=manifest,
            expected_count=len(bindings),
            receipt=receipt,
            vector_records=tuple(bindings),
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
            capabilities.model_dump(mode="json"),
        )
        creation_uuid = str(uuid5(UUID(workspace_id), "image:" + family_config))
        spec = FamilySpec(
            family_id=derive_family_id(workspace_id, family_config, creation_uuid),
            workspace_id=workspace_id,
            config_sha=family_config,
            creation_uuid=creation_uuid,
            modality="visual",
            provider=capabilities.provider,
            model=capabilities.model,
            dimensions=capabilities.dimensions,
            metric="cosine",
            normalization="unit-l2",
            representation_fingerprint=capabilities.representation_fingerprint,
            backend_namespace="image",
            backend_space="ip",
        )
        handle = index.create_family(spec)
        family = IndexFamily(
            family_id=spec.family_id,
            channel="image",
            backend="chroma",
            provider=spec.provider,
            model=spec.model,
            dimension=spec.dimensions,
            config_sha=handle.config_sha,
            config_json=FrozenJsonMap(
                {
                    "effective_config_sha": config_sha,
                    "spec": spec.model_dump(mode="json"),
                    "namespace_hash": handle.namespace_hash,
                }
            ),
            backend_namespace=spec.backend_namespace,
            created_at=created_at,
        )
        self.store.vector_indexes[spec.family_id] = index
        return family, spec

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
            ref = ArtifactRef(
                sha256=asset.artifact_sha,
                size_bytes=artifact[0],
                media_type=artifact[1],
                storage_key=artifact[2],
            )
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
    ) -> PublicationBatch:
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
        bindings = []
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
            raw = struct.pack("<" + "f" * len(record.values), *record.values)
            cache = self.artifacts.put_stream(
                BytesIO(raw), "application/vnd.tychora.float32-le"
            )
            if cache.sha256 != record.value_sha:
                _fail("Visual provider value digest differs from retained bytes")
            bindings.append(
                VectorRecordBinding(
                    record_id=record.record_id,
                    asset_id=item.asset.asset_id,
                    input_sha=record.input_sha,
                    value_sha=record.value_sha,
                    embedding_cache=cache,
                )
            )
        ordered = tuple(sorted(records, key=lambda record: record.record_id))
        if ordered:
            receipt = self.image_index.upsert_batch(
                VectorBatch(family_id=family.family_id, records=ordered), ctx
            )
        else:
            handle = self.image_index.create_family(spec)
            receipt = BatchReceipt(
                family_id=family.family_id,
                namespace_hash=handle.namespace_hash,
                config_sha=handle.config_sha,
                records=(),
                digest=receipt_digest(()),
                count=0,
            )
        manifest = _input_manifest(
            self.artifacts, tuple(row.record_id for row in bindings)
        )
        return PublicationBatch(
            batch_id=_batch_id(
                family.family_id, bundle.extraction.extraction_id, manifest
            ),
            family_id=family.family_id,
            extraction_id=bundle.extraction.extraction_id,
            input_manifest=manifest,
            expected_count=len(bindings),
            receipt=receipt,
            vector_records=tuple(bindings),
        )

    def _graph_slot(
        self,
        members: tuple[SnapshotMember, ...],
        workspace_id: str,
        config_sha: str,
        created_at: str,
        ctx: ExecutionContext,
    ) -> PublicationSlot:
        if self.graph_factory is None:
            raise TychoraError(
                "CAPABILITY_MISSING",
                "Configured graph projection lacks a native generation factory",
            )
        graph_config = digest(
            "graph-family-config-v1",
            config_sha,
            self.store.registry.registry_digest,
            self.config.values.retrieval.graph.model_dump(mode="json"),
        )
        family_id = digest("graph-family-v1", workspace_id, graph_config)
        family = IndexFamily(
            family_id=family_id,
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
                    _fail("Graph assertion root is unavailable")
                root = ArtifactRef(
                    sha256=artifact[0],
                    size_bytes=artifact[1],
                    media_type=artifact[2],
                    storage_key=artifact[3],
                )
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
        watermark = self.store.db.execute(
            "SELECT COALESCE(MAX(decision_id),0) FROM assertion_decisions"
        ).fetchone()[0]
        spec = GraphGenerationSpec(
            generation_id=digest(
                "graph-generation-v1",
                family_id,
                graph_config,
                [item.model_dump(mode="json") for item in selected],
                watermark,
            ),
            family_id=family_id,
            config_sha=graph_config,
            registry_digest=self.store.registry.registry_digest,
            workspace_id=workspace_id,
            decision_watermark=watermark,
            memberships=tuple(selected),
        )
        build = read_graph_build(self.store, spec, ctx)
        manifest = GraphManifest.from_build(build)
        graph = self.graph_factory(build)
        graph.begin_generation(spec)
        graph.upsert_batch(
            spec.generation_id,
            tuple(item.assertion for item in build.assertions),
            tuple(item.unit for item in build.units),
            ctx,
        )
        receipt = graph.seal_generation(spec.generation_id, manifest, ctx)
        self.store.graph_indexes[spec.generation_id] = graph
        input_ref = _input_manifest(
            self.artifacts, tuple(sorted(manifest.evidence_ids))
        )
        batch = PublicationBatch(
            batch_id=_batch_id(family_id, None, input_ref),
            family_id=family_id,
            input_manifest=input_ref,
            expected_count=receipt.evidence_count,
            receipt=receipt,
            graph_manifest=manifest,
        )
        return PublicationSlot(slot="graph", family=family, batches=(batch,))

    def prepare(
        self,
        bundle: ExtractionBundle,
        base_snapshot_id: str | None,
        job: JobHandle,
        ctx: ExecutionContext,
    ) -> PreparedProjection:
        ctx.checkpoint()
        config_ref = self._config_ref(job)
        windows = self._windows(bundle)
        canonical = self._canonical_bundle(bundle, windows)
        self._commit_or_reuse(canonical)
        current = (
            self.store.resolve_snapshot(base_snapshot_id) if base_snapshot_id else None
        )
        members = (
            {member.source_id: member for member in current.members} if current else {}
        )
        members[bundle.source.source_id] = SnapshotMember(
            source_id=bundle.source.source_id,
            revision_id=bundle.revision.revision_id,
            extraction_id=bundle.extraction.extraction_id,
        )
        selected = tuple(sorted(members.values(), key=lambda item: item.source_id))
        old_slots = {slot.slot: slot for slot in current.slots} if current else {}
        lexical_family = self._lexical_family(
            bundle.workspace.workspace_id,
            config_ref.sha256,
            bundle.workspace.created_at,
        )
        text_family = None
        text_spec = None
        text_groups: tuple[ContextGroup, ...] = ()
        if self.config.values.index.text.backend != "disabled":
            text_family, text_spec = self._text_family(
                bundle.workspace.workspace_id,
                config_ref.sha256,
                bundle.workspace.created_at,
            )
            old_text = old_slots.get("text")
            reusable = (
                old_text is not None
                and old_text.family == text_family
                and any(
                    batch.extraction_id == canonical.extraction.extraction_id
                    for batch in old_text.batches
                )
            )
            if not reusable:
                text_groups = build_groups(windows, self.config, self.counter)
        image_family = None
        image_spec = None
        visual_inputs: tuple[VisualInput, ...] = ()
        if self.config.values.index.image.backend != "disabled":
            image_family, image_spec = self._image_family(
                bundle.workspace.workspace_id,
                config_ref.sha256,
                bundle.workspace.created_at,
            )
            old_image = old_slots.get("image")
            reusable_image = (
                old_image is not None
                and old_image.family == image_family
                and any(
                    batch.extraction_id == canonical.extraction.extraction_id
                    for batch in old_image.batches
                )
            )
            if not reusable_image:
                visual_inputs = self._visual_inputs(canonical)
        return PreparedProjection(
            bundle=canonical,
            base_snapshot_id=base_snapshot_id,
            job=job,
            config_ref=config_ref,
            windows=windows,
            members=selected,
            old_slots=old_slots,
            lexical_family=lexical_family,
            text_family=text_family,
            text_spec=text_spec,
            text_groups=text_groups,
            image_family=image_family,
            image_spec=image_spec,
            visual_inputs=visual_inputs,
        )

    def finalize(
        self,
        prepared: PreparedProjection,
        text_records: tuple[EmbeddingRecord, ...],
        ctx: ExecutionContext,
        visual_records: tuple[EmbeddingRecord, ...] = (),
    ) -> PublicationPlan:
        ctx.checkpoint()
        if self._config_ref(prepared.job) != prepared.config_ref:
            _fail("Projection configuration changed before finalization")
        canonical = prepared.bundle
        selected = prepared.members
        windows = prepared.windows
        old_slots = prepared.old_slots
        lexical_family = prepared.lexical_family
        old_lexical = (
            {batch.extraction_id: batch for batch in old_slots["lexical"].batches}
            if "lexical" in old_slots and old_slots["lexical"].family == lexical_family
            else {}
        )
        if any(
            member.extraction_id != canonical.extraction.extraction_id
            and member.extraction_id not in old_lexical
            for member in selected
        ):
            raise TychoraError(
                "INDEX_INCOMPLETE",
                "Earlier lexical source batch is unavailable for rebase",
            )
        lexical_batches = tuple(
            old_lexical.get(member.extraction_id)
            or self._lexical_batch(canonical, windows, lexical_family)
            for member in selected
        )
        slots = [
            PublicationSlot(
                slot="lexical", family=lexical_family, batches=lexical_batches
            )
        ]
        if prepared.text_family is not None and prepared.text_spec is not None:
            family, spec = prepared.text_family, prepared.text_spec
            old_text = (
                {batch.extraction_id: batch for batch in old_slots["text"].batches}
                if "text" in old_slots and old_slots["text"].family == family
                else {}
            )
            if any(
                member.extraction_id != canonical.extraction.extraction_id
                and member.extraction_id not in old_text
                for member in selected
            ):
                raise TychoraError(
                    "INDEX_INCOMPLETE",
                    "Earlier text source batch is unavailable for rebase",
                )
            needs_current = canonical.extraction.extraction_id not in old_text
            if not needs_current and text_records:
                _fail(
                    "Unrequested provider records cannot replace an immutable source batch"
                )
            text_batches = tuple(
                old_text.get(member.extraction_id)
                or self._text_batch(
                    canonical,
                    windows,
                    family,
                    spec,
                    prepared.text_groups,
                    text_records,
                    ctx,
                )
                for member in selected
            )
            slots.append(
                PublicationSlot(slot="text", family=family, batches=text_batches)
            )
        elif text_records:
            _fail("Text records were supplied to a disabled slot")
        if prepared.image_family is not None and prepared.image_spec is not None:
            family, spec = prepared.image_family, prepared.image_spec
            old_image = (
                {batch.extraction_id: batch for batch in old_slots["image"].batches}
                if "image" in old_slots and old_slots["image"].family == family
                else {}
            )
            if any(
                member.extraction_id != canonical.extraction.extraction_id
                and member.extraction_id not in old_image
                for member in selected
            ):
                raise TychoraError(
                    "INDEX_INCOMPLETE",
                    "Earlier image source batch is unavailable for rebase",
                )
            if canonical.extraction.extraction_id in old_image and visual_records:
                _fail(
                    "Unrequested visual records cannot replace an immutable source batch"
                )
            image_batches = tuple(
                old_image.get(member.extraction_id)
                or self._image_batch(
                    canonical,
                    family,
                    spec,
                    prepared.visual_inputs,
                    visual_records,
                    ctx,
                )
                for member in selected
            )
            slots.append(
                PublicationSlot(slot="image", family=family, batches=image_batches)
            )
        elif visual_records:
            _fail("Visual records were supplied to a disabled slot")
        if self.config.values.index.graph != "disabled":
            slots.append(
                self._graph_slot(
                    selected,
                    canonical.workspace.workspace_id,
                    prepared.config_ref.sha256,
                    canonical.workspace.created_at,
                    ctx,
                )
            )
        return self._plan(
            prepared.job,
            canonical.workspace.workspace_id,
            prepared.base_snapshot_id,
            prepared.config_ref,
            selected,
            tuple(slots),
        )

    def _plan(
        self,
        job: JobHandle,
        workspace_id: str,
        base_snapshot_id: str | None,
        config_ref: ArtifactRef,
        selected: tuple[SnapshotMember, ...],
        slots_tuple: tuple[PublicationSlot, ...],
    ) -> PublicationPlan:
        snapshot_id = digest(
            "snapshot-v1",
            workspace_id,
            base_snapshot_id,
            job.id,
            config_ref.sha256,
            [member.model_dump(mode="json") for member in selected],
            [
                {
                    "slot": slot.slot,
                    "family_id": slot.family.family_id,
                    "required": slot.required,
                    "batch_ids": [batch.batch_id for batch in slot.batches],
                }
                for slot in slots_tuple
            ],
        )
        placeholder = _input_manifest(self.artifacts, ())
        plan = PublicationPlan(
            job_id=job.id,
            snapshot_id=snapshot_id,
            workspace_id=workspace_id,
            base_snapshot_id=base_snapshot_id,
            config_sha=config_ref.sha256,
            manifest=placeholder,
            members=selected,
            slots=slots_tuple,
        )
        manifest = self.artifacts.put_stream(
            BytesIO(publication_manifest(plan)), "application/json"
        )
        return plan.model_copy(update={"manifest": manifest})

    def reuse_existing(
        self,
        source_id: str,
        revision_id: str,
        base_snapshot_id: str,
        job: JobHandle,
        ctx: ExecutionContext,
    ) -> PublicationPlan:
        """Fresh capture of an active immutable revision reuses retained artifacts."""
        ctx.checkpoint()
        current = self.store.resolve_snapshot(base_snapshot_id)
        selected = tuple(current.members)
        if not any(
            m.source_id == source_id and m.revision_id == revision_id for m in selected
        ):
            _fail("Captured revision is not the selected canonical source")
        config_ref = self._config_ref(job)
        if current.config_sha != config_ref.sha256:
            raise TychoraError(
                "INDEX_INCOMPLETE", "Existing projection configuration differs"
            )
        slots = tuple(current.slots)
        if self.config.values.index.graph != "disabled":
            watermark = self.store.db.execute(
                "SELECT COALESCE(MAX(decision_id),0) FROM assertion_decisions"
            ).fetchone()[0]
            graph = next((slot for slot in slots if slot.slot == "graph"), None)
            if (
                graph is None
                or not isinstance(graph.batches[0].receipt, GraphBatchReceipt)
                or graph.batches[0].receipt.decision_watermark != watermark
            ):
                slots = tuple(slot for slot in slots if slot.slot != "graph") + (
                    self._graph_slot(
                        selected,
                        current.workspace_id,
                        config_ref.sha256,
                        self.store.db.execute(
                            "SELECT created_at FROM workspaces"
                        ).fetchone()[0],
                        ctx,
                    ),
                )
        return self._plan(
            job,
            current.workspace_id,
            base_snapshot_id,
            config_ref,
            selected,
            slots,
        )

    def attach(self, plan: PublicationPlan, ctx: ExecutionContext) -> None:
        """Reopen configured native ports for a retained plan before verification."""
        for slot in plan.slots:
            ctx.checkpoint()
            if slot.slot in {"text", "image"}:
                if slot.family.family_id in self.store.vector_indexes:
                    continue
                index = self.text_index if slot.slot == "text" else self.image_index
                if index is None:
                    raise TychoraError(
                        "CAPABILITY_MISSING",
                        "Required native vector index is unavailable",
                    )
                spec_data = slot.family.config_json.get("spec")
                try:
                    spec = FamilySpec.model_validate_json(canonical_bytes(spec_data))
                except (TypeError, ValueError):
                    _fail("Retained vector family specification is malformed")
                if spec.family_id != slot.family.family_id:
                    _fail("Retained vector family identity differs")
                handle = index.create_family(spec)
                if handle.config_sha != slot.family.config_sha:
                    _fail("Retained native vector family configuration differs")
                self.store.vector_indexes[slot.family.family_id] = index
            elif slot.slot == "graph":
                if self.graph_factory is None:
                    raise TychoraError(
                        "CAPABILITY_MISSING",
                        "Required graph generation factory is unavailable",
                    )
                manifest = slot.batches[0].graph_manifest
                if manifest is None:
                    _fail("Retained graph manifest is unavailable")
                if manifest.spec.generation_id in self.store.graph_indexes:
                    continue
                build = read_graph_build(self.store, manifest.spec, ctx)
                if GraphManifest.from_build(build) != manifest:
                    _fail("Retained graph canonical build differs")
                self.store.graph_indexes[manifest.spec.generation_id] = (
                    self.graph_factory(build)
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
