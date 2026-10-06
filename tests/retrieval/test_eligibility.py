"""Compiled eligibility from pinned membership and detached current policy."""

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest

from tests.typed_json import json_object, json_string
from tychora.configuration.models import ResolvedConfig
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.indexing import IndexRecordDigest
from tychora.contracts.publication import (
    IndexFamily,
    LexicalBatchReceipt,
    LexicalRecord,
    PublicationBatch,
    PublicationSlot,
    SnapshotMember,
    SnapshotView,
    VectorRecordBinding,
)
from tychora.contracts.requests import QueryRequest, RequestScope
from tychora.contracts.vectors import BatchReceipt
from tychora.retrieval.eligibility import (
    AssetFact,
    ChunkFact,
    EligibilityCompiler,
    EligibilityFacts,
    RevisionFact,
    UnitFact,
    VectorFact,
)


def test_vector_group_dependency_serialization_is_order_independent():
    artifact = ArtifactRef(
        sha256=digest("cache"),
        size_bytes=1,
        media_type="application/octet-stream",
        storage_key="cache",
    )

    def binding(dependencies: frozenset[str]) -> VectorRecordBinding:
        return VectorRecordBinding(
            record_id="record",
            chunk_id="chunk",
            input_sha=digest("input"),
            value_sha=digest("value"),
            embedding_cache=artifact,
            group_id="group",
            dependency_ids=dependencies,
        )

    first = binding(frozenset(("unit-a", "unit-b")))
    second = binding(frozenset(("unit-b", "unit-a")))
    first_bytes = first.model_dump_json().encode()
    second_bytes = second.model_dump_json().encode()
    assert first_bytes == second_bytes
    assert json.loads(first_bytes)["dependency_ids"] == ["unit-a", "unit-b"]
    assert VectorRecordBinding.model_validate_json(first_bytes) == first


@pytest.fixture
def config(tmp_path: Path):
    workspace = tmp_path / "workspace.toml"
    workspace.write_text(
        'schema_version = 1\n[profiles.test]\nbuiltin = "documents@1"\n'
    )
    return resolve_config(workspace, "test", {})


@pytest.fixture
def scope():
    return RequestScope(
        principal_id="principal",
        collection_ids=frozenset({"collection"}),
        access_ceiling=2,
        uses=frozenset({"audit", "compose"}),
        capabilities=frozenset({"search"}),
    )


def _revision(**changes):
    fact = RevisionFact(
        source_id="source",
        revision_id="revision",
        extraction_id="extraction",
        collection_id="collection",
        kind_id="manual@1",
        access_rank=0,
        source_floor=0,
        collection_floor=0,
        allowed_uses=frozenset({"audit", "compose"}),
        review_state="accepted",
        withdrawn=False,
        published_on=None,
        effective_from=None,
        effective_until=None,
        is_default=True,
    )
    return fact.__class__(**{**fact.__dict__, **changes})


def _snapshot(config, chunks=("parent-chunk", "child-chunk"), vectors=(), images=()):
    artifact = ArtifactRef(
        sha256=digest("artifact"),
        size_bytes=1,
        media_type="application/json",
        storage_key="fixture",
    )
    family = IndexFamily(
        family_id=digest("family"),
        channel="lexical",
        backend="sqlite",
        config_sha=digest("family-config"),
        config_json=FrozenJsonMap(),
        backend_namespace="fixture",
        created_at="2025-01-01",
    )
    batch = PublicationBatch(
        batch_id="lexical-batch",
        family_id=family.family_id,
        extraction_id="extraction",
        input_manifest=artifact,
        expected_count=len(chunks),
        receipt=LexicalBatchReceipt(
            family_id=family.family_id,
            count=len(chunks),
            entry_ids_sha=digest("entries", chunks),
        ),
        lexical_records=tuple(LexicalRecord(chunk_id=chunk) for chunk in chunks),
    )
    slots = [PublicationSlot(slot="lexical", family=family, batches=(batch,))]
    if vectors:
        text_family = IndexFamily(
            family_id=digest("text-family"),
            channel="text",
            backend="chroma",
            config_sha=digest("text-config"),
            config_json=FrozenJsonMap(),
            backend_namespace="text-fixture",
            created_at="2025-01-01",
        )
        bindings = tuple(
            VectorRecordBinding(
                record_id=record_id,
                chunk_id=chunk_id,
                input_sha=digest("same-input"),
                value_sha=digest("value", record_id),
                embedding_cache=artifact,
                group_id=group_id,
                dependency_ids=frozenset(dependencies),
            )
            for record_id, chunk_id, group_id, dependencies in vectors
        )
        receipt = BatchReceipt(
            family_id=text_family.family_id,
            namespace_hash=digest("namespace"),
            config_sha=text_family.config_sha,
            records=tuple(
                IndexRecordDigest(
                    record_id=b.record_id, input_sha=b.input_sha, value_sha=b.value_sha
                )
                for b in bindings
            ),
            digest=digest("receipt"),
            count=len(bindings),
        )
        text_batch = PublicationBatch(
            batch_id="text-batch",
            family_id=text_family.family_id,
            extraction_id="extraction",
            input_manifest=artifact,
            expected_count=len(bindings),
            receipt=receipt,
            vector_records=bindings,
        )
        slots.append(
            PublicationSlot(slot="text", family=text_family, batches=(text_batch,))
        )
    if images:
        image_family = IndexFamily(
            family_id=digest("image-family"),
            channel="image",
            backend="chroma",
            config_sha=digest("image-config"),
            config_json=FrozenJsonMap(),
            backend_namespace="image-fixture",
            created_at="2025-01-01",
        )
        bindings = tuple(
            VectorRecordBinding(
                record_id=record_id,
                asset_id=asset_id,
                input_sha=digest("image-input", record_id),
                value_sha=digest("image-value", record_id),
                embedding_cache=artifact,
            )
            for record_id, asset_id in images
        )
        receipt = BatchReceipt(
            family_id=image_family.family_id,
            namespace_hash=digest("image-namespace"),
            config_sha=image_family.config_sha,
            records=tuple(
                IndexRecordDigest(
                    record_id=b.record_id, input_sha=b.input_sha, value_sha=b.value_sha
                )
                for b in bindings
            ),
            digest=digest("image-receipt"),
            count=len(bindings),
        )
        image_batch = PublicationBatch(
            batch_id="image-batch",
            family_id=image_family.family_id,
            extraction_id="extraction",
            input_manifest=artifact,
            expected_count=len(bindings),
            receipt=receipt,
            vector_records=bindings,
        )
        slots.append(
            PublicationSlot(slot="image", family=image_family, batches=(image_batch,))
        )
    return SnapshotView(
        snapshot_id="snapshot",
        workspace_id="workspace",
        membership_sha=digest("membership"),
        config_sha=sha256(
            canonical_bytes(
                {
                    "type": "registry",
                    "schema_version": "1",
                    "data": config.model_dump(mode="json"),
                }
            )
        ).hexdigest(),
        manifest=artifact,
        members=(
            SnapshotMember(
                source_id="source", revision_id="revision", extraction_id="extraction"
            ),
        ),
        slots=tuple(slots),
    )


