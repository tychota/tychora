"""Immutable Chroma families and bounded eligible-owner search."""

from __future__ import annotations

import json
import math
import struct
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import chromadb
from chromadb.api.collection_configuration import CreateHNSWConfiguration
from chromadb.config import Settings
from chromadb.errors import NotFoundError

from tychora.contracts.embeddings import EmbeddingRecord
from tychora.contracts.identity import digest, space_id
from tychora.contracts.indexing import FamilySpec, IndexRecordDigest
from tychora.contracts.vector_batches import vector_value_sha
from tychora.contracts.vectors import (
    BatchReceipt,
    CandidateKey,
    CandidatePage,
    DenseCandidate,
    FamilyHandle,
    VectorBatch,
    VectorOwner,
    Verification,
    WorkLimits,
)
from tychora.indexing.receipts import receipt_digest
from tychora.ports.execution import ExecutionContext

_PARTITION_SIZE = 5_000
_EF_SEARCH = 200
_QUALIFIED_HNSW: CreateHNSWConfiguration = {
    "space": "ip",
    "ef_construction": 100,
    "ef_search": _EF_SEARCH,
    "max_neighbors": 16,
    "resize_factor": 1.2,
    "sync_threshold": 1_000,
}
_QUALIFIED_CONFIGURATION = {
    "hnsw": _QUALIFIED_HNSW,
    "spann": None,
    "embedding_function": None,
}


class _PresenceDeadline(Exception):
    """The eligible-ID scan ended before full membership was known."""


def _float32(raw: bytes, dimensions: int) -> tuple[float, ...]:
    if len(raw) != 4 * dimensions:
        raise ValueError("Vector dimensions differ from family")
    values = struct.unpack("<" + "f" * dimensions, raw)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Vector must be finite")
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isclose(norm, 1.0, rel_tol=2e-6, abs_tol=2e-6):
        raise ValueError("Vector must already be unit-normalized float32")
    return values


def _bytes(values: Sequence[float]) -> bytes:
    return struct.pack("<" + "f" * len(values), *values)


def _metadatas(
    records: Sequence[EmbeddingRecord], owners: Sequence[VectorOwner]
) -> list[dict[str, str]]:
    """Chroma metadata per record: its digests and, when known, its owner."""
    by_record = {owner.record_id: owner for owner in owners}
    result = []
    for item in records:
        values = {"input_sha": item.input_sha, "value_sha": item.value_sha}
        owner = by_record.get(item.record_id)
        if owner is not None:
            values |= {
                "revision_id": owner.revision_id,
                "extraction_id": owner.extraction_id,
                "owner_kind": owner.owner_kind,
                "owner_id": owner.owner_id,
            }
        result.append(values)
    return result


