"""Explicit real vector family fixtures."""

import struct
from dataclasses import dataclass, field

import pytest

from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.contracts.embeddings import EmbeddingRecord
from tychora.contracts.identity import family_id, space_id
from tychora.contracts.indexing import FamilySpec
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