def _register_config_artifact(db, artifacts, config):
    body = canonical_bytes(
        {
            "type": "registry",
            "schema_version": "1",
            "data": config.model_dump(mode="json"),
        }
    )
    ref = artifacts.put_stream(BytesIO(body), "application/json")
    db.execute(
        "INSERT OR IGNORE INTO artifacts(artifact_sha,size_bytes,media_type,storage_key,created_at) VALUES (?,?,?,?,?)",
        (ref.sha256, ref.size_bytes, ref.media_type, ref.storage_key, "2025-01-01"),
    )
    db.commit()


def _facts(
    *, child_review="accepted", source_floor=0, proxy_review="accepted", epoch=1
):
    return EligibilityFacts(
        policy_epoch=epoch,
        revisions=(_revision(source_floor=source_floor),),
        units=(
            UnitFact(
                "parent",
                "revision",
                "extraction",
                "paragraph",
                "accepted",
                True,
                frozenset({"revision", "parent", "child"}),
            ),
            UnitFact(
                "child",
                "revision",
                "extraction",
                "paragraph",
                child_review,
                True,
                frozenset({"revision", "child"}),
            ),
            UnitFact(
                "proxy",
                "revision",
                "extraction",
                "asset_proxy",
                proxy_review,
                False,
                frozenset({"revision", "proxy", "child", "asset"}),
                proxy_asset_id="asset",
            ),
        ),
        chunks=(
            ChunkFact(
                "parent-chunk", "parent", frozenset({"revision", "parent", "child"})
            ),
            ChunkFact("child-chunk", "child", frozenset({"revision", "child"})),
        ),
        assets=(
            AssetFact(
                "asset",
                "revision",
                "accepted",
                frozenset({"revision", "asset", "child"}),
                source_owner_ids=frozenset({"child"}),
            ),
        ),
    )


class _Reader:
    def __init__(self, config: ResolvedConfig, facts: EligibilityFacts):
        self.config = config
        self.registry = Registry.from_config(config)
        self.facts = facts
        self.epoch = facts.policy_epoch
        self.active_pins = 0
        self.pin: (
            Callable[[str | None], AbstractContextManager[SnapshotView]] | None
        ) = None

    def read_eligibility_facts(self, snapshot: SnapshotView) -> EligibilityFacts:
        return self.facts

    def retained_config(self, snapshot: SnapshotView) -> ResolvedConfig:
        raise TychoraError(
            "INVALID_REQUEST",
            "Synthetic reader has no retained configuration artifact.",
        )

    def policy_epoch(self, workspace_id: str) -> int:
        return self.epoch

    def pin_snapshot(
        self, selector: str | None
    ) -> AbstractContextManager[SnapshotView]:
        assert self.pin is not None, "this reader has no pinned snapshot fixture"
        return self.pin(selector)


def _request(mode="audit", **updates):
    return type.__call__(QueryRequest, question="Why?", mode=mode, **updates)


def test_reviewed_child_blocks_ancestor_representation_and_proxy(config, scope):
    reader = _Reader(config, _facts(child_review="rejected"))
    view = EligibilityCompiler(reader).compile_eligibility(
        _snapshot(config), scope, _request(), config
    )
    assert view.revision_ids == frozenset({"revision"})
    assert view.source_unit_ids == frozenset()
    assert view.context_unit_ids == frozenset()
    assert view.proxy_unit_ids == frozenset()
    assert view.chunk_ids == frozenset()
    assert view.asset_ids == frozenset()