class ChromaVectorIndex:
    """One active immutable family in a persistent Chroma store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=str(self.path), settings=Settings(anonymized_telemetry=False)
        )
        self._collection: Any = None
        self._handle: FamilyHandle | None = None

    @staticmethod
    def _name(spec: FamilySpec) -> str:
        return "family_" + spec.family_id

    @staticmethod
    def _metadata(spec: FamilySpec) -> dict[str, str]:
        return {
            "family_spec": json.dumps(spec.model_dump(mode="json"), sort_keys=True),
            "namespace_hash": digest(
                "chroma-namespace-v1", spec.backend_namespace, spec.family_id
            ),
            "physical_space": "ip",
            "normalization_policy": "unit-l2-float32-le-v1",
        }

    @staticmethod
    def _config_hash(collection: Any) -> str:
        return digest("chroma-physical-config-v1", collection.configuration)

    def _require(self) -> tuple[Any, FamilyHandle]:
        if self._collection is None or self._handle is None:
            raise ValueError("Vector family has not been opened")
        return self._collection, self._handle

    def _check_collection(self, collection: Any, spec: FamilySpec) -> None:
        if (
            collection.metadata != self._metadata(spec)
            or collection.configuration != _QUALIFIED_CONFIGURATION
        ):
            raise ValueError("Chroma family configuration differs from immutable spec")

    def create_family(self, spec: FamilySpec) -> FamilyHandle:
        if (
            spec.metric != "cosine"
            or spec.normalization != "unit-l2"
            or spec.backend_space != "ip"
        ):
            raise ValueError("Unsupported vector family configuration")
        if self._handle is not None and self._handle.spec != spec:
            raise ValueError("Index is bound to another family configuration")
        collection = self._client.get_or_create_collection(
            name=self._name(spec),
            embedding_function=None,
            metadata=self._metadata(spec),
            configuration={"hnsw": _QUALIFIED_HNSW},
        )
        self._check_collection(collection, spec)
        handle = FamilyHandle(
            spec=spec,
            namespace_hash=self._metadata(spec)["namespace_hash"],
            config_sha=self._config_hash(collection),
        )
        self._collection, self._handle = collection, handle
        return handle

    def open_family(self, spec: FamilySpec) -> FamilyHandle | None:
        """Bind an existing ready collection without creating a replacement."""
        if self._handle is not None and self._handle.spec != spec:
            raise ValueError("Index is bound to another family configuration")
        try:
            collection = self._client.get_collection(
                name=self._name(spec), embedding_function=None
            )
        except NotFoundError:
            return None
        self._check_collection(collection, spec)
        handle = FamilyHandle(
            spec=spec,
            namespace_hash=self._metadata(spec)["namespace_hash"],
            config_sha=self._config_hash(collection),
        )
        self._collection, self._handle = collection, handle
        return handle

    @staticmethod
    def _chunks[T](ids: Sequence[T], size: int = _PARTITION_SIZE) -> list[list[T]]:
        return [list(ids[start : start + size]) for start in range(0, len(ids), size)]

    def _present(
        self,
        ids: Sequence[str],
        *,
        embeddings: bool = False,
        ctx: ExecutionContext | None = None,
        deadline: float | None = None,
    ) -> dict[str, tuple[str, str, bytes]]:
        collection, _ = self._require()
        found: dict[str, tuple[str, str, bytes]] = {}
        for chunk in self._chunks(ids):
            if not chunk:
                continue
            self._presence_checkpoint(ctx, deadline)
            result = collection.get(
                ids=chunk,
                include=["metadatas", "embeddings"] if embeddings else ["metadatas"],
            )
            self._presence_checkpoint(ctx, deadline)
            vectors = result["embeddings"] if embeddings else None
            for index, record_id in enumerate(result["ids"]):
                metadata = result["metadatas"][index]
                raw = _bytes(vectors[index]) if vectors is not None else b""
                found[record_id] = (metadata["input_sha"], metadata["value_sha"], raw)
        return found

    @staticmethod
    def _presence_checkpoint(
        ctx: ExecutionContext | None, deadline: float | None
    ) -> None:
        if ctx is not None:
            ctx.checkpoint()
        if deadline is not None and time.monotonic() >= deadline:
            raise _PresenceDeadline

    def upsert_batch(self, batch: VectorBatch, ctx: ExecutionContext) -> BatchReceipt:
        collection, handle = self._require()
        self._check_collection(collection, handle.spec)
        if batch.family_id != handle.spec.family_id:
            raise ValueError("Batch belongs to another family")
        expected_space = space_id(
            handle.spec.modality,
            handle.spec.provider,
            handle.spec.model,
            handle.spec.dimensions,
            handle.spec.metric,
            handle.spec.representation_fingerprint,
        )
        for record in batch.records:
            ctx.checkpoint()
            if (
                record.space_id != expected_space
                or len(record.values) != handle.spec.dimensions
            ):
                raise ValueError("Vector embedding space differs from family")
            raw = _bytes(record.values)
            _float32(raw, handle.spec.dimensions)
            if vector_value_sha(raw) != record.value_sha:
                raise ValueError("Vector value digest differs from exact bytes")
        existing = self._present(
            [record.record_id for record in batch.records], embeddings=True, ctx=ctx
        )
        for record in batch.records:
            actual = existing.get(record.record_id)
            if actual is not None and actual != (
                record.input_sha,
                record.value_sha,
                _bytes(record.values),
            ):
                raise ValueError("Conflicting immutable vector record")
        additions = [
            record for record in batch.records if record.record_id not in existing
        ]
        for chunk in self._chunks(additions):
            ctx.checkpoint()
            collection.add(
                ids=[item.record_id for item in chunk],
                embeddings=[list(item.values) for item in chunk],
                metadatas=_metadatas(chunk, batch.owners),
            )
        records = tuple(
            sorted(
                (
                    IndexRecordDigest(
                        record_id=item.record_id,
                        input_sha=item.input_sha,
                        value_sha=item.value_sha,
                    )
                    for item in batch.records
                ),
                key=lambda item: item.record_id,
            )
        )
        return BatchReceipt(
            family_id=handle.spec.family_id,
            namespace_hash=handle.namespace_hash,
            config_sha=handle.config_sha,
            records=records,
            digest=receipt_digest(records),
            count=len(records),
        )

    def verify_batch(self, receipt: BatchReceipt) -> Verification:
        collection, handle = self._require()
        config_matches = (
            receipt.family_id == handle.spec.family_id
            and receipt.namespace_hash == handle.namespace_hash
            and receipt.config_sha == handle.config_sha
            and receipt.count == len(receipt.records)
            and receipt.digest == receipt_digest(receipt.records)
        )
        try:
            self._check_collection(collection, handle.spec)
        except ValueError:
            config_matches = False
        found = self._present(
            [record.record_id for record in receipt.records], embeddings=True
        )
        missing = tuple(
            sorted(
                record.record_id
                for record in receipt.records
                if record.record_id not in found
            )
        )
        mismatched_ids: list[str] = []
        for record in receipt.records:
            stored = found.get(record.record_id)
            if stored is not None and (
                stored[0] != record.input_sha
                or stored[1] != record.value_sha
                or vector_value_sha(stored[2]) != record.value_sha
            ):
                mismatched_ids.append(record.record_id)
        mismatched = tuple(sorted(mismatched_ids))
        return Verification(
            ready=config_matches and not missing and not mismatched,
            missing_record_ids=missing,
            mismatched_record_ids=mismatched,
            config_matches=config_matches,
        )

    def search(
        self,
        query_vector: bytes,
        eligible_records_and_owners: Mapping[str, CandidateKey],
        candidate_limit: int,
        work_limits: WorkLimits,
        ctx: ExecutionContext,
    ) -> CandidatePage:
        collection, handle = self._require()
        self._check_collection(collection, handle.spec)
        query = _float32(query_vector, handle.spec.dimensions)
        if candidate_limit <= 0:
            raise ValueError("Candidate limit must be positive")
        if (
            len(eligible_records_and_owners)
            > _PARTITION_SIZE * work_limits.max_partitions
        ):
            raise ValueError("Eligible scope exceeds partition limit")
        ctx.checkpoint()
        if not eligible_records_and_owners:
            return CandidatePage(
                candidates=(),
                missing_record_ids=(),
                coverage_complete=True,
                truncated=False,
                exhausted=True,
                returned_records=0,
                queried_partitions=0,
            )
        try:
            present = self._present(
                sorted(eligible_records_and_owners),
                ctx=ctx,
                deadline=work_limits.deadline,
            )
        except _PresenceDeadline:
            return CandidatePage(
                candidates=(),
                missing_record_ids=(),
                coverage_complete=False,
                truncated=True,
                exhausted=False,
                returned_records=0,
                queried_partitions=0,
            )
        missing = tuple(sorted(set(eligible_records_and_owners) - present.keys()))
        pools = (
            sorted(
                record_id
                for record_id in present
                if eligible_records_and_owners[record_id][0] == kind
            )
            for kind in ("unit", "asset")
        )
        partitions = [chunk for pool in pools for chunk in self._chunks(pool)]
        if len(partitions) > work_limits.max_partitions:
            raise ValueError("Eligible scope exceeds partition limit")
        requests = [
            min(2 * candidate_limit, len(partition)) for partition in partitions
        ]
        seen: dict[str, float] = {}
        tails = [math.inf] * len(partitions)
        returned = 0
        queried: set[int] = set()
        truncated = False
        while True:
            progressed = False
            ranked = self._rank(seen, eligible_records_and_owners)
            for index, partition in enumerate(partitions):
                kind = eligible_records_and_owners[partition[0]][0]
                pool = [item for item in ranked if item.key[0] == kind]
                threshold = (
                    pool[candidate_limit - 1].distance
                    if len(pool) >= candidate_limit
                    else math.inf
                )
                if requests[index] == 0 or (
                    index in queried
                    and len(pool) >= candidate_limit
                    and tails[index] > threshold
                ):
                    continue
                if (
                    work_limits.deadline is not None
                    and time.monotonic() >= work_limits.deadline
                ):
                    truncated = True
                    break
                remaining = work_limits.max_returned_records - returned
                if remaining <= 0:
                    truncated = True
                    break
                requested = min(requests[index], remaining)
                budget_clipped = requested < requests[index]
                ctx.checkpoint()
                raw = collection.query(
                    query_embeddings=[list(query)],
                    ids=partition,
                    n_results=requested,
                    include=["distances"],
                )
                queried.add(index)
                ids = raw["ids"][0]
                distances = raw["distances"][0]
                returned += len(ids)
                for record_id, distance in zip(ids, distances, strict=True):
                    seen[record_id] = min(seen.get(record_id, math.inf), distance)
                tails[index] = (
                    math.inf
                    if requested >= len(partition)
                    else (distances[-1] if distances else math.inf)
                )
                requests[index] = (
                    0
                    if requested >= len(partition)
                    else min(len(partition), 2 * requested)
                )
                if requested == len(partition) and len(ids) < len(partition):
                    truncated = True
                progressed = True
                ranked = self._rank(seen, eligible_records_and_owners)
                if budget_clipped:
                    truncated = True
                    break
            if truncated or not progressed:
                break
        ranked = self._rank(seen, eligible_records_and_owners)
        exhausted = all(request == 0 for request in requests) and not truncated
        return CandidatePage(
            candidates=tuple(
                item
                for kind in ("unit", "asset")
                for item in [item for item in ranked if item.key[0] == kind][
                    :candidate_limit
                ]
            ),
            missing_record_ids=missing,
            coverage_complete=not missing,
            truncated=truncated,
            exhausted=exhausted,
            returned_records=returned,
            queried_partitions=len(queried),
        )

    @staticmethod
    def _rank(
        seen: Mapping[str, float], eligible: Mapping[str, CandidateKey]
    ) -> list[DenseCandidate]:
        grouped: dict[CandidateKey, list[tuple[str, float]]] = defaultdict(list)
        for record_id, distance in seen.items():
            grouped[eligible[record_id]].append((record_id, distance))
        return sorted(
            (
                DenseCandidate(
                    key=key,
                    distance=min(distance for _, distance in rows),
                    record_ids=tuple(
                        record_id
                        for record_id, _ in sorted(
                            rows, key=lambda row: (row[1], row[0])
                        )
                    ),
                )
                for key, rows in grouped.items()
            ),
            key=lambda item: (item.distance, item.key),
        )

    def drop_records(self, record_ids: Sequence[str], ctx: ExecutionContext) -> None:
        collection, handle = self._require()
        self._check_collection(collection, handle.spec)
        for chunk in self._chunks(sorted(set(record_ids))):
            ctx.checkpoint()
            collection.delete(ids=chunk)
