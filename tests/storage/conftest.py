"""Explicit real vector family fixtures and a seeded lexical batch."""

import struct
from dataclasses import dataclass, field
from io import BytesIO

import pytest

from tests.helpers.corpus import reader, registry, seed_bundle
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.adapters.sqlite.repositories import SqliteUnitOfWork
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.embeddings import EmbeddingRecord
from tychora.contracts.identity import (
    canonical_bytes,
    digest,
    family_id,
    new_family_nonce,
    space_id,
)
from tychora.contracts.indexing import FamilySpec
from tychora.contracts.publication import LexicalBatchReceipt, NewBatch
from tychora.contracts.snapshots import IndexFamily
from tychora.contracts.vectors import VectorBatch, WorkLimits
from tychora.ports.execution import ExecutionContext


@dataclass(frozen=True)
class _Family:
    spec: FamilySpec
    limits: WorkLimits = field(default_factory=WorkLimits)

    def batch(self, rows: list[tuple[str, str, tuple[float, float]]]) -> VectorBatch:
        records = tuple(
            EmbeddingRecord.from_normalized_bytes(
                record_id=record_id,
                input_sha="a" * 64,
                space_id=space_id(
                    self.spec.modality,
                    self.spec.provider,
                    self.spec.model,
                    self.spec.dimensions,
                    self.spec.metric,
                    self.spec.representation_fingerprint,
                ),
                stored=struct.pack("<ff", *values),
            )
            for record_id, _owner_id, values in rows
        )
        return VectorBatch(family_id=self.spec.family_id, records=records)

    def query(self, values: tuple[float, float]) -> bytes:
        return struct.pack("<ff", *values)


@dataclass(frozen=True)
class _Execution:
    ctx: ExecutionContext = field(default_factory=ExecutionContext)


@pytest.fixture
def vector_family() -> _Family:
    nonce = "00000000-0000-4000-8000-000000000023"
    return _Family(
        FamilySpec(
            family_id=family_id("workspace", "c" * 64, nonce),
            workspace_id="workspace",
            config_sha="c" * 64,
            creation_uuid=nonce,
            modality="text",
            provider="synthetic",
            model="two-dimensional",
            dimensions=2,
            metric="cosine",
            normalization="unit-l2",
            representation_fingerprint="d" * 64,
            backend_namespace="synthetic-text",
            backend_space="ip",
        )
    )


@pytest.fixture
def execution() -> _Execution:
    return _Execution()


@pytest.fixture
def vector_index(tmp_path, vector_family: _Family) -> ChromaVectorIndex:
    index = ChromaVectorIndex(tmp_path / "vectors")
    index.create_family(vector_family.spec)
    return index


@pytest.fixture
def seeded_batch(tmp_path):
    """A committed extraction, its lexical family and a not yet begun batch."""
    db = open_corpus(tmp_path / "c.sqlite3", initialize=True)
    store = CorpusStore(db, registry=registry(), artifacts=reader())
    bundle = seed_bundle("Record verification outcomes.")
    store.commit_extraction(bundle)
    nonce = new_family_nonce()
    family = IndexFamily(
        family_id=family_id(bundle.workspace.workspace_id, "c" * 64, nonce),
        channel="lexical",
        backend="sqlite-fts5",
        config_sha="c" * 64,
        config_json=FrozenJsonMap({"effective_config_sha": "c" * 64}),
        backend_namespace="lexical",
        created_at=bundle.workspace.created_at,
    )
    uow = SqliteUnitOfWork(db)
    with uow.write() as tx:
        tx.families.create(family, nonce, "c" * 64)
    entries = (bundle.chunks[0].chunk_id,)
    manifest = reader().put_stream(
        BytesIO(
            canonical_bytes(
                {
                    "type": "index_input",
                    "schema_version": "1",
                    "data": {"entries": entries},
                }
            )
        ),
        "application/json",
    )
    batch = NewBatch(
        slot="lexical",
        batch_id=digest(
            "index-batch-v1",
            family.family_id,
            bundle.extraction.extraction_id,
            manifest.sha256,
        ),
        family_id=family.family_id,
        extraction_id=bundle.extraction.extraction_id,
        input_manifest=manifest,
        expected_count=1,
        receipt=LexicalBatchReceipt(
            family_id=family.family_id,
            count=1,
            entry_ids_sha=digest("index-entries-v1", entries),
        ),
    )
    yield db, uow, batch, entries
    db.close()