def test_snapshot_semantic_digest_cannot_substitute_retained_artifact(config, scope):
    reader = _Reader(config, _facts())
    snapshot = _snapshot(config).model_copy(update={"config_sha": config.config_sha})
    with pytest.raises(TychoraError) as error:
        EligibilityCompiler(reader).compile_eligibility(
            snapshot, scope, _request(), config
        )
    assert error.value.code == "INVALID_REQUEST"


def test_access_floor_and_withdrawal_remove_every_channel(config, scope):
    snapshot = _snapshot(config)
    reader = _Reader(config, _facts(source_floor=3))
    denied = EligibilityCompiler(reader).compile_eligibility(
        snapshot, scope, _request(), config
    )
    assert denied.revision_ids == frozenset()
    reader.facts = replace(_facts(), revisions=(_revision(withdrawn=True),))
    withdrawn = EligibilityCompiler(reader).compile_eligibility(
        snapshot, scope, _request(), config
    )
    assert withdrawn.revision_ids == frozenset()


def test_mode_policy_changes_cache_identity_on_same_snapshot(config, scope):
    reader = _Reader(config, _facts())
    compiler = EligibilityCompiler(reader)
    snapshot = _snapshot(config)
    audit = compiler.compile_eligibility(snapshot, scope, _request("audit"), config)
    compose = compiler.compile_eligibility(snapshot, scope, _request("compose"), config)
    assert audit.snapshot_id == compose.snapshot_id
    assert audit.scope_digest == compose.scope_digest
    assert audit.eligibility_policy_digest != compose.eligibility_policy_digest
    assert "child" in audit.source_unit_ids


def test_provider_and_delivery_boundaries_detect_new_policy_epoch(config, scope):
    reader = _Reader(config, _facts())
    compiler = EligibilityCompiler(reader)
    view = compiler.compile_eligibility(_snapshot(config), scope, _request(), config)
    compiler.revalidate_epoch(view, "provider")
    reader.epoch += 1
    for stage in ("provider", "delivery"):
        with pytest.raises(TychoraError) as error:
            compiler.revalidate_epoch(view, stage)
        assert error.value.code == "SCOPE_CHANGED"


def test_sqlite_reader_detaches_current_floor_and_epoch(tmp_path, config):
    from tychora.adapters.files.cas import LocalArtifacts
    from tychora.adapters.sqlite.connection import open_corpus
    from tychora.adapters.sqlite.corpus import CorpusStore

    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    artifact_sha = digest("original")
    db.execute(
        "INSERT INTO workspaces(workspace_id,schema_version,created_at) VALUES ('workspace',1,'2025-01-01')"
    )
    db.execute(
        "INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES ('collection','workspace','synthetic','Synthetic',0,'{}')"
    )
    from tychora.contracts.identity import canonical_bytes

    envelope = json_object(
        Registry.from_config(config).historical_definitions(), "manual@1"
    )
    db.execute(
        "INSERT INTO kinds(kind_id,definition_hash,label,description,role,definition_json) VALUES ('manual@1',?,'Manual','Synthetic','standard',?)",
        (json_string(envelope["envelope_digest"]), canonical_bytes(envelope).decode()),
    )
    db.execute(
        "INSERT INTO sources(source_id,collection_id,source_key,source_type,registration_json,access_floor,created_at) VALUES ('source','collection','synthetic','document','{}',0,'2025-01-01')"
    )
    db.execute(
        "INSERT INTO artifacts(artifact_sha,size_bytes,media_type,storage_key,created_at) VALUES (?,1,'text/plain','original','2025-01-01')",
        (artifact_sha,),
    )
    db.execute(
        "INSERT INTO revisions(revision_id,source_id,kind_id,original_sha,edition_key,metadata_sha,title,access_rank,declared_review_state,allowed_uses_json,metadata_json,captured_at) VALUES ('revision','source','manual@1',?,'v1',?,'Synthetic',0,'accepted','[\"audit\",\"compose\"]','{}','2025-01-01')",
        (artifact_sha, digest("metadata-v1", FrozenJsonMap())),
    )
    db.execute(
        "INSERT INTO extractions(extraction_id,revision_id,parser_fingerprint,normalizer_fingerprint,document_artifact_sha,created_at) VALUES ('extraction','revision','parser','normalizer',?,'2025-01-01')",
        (artifact_sha,),
    )
    db.commit()
    store = CorpusStore(
        db,
        registry=Registry.from_config(config),
        artifacts=LocalArtifacts(tmp_path / "artifacts"),
    )
    _register_config_artifact(db, store.artifacts, config)
    snapshot = _snapshot(config, chunks=())
    first = store.read_eligibility_facts(snapshot)
    assert first.policy_epoch == 0
    assert first.revisions[0].source_floor == 0
    db.execute("UPDATE sources SET access_floor=3 WHERE source_id='source'")
    db.execute(
        "UPDATE workspaces SET policy_epoch=policy_epoch+1 WHERE workspace_id='workspace'"
    )
    db.commit()
    second = store.read_eligibility_facts(snapshot)
    assert second.policy_epoch == 1
    assert second.revisions[0].source_floor == 3
    assert store.policy_epoch(snapshot.workspace_id) == 1
    db.close()


