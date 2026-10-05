"""Synthetic native vector write victim, launched only by the crash matrix test."""

from __future__ import annotations

import sys
from pathlib import Path

from tests.acceptance.faults import FaultInjector
from tests.storage.conftest import _Family
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.contracts.identity import family_id
from tychora.contracts.indexing import FamilySpec
from tychora.ports.execution import ExecutionContext


def family() -> _Family:
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


def run(root: Path, point: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    injector = FaultInjector(root, point)
    index = ChromaVectorIndex(root / "vectors")
    spec = family()
    index.create_family(spec.spec)
    rows = [("r1", "u1", (1.0, 0.0))]
    if point == "half_embeddings":
        rows.append(("r2", "u2", (0.0, 1.0)))
        index._chunks = lambda items: [[item] for item in items]
    collection_type = type(index._collection)
    native_add = collection_type.add
    writes = 0

    def after_native_write(collection, *args, **kwargs):
        nonlocal writes
        result = native_add(collection, *args, **kwargs)
        writes += 1
        if writes == 1:
            injector.hit(point)
        return result

    collection_type.add = after_native_write
    index.upsert_batch(spec.batch(rows), ExecutionContext())
    raise AssertionError("The expected native-write boundary was not reached")


if __name__ == "__main__":
    run(Path(sys.argv[1]), sys.argv[2])