def test_all_unit_filter_removes_context_and_group_dependent_chunks(config, scope):
    from tychora.contracts.requests import EqFilter

    original = _facts()
    facts = replace(
        original,
        revision_facets=(("revision", "scope.project", "alpha"),),
        units=tuple(
            replace(unit, facets=(("scope.project", "beta"),))
            if unit.unit_id == "child"
            else unit
            for unit in original.units
        ),
    )
    reader = _Reader(config, facts)
    view = EligibilityCompiler(reader).compile_eligibility(
        _snapshot(config),
        scope,
        _request(filters=EqFilter(field="scope.project", value="alpha")),
        config,
    )
    assert "child" not in view.context_unit_ids
    assert "parent" not in view.context_unit_ids
    assert not view.chunk_ids


def test_mixed_scope_or_is_rejected_before_eligibility(config, scope):
    from tychora.contracts.requests import EqFilter, OrFilter

    expression = OrFilter(
        children=(
            EqFilter(field="scope.project", value="alpha"),
            EqFilter(field="code.symbol_kind", value="function"),
        )
    )
    with pytest.raises(TychoraError) as error:
        EligibilityCompiler(_Reader(config, _facts())).compile_eligibility(
            _snapshot(config),
            scope,
            _request(filters=expression),
            config,
        )
    assert error.value.code == "INVALID_REQUEST"


def test_candidate_filter_preserves_allowed_context(config, scope):
    from tychora.contracts.requests import EqFilter

    original = _facts()
    facts = replace(
        original,
        units=tuple(
            replace(unit, facets=(("code.symbol_kind", "function"),))
            if unit.unit_id == "child"
            else unit
            for unit in original.units
        ),
    )
    view = EligibilityCompiler(_Reader(config, facts)).compile_eligibility(
        _snapshot(config),
        scope,
        _request(filters=EqFilter(field="code.symbol_kind", value="function")),
        config,
    )
    assert view.source_unit_ids == frozenset({"child"})
    assert {"parent", "child"} <= view.context_unit_ids


def test_candidate_filter_limits_proxy_lexical_without_removing_context(config, scope):
    from tychora.contracts.requests import EqFilter

    original = _facts()
    facts = replace(
        original,
        units=tuple(
            replace(unit, facets=(("code.symbol_kind", "function"),))
            if unit.unit_id == "proxy"
            else unit
            for unit in original.units
        ),
        chunks=(
            *original.chunks,
            ChunkFact(
                "proxy-chunk",
                "proxy",
                frozenset({"revision", "proxy", "child", "asset"}),
            ),
        ),
    )
    compiler = EligibilityCompiler(_Reader(config, facts))
    snapshot = _snapshot(config, chunks=("child-chunk", "proxy-chunk"))
    matching = compiler.compile_eligibility(
        snapshot,
        scope,
        _request(filters=EqFilter(field="code.symbol_kind", value="function")),
        config,
    )
    nonmatching = compiler.compile_eligibility(
        snapshot,
        scope,
        _request(filters=EqFilter(field="code.symbol_kind", value="class")),
        config,
    )
    assert "proxy-chunk" in matching.chunk_ids
    assert "proxy-chunk" not in nonmatching.chunk_ids
    assert "proxy" in matching.context_unit_ids & nonmatching.context_unit_ids
    assert "asset" in matching.asset_ids & nonmatching.asset_ids


def test_candidate_filter_limits_image_vector_per_complete_source_owner(config, scope):
    from tychora.contracts.requests import AndFilter, EqFilter

    original = _facts()
    facts = replace(
        original,
        units=tuple(
            replace(
                unit,
                facets=(
                    ("code.symbol_kind", "function"),
                    ("code.qualified_name", "example.function"),
                ),
            )
            if unit.unit_id == "child"
            else unit
            for unit in original.units
        ),
        vectors=(
            VectorFact(
                "image-vector",
                asset_id="asset",
                dependency_ids=frozenset({"revision", "asset", "child"}),
            ),
        ),
    )
    compiler = EligibilityCompiler(_Reader(config, facts))
    snapshot = _snapshot(config, images=(("image-vector", "asset"),))
    matching = compiler.compile_eligibility(
        snapshot,
        scope,
        _request(
            filters=AndFilter(
                children=(
                    EqFilter(field="code.symbol_kind", value="function"),
                    EqFilter(field="code.qualified_name", value="example.function"),
                )
            )
        ),
        config,
    )
    nonmatching = compiler.compile_eligibility(
        snapshot,
        scope,
        _request(filters=EqFilter(field="code.symbol_kind", value="class")),
        config,
    )
    assert "image-vector" in matching.vector_record_ids
    assert "image-vector" not in nonmatching.vector_record_ids
    assert "asset" in matching.asset_ids & nonmatching.asset_ids
    assert "child" in matching.context_unit_ids & nonmatching.context_unit_ids


def test_image_candidate_requires_one_owner_to_match_entire_expression(config, scope):
    from tychora.contracts.requests import AndFilter, EqFilter

    query = _request(
        filters=AndFilter(
            children=(
                EqFilter(field="code.symbol_kind", value="function"),
                EqFilter(field="code.qualified_name", value="example.function"),
            )
        )
    )
    snapshot = _snapshot(config, chunks=(), images=(("image-vector", "asset"),))

    def view(left_facets, right_facets, source_owners=frozenset({"left", "right"})):
        facts = EligibilityFacts(
            policy_epoch=1,
            revisions=(_revision(),),
            units=(
                UnitFact(
                    "left",
                    "revision",
                    "extraction",
                    "paragraph",
                    "accepted",
                    True,
                    frozenset({"revision", "left"}),
                    facets=left_facets,
                ),
                UnitFact(
                    "right",
                    "revision",
                    "extraction",
                    "paragraph",
                    "accepted",
                    True,
                    frozenset({"revision", "right"}),
                    facets=right_facets,
                ),
            ),
            chunks=(),
            assets=(
                AssetFact(
                    "asset",
                    "revision",
                    "accepted",
                    frozenset({"revision", "asset", "left", "right"}),
                    source_owner_ids=source_owners,
                ),
            ),
            vectors=(
                VectorFact(
                    "image-vector",
                    asset_id="asset",
                    dependency_ids=frozenset({"revision", "asset", "left", "right"}),
                ),
            ),
        )
        return EligibilityCompiler(_Reader(config, facts)).compile_eligibility(
            snapshot, scope, query, config
        )

    whole = view(
        (("code.symbol_kind", "function"), ("code.qualified_name", "example.function")),
        (),
    )
    split = view(
        (("code.symbol_kind", "function"),),
        (("code.qualified_name", "example.function"),),
    )
    ownerless = view(
        (("code.symbol_kind", "function"), ("code.qualified_name", "example.function")),
        (),
        frozenset(),
    )
    assert whole.vector_record_ids == frozenset({"image-vector"})
    assert split.vector_record_ids == frozenset()
    assert ownerless.vector_record_ids == frozenset()
    assert (
        whole.context_unit_ids == split.context_unit_ids == frozenset({"left", "right"})
    )
    assert whole.asset_ids == split.asset_ids == frozenset({"asset"})


@pytest.mark.asyncio
async def test_pinned_attempt_discards_stale_result_and_retries_once(config, scope):
    from contextlib import contextmanager

    from tychora.retrieval.eligibility import run_pinned_eligibility

    snapshot = _snapshot(config)
    reader = _Reader(config, _facts())
    reader.active_pins = 0

    @contextmanager
    def pin_snapshot(selector):
        assert selector == "snapshot"
        reader.active_pins += 1
        try:
            yield snapshot
        finally:
            reader.active_pins -= 1

    reader.pin = pin_snapshot
    calls = []

    async def operation(attempt):
        assert reader.active_pins == 1

        async def provider():
            calls.append(attempt.view.policy_epoch)
            if len(calls) == 1:
                reader.epoch = 2
                reader.facts = replace(reader.facts, policy_epoch=2)
            return "result-" + str(len(calls))

        return await attempt.owner_provider(provider)

    result = await run_pinned_eligibility(
        EligibilityCompiler(reader), "snapshot", scope, _request(), config, operation
    )
    assert result == "result-2"
    assert calls == [1, 2]
    assert reader.active_pins == 0


@pytest.mark.asyncio
async def test_second_epoch_change_returns_scope_changed(config, scope):
    from contextlib import contextmanager

    from tychora.retrieval.eligibility import run_pinned_eligibility

    snapshot = _snapshot(config)
    reader = _Reader(config, _facts())

    @contextmanager
    def pin_snapshot(selector):
        yield snapshot

    reader.pin = pin_snapshot

    async def operation(attempt):
        async def provider():
            reader.epoch += 1
            reader.facts = replace(reader.facts, policy_epoch=reader.epoch)
            return "stale"

        return await attempt.owner_provider(provider)

    with pytest.raises(TychoraError) as error:
        await run_pinned_eligibility(
            EligibilityCompiler(reader),
            "snapshot",
            scope,
            _request(),
            config,
            operation,
        )
    assert error.value.code == "SCOPE_CHANGED"


def test_rejected_overlapping_child_does_not_remove_unrelated_source(config, scope):
    original = _facts(child_review="rejected")
    facts = replace(
        original,
        units=(
            *original.units,
            UnitFact(
                "independent",
                "revision",
                "extraction",
                "paragraph",
                "accepted",
                True,
                frozenset({"revision", "independent"}),
            ),
        ),
        chunks=(
            *original.chunks,
            ChunkFact(
                "independent-chunk",
                "independent",
                frozenset({"revision", "independent"}),
            ),
        ),
    )
    view = EligibilityCompiler(_Reader(config, facts)).compile_eligibility(
        _snapshot(config, chunks=("parent-chunk", "child-chunk", "independent-chunk")),
        scope,
        _request(),
        config,
    )
    assert view.source_unit_ids == frozenset({"independent"})
    assert view.chunk_ids == frozenset({"independent-chunk"})


def test_original_kind_use_restriction_is_part_of_readability(config, scope):
    facts = replace(
        _facts(), revisions=(_revision(kind_allowed_uses=frozenset({"lookup"})),)
    )
    view = EligibilityCompiler(_Reader(config, facts)).compile_eligibility(
        _snapshot(config),
        scope,
        _request("audit"),
        config,
    )
    assert view.revision_ids == frozenset()


def test_attempt_dependency_lookup_carries_complete_group_leaves(config, scope):
    original = _facts()
    facts = replace(
        original,
        chunks=(
            ChunkFact(
                "parent-chunk", "parent", frozenset({"revision", "parent"}), "group"
            ),
            ChunkFact(
                "child-chunk", "child", frozenset({"revision", "child"}), "group"
            ),
        ),
        vectors=(
            VectorFact(
                "parent-vector",
                chunk_id="parent-chunk",
                dependency_ids=frozenset({"revision", "parent", "child"}),
            ),
            VectorFact(
                "child-vector",
                chunk_id="child-chunk",
                dependency_ids=frozenset({"revision", "parent", "child"}),
            ),
        ),
    )
    attempt = EligibilityCompiler(_Reader(config, facts)).compile_attempt(
        _snapshot(
            config,
            vectors=(
                (
                    "parent-vector",
                    "parent-chunk",
                    "group",
                    {"revision", "parent", "child"},
                ),
                (
                    "child-vector",
                    "child-chunk",
                    "group",
                    {"revision", "parent", "child"},
                ),
            ),
        ),
        scope,
        _request(),
        config,
    )
    assert attempt.dependencies_for("parent-chunk") == frozenset({"revision", "parent"})
    assert attempt.dependencies_for("parent-vector") == frozenset(
        {"revision", "parent", "child"}
    )
    assert attempt.dependencies_for("child-vector") == frozenset(
        {"revision", "parent", "child"}
    )
    with pytest.raises(TychoraError) as error:
        attempt.dependencies_for("unknown")
    assert error.value.code == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio
async def test_owner_runner_retries_after_canonical_withdrawal_and_releases_pin(
    tmp_path,
):
    from contextlib import contextmanager

    from tests.runtime.test_upload_policy import install, prepared
    from tychora.contracts.publication import SnapshotMember
    from tychora.ports.execution import ExecutionContext
    from tychora.retrieval.eligibility import run_pinned_eligibility

    with prepared(tmp_path) as (owner, scope):
        bundle = await owner.run_blocking(lambda: install(owner), ExecutionContext())
        await owner.run_blocking(
            lambda: _register_config_artifact(owner.db, owner.artifacts, owner.config),
            ExecutionContext(),
        )
        snapshot = _snapshot(owner.config, chunks=()).model_copy(
            update={
                "snapshot_id": "synthetic-pin",
                "workspace_id": owner.workspace_id,
                "members": (
                    SnapshotMember(
                        source_id=bundle.source.source_id,
                        revision_id=bundle.revision.revision_id,
                        extraction_id=bundle.extraction.extraction_id,
                    ),
                ),
            }
        )
        pins = []

        @contextmanager
        def pin_snapshot(selector):
            assert selector == "synthetic-pin"
            pins.append("enter")
            try:
                yield snapshot
            finally:
                pins.append("exit")

        owner.store.pin_snapshot = pin_snapshot
        ctx = ExecutionContext()
        calls = []

        async def operation(attempt):
            if not calls:
                calls.append("withdraw")
                await owner.run_blocking(
                    lambda: owner.store.withdraw_source(bundle.source.source_id), ctx
                )
            if not attempt.view.revision_ids:
                return "excluded"
            return await attempt.owner_provider(
                lambda: owner.run_provider(
                    "synthetic",
                    lambda: "remote-result",
                    ctx,
                    scope=scope,
                    dependencies=frozenset({bundle.units[0].unit_id}),
                    policy_epoch=attempt.view.policy_epoch,
                )
            )

        result = await run_pinned_eligibility(
            EligibilityCompiler(owner.store),
            "synthetic-pin",
            scope,
            _request("lookup"),
            owner.config,
            operation,
            owner=owner,
            ctx=ctx,
        )
        assert result == "excluded"
        assert calls == ["withdraw"]
        assert pins == ["enter", "exit"]


@pytest.mark.asyncio
async def test_owner_runner_releases_pin_after_cancellation(tmp_path):
    import asyncio
    from contextlib import contextmanager

    from tests.runtime.test_upload_policy import install, prepared
    from tychora.contracts.publication import SnapshotMember
    from tychora.ports.execution import ExecutionContext
    from tychora.retrieval.eligibility import run_pinned_eligibility

    with prepared(tmp_path) as (owner, scope):
        bundle = await owner.run_blocking(lambda: install(owner), ExecutionContext())
        await owner.run_blocking(
            lambda: _register_config_artifact(owner.db, owner.artifacts, owner.config),
            ExecutionContext(),
        )
        snapshot = _snapshot(owner.config, chunks=()).model_copy(
            update={
                "snapshot_id": "synthetic-pin",
                "workspace_id": owner.workspace_id,
                "members": (
                    SnapshotMember(
                        source_id=bundle.source.source_id,
                        revision_id=bundle.revision.revision_id,
                        extraction_id=bundle.extraction.extraction_id,
                    ),
                ),
            }
        )
        pins = []

        @contextmanager
        def pin_snapshot(selector):
            pins.append("enter")
            try:
                yield snapshot
            finally:
                pins.append("exit")

        owner.store.pin_snapshot = pin_snapshot
        ctx = ExecutionContext()

        async def operation(attempt):
            assert pins == ["enter"]
            ctx.cancelled.set()
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await run_pinned_eligibility(
                EligibilityCompiler(owner.store),
                "synthetic-pin",
                scope,
                _request("lookup"),
                owner.config,
                operation,
                owner=owner,
                ctx=ctx,
            )
        assert pins == ["enter", "exit"]


@pytest.mark.asyncio
async def test_owner_provider_denied_grant_never_dispatches(tmp_path):
    from contextlib import contextmanager

    from tests.runtime.test_upload_policy import install, prepared
    from tychora.contracts.publication import SnapshotMember
    from tychora.ports.execution import ExecutionContext
    from tychora.retrieval.eligibility import run_pinned_eligibility

    with prepared(tmp_path) as (owner, scope):
        bundle = await owner.run_blocking(lambda: install(owner), ExecutionContext())
        await owner.run_blocking(
            lambda: _register_config_artifact(owner.db, owner.artifacts, owner.config),
            ExecutionContext(),
        )
        snapshot = _snapshot(owner.config, chunks=()).model_copy(
            update={
                "snapshot_id": "synthetic-pin",
                "workspace_id": owner.workspace_id,
                "members": (
                    SnapshotMember(
                        source_id=bundle.source.source_id,
                        revision_id=bundle.revision.revision_id,
                        extraction_id=bundle.extraction.extraction_id,
                    ),
                ),
            }
        )

        @contextmanager
        def pin_snapshot(selector):
            yield snapshot

        owner.store.pin_snapshot = pin_snapshot
        denied_scope = scope.model_copy(update={"upload_providers": frozenset()})
        invoked = []
        ctx = ExecutionContext()

        async def operation(attempt):
            return await attempt.owner_provider(
                lambda: owner.run_provider(
                    "synthetic",
                    lambda: invoked.append("sent"),
                    ctx,
                    scope=denied_scope,
                    dependencies=frozenset({bundle.units[0].unit_id}),
                    policy_epoch=attempt.view.policy_epoch,
                )
            )

        with pytest.raises(TychoraError) as error:
            await run_pinned_eligibility(
                EligibilityCompiler(owner.store),
                "synthetic-pin",
                denied_scope,
                _request("lookup"),
                owner.config,
                operation,
                owner=owner,
                ctx=ctx,
            )
        assert error.value.code == "FORBIDDEN_SCOPE"
        assert invoked == []


def test_lexical_chunks_do_not_inherit_other_split_group_owners(config, scope):
    facts = EligibilityFacts(
        policy_epoch=1,
        revisions=(_revision(),),
        units=(
            UnitFact(
                "first",
                "revision",
                "extraction",
                "paragraph",
                "accepted",
                True,
                frozenset({"revision", "first"}),
            ),
            UnitFact(
                "second",
                "revision",
                "extraction",
                "paragraph",
                "rejected",
                True,
                frozenset({"revision", "second"}),
            ),
        ),
        chunks=(
            ChunkFact(
                "first-chunk",
                "first",
                frozenset({"revision", "first"}),
                "same-boundary",
            ),
            ChunkFact(
                "second-chunk",
                "second",
                frozenset({"revision", "second"}),
                "same-boundary",
            ),
        ),
        assets=(),
    )
    attempt = EligibilityCompiler(_Reader(config, facts)).compile_attempt(
        _snapshot(config, chunks=("first-chunk", "second-chunk")),
        scope,
        _request(),
        config,
    )
    assert attempt.view.chunk_ids == frozenset({"first-chunk"})
    assert attempt.dependencies_for("first-chunk") == frozenset({"revision", "first"})


def test_rejected_group_owner_blocks_only_its_vector_records(config, scope):
    facts = EligibilityFacts(
        policy_epoch=1,
        revisions=(_revision(),),
        units=(
            UnitFact(
                "a",
                "revision",
                "extraction",
                "paragraph",
                "accepted",
                True,
                frozenset({"revision", "a"}),
            ),
            UnitFact(
                "denied",
                "revision",
                "extraction",
                "paragraph",
                "rejected",
                True,
                frozenset({"revision", "denied"}),
            ),
            UnitFact(
                "b",
                "revision",
                "extraction",
                "paragraph",
                "accepted",
                True,
                frozenset({"revision", "b"}),
            ),
        ),
        chunks=(
            ChunkFact("a-chunk", "a", frozenset({"revision", "a"})),
            ChunkFact("denied-chunk", "denied", frozenset({"revision", "denied"})),
            ChunkFact("b-chunk", "b", frozenset({"revision", "b"})),
        ),
        assets=(),
        vectors=(
            VectorFact(
                "a-vector",
                chunk_id="a-chunk",
                dependency_ids=frozenset({"revision", "a", "denied"}),
            ),
            VectorFact(
                "denied-vector",
                chunk_id="denied-chunk",
                dependency_ids=frozenset({"revision", "a", "denied"}),
            ),
            VectorFact(
                "b-vector",
                chunk_id="b-chunk",
                dependency_ids=frozenset({"revision", "b"}),
            ),
        ),
    )
    snapshot = _snapshot(
        config,
        chunks=("a-chunk", "denied-chunk", "b-chunk"),
        vectors=(
            ("a-vector", "a-chunk", "group-a", {"revision", "a", "denied"}),
            ("denied-vector", "denied-chunk", "group-a", {"revision", "a", "denied"}),
            ("b-vector", "b-chunk", "group-b", {"revision", "b"}),
        ),
    )
    attempt = EligibilityCompiler(_Reader(config, facts)).compile_attempt(
        snapshot, scope, _request(), config
    )
    assert attempt.view.chunk_ids == frozenset({"a-chunk", "b-chunk"})
    assert attempt.view.vector_record_ids == frozenset({"b-vector"})
    assert attempt.dependencies_for("a-chunk") == frozenset({"revision", "a"})
    assert attempt.dependencies_for("b-vector") == frozenset({"revision", "b"})


def test_vector_fact_outside_selected_receipt_is_not_eligible(config, scope):
    original = _facts()
    facts = replace(
        original,
        vectors=(
            VectorFact(
                "foreign",
                chunk_id="child-chunk",
                dependency_ids=frozenset({"revision", "child"}),
            ),
        ),
    )
    view = EligibilityCompiler(_Reader(config, facts)).compile_eligibility(
        _snapshot(config),
        scope,
        _request(),
        config,
    )
    assert view.vector_record_ids == frozenset()


@pytest.mark.asyncio
async def test_retired_original_is_not_reopened_for_range_ownership(tmp_path):
    from tests.runtime.test_upload_policy import install, prepared
    from tychora.contracts.publication import SnapshotMember
    from tychora.ports.execution import ExecutionContext

    with prepared(tmp_path) as (owner, _scope):
        bundle = await owner.run_blocking(lambda: install(owner), ExecutionContext())
        await owner.run_blocking(
            lambda: _register_config_artifact(owner.db, owner.artifacts, owner.config),
            ExecutionContext(),
        )
        snapshot = _snapshot(owner.config, chunks=()).model_copy(
            update={
                "workspace_id": owner.workspace_id,
                "members": (
                    SnapshotMember(
                        source_id=bundle.source.source_id,
                        revision_id=bundle.revision.revision_id,
                        extraction_id=bundle.extraction.extraction_id,
                    ),
                ),
            }
        )

        def retire_and_read():
            owner.db.execute(
                "UPDATE artifacts SET payload_state='deleted',payload_deleted_at='2025-01-01' "
                "WHERE artifact_sha=?",
                (bundle.revision.original_sha,),
            )
            owner.db.commit()
            return owner.store.read_eligibility_facts(snapshot)

        facts = await owner.run_blocking(retire_and_read, ExecutionContext())
        assert facts.units[0].dependency_ids == frozenset(
            {bundle.revision.revision_id, bundle.units[0].unit_id}
        )


def test_request_cannot_exceed_resolved_limit(config, scope):
    with pytest.raises(TychoraError) as error:
        EligibilityCompiler(_Reader(config, _facts())).compile_eligibility(
            _snapshot(config),
            scope,
            _request(limit=config.values.limits.limit + 1),
            config,
        )
    assert error.value.code == "INVALID_REQUEST"


def test_only_explicit_global_revision_matches_missing_scoped_facet(config, scope):
    from tychora.contracts.requests import EqFilter

    request = _request(
        filters=EqFilter(field="scope.project", value="alpha"), include_global=True
    )
    global_facts = replace(
        _facts(), revisions=(_revision(global_facets=frozenset({"scope.project"})),)
    )
    normal = EligibilityCompiler(_Reader(config, _facts())).compile_eligibility(
        _snapshot(config), scope, request, config
    )
    global_view = EligibilityCompiler(
        _Reader(config, global_facts)
    ).compile_eligibility(_snapshot(config), scope, request, config)
    assert normal.context_unit_ids == frozenset()
    assert "child" in global_view.context_unit_ids


def test_new_decision_blocks_only_selected_graph_assertion(config, scope):
    from tychora.contracts.graph import (
        GraphBatchReceipt,
        GraphGenerationSpec,
        GraphManifest,
    )

    snapshot = _snapshot(config)
    selected_assertion = digest("selected-assertion")
    unrelated_assertion = digest("unrelated-assertion")
    family = IndexFamily(
        family_id=digest("graph-family"),
        channel="graph",
        backend="ladybug",
        config_sha=digest("graph-config"),
        config_json=FrozenJsonMap(),
        backend_namespace="graph-fixture",
        created_at="2025-01-01",
    )
    manifest = GraphManifest(
        spec=GraphGenerationSpec(
            generation_id="generation",
            family_id=family.family_id,
            config_sha=family.config_sha,
            registry_digest=config.registry_digest,
            workspace_id="workspace",
            decision_watermark=0,
        ),
        entity_ids=(),
        evidence_ids=(),
        assertion_ids=(selected_assertion,),
        content_digest=digest("content"),
    )
    receipt = GraphBatchReceipt(
        generation_id="generation",
        family_id=family.family_id,
        config_sha=family.config_sha,
        decision_watermark=0,
        manifest_digest=digest("graph-manifest-v1", manifest.model_dump(mode="json")),
        entity_count=0,
        evidence_count=0,
        assertion_count=1,
    )
    batch = PublicationBatch(
        batch_id="graph-batch",
        family_id=family.family_id,
        input_manifest=snapshot.manifest,
        expected_count=0,
        receipt=receipt,
        graph_manifest=manifest,
    )
    snapshot = snapshot.model_copy(
        update={
            "slots": (
                *snapshot.slots,
                PublicationSlot(slot="graph", family=family, batches=(batch,)),
            ),
        }
    )
    facts = replace(
        _facts(),
        assertion_decisions=((selected_assertion, 1), (unrelated_assertion, 1)),
    )
    view = EligibilityCompiler(_Reader(config, facts)).compile_eligibility(
        snapshot, scope, _request(), config
    )
    assert view.graph_generation_id == "generation"
    assert view.blocked_assertion_ids == frozenset({selected_assertion})
