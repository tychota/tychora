"""Immutable Chroma families and owner search restricted to an eligibility view."""

from __future__ import annotations

import json
import math
import struct
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import chromadb
from chromadb.api.collection_configuration import CreateHNSWConfiguration
from chromadb.config import Settings
from chromadb.errors import NotFoundError

from tychora.contracts.candidates import CandidateKey
from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.embeddings import EmbeddingRecord
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest, space_id
from tychora.contracts.indexing import FamilySpec, IndexRecordDigest
from tychora.contracts.vector_batches import vector_value_sha
from tychora.contracts.vectors import (
    BatchReceipt,
    FamilyHandle,
    VectorBatch,
    VectorOwner,
    Verification,
    partition_size,
)
from tychora.indexing.receipts import receipt_digest
from tychora.ports.execution import ExecutionContext
from tychora.ports.search import RankedOwner, RankedOwners

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
REVISION_FILTER_LIMIT = 512
OVERFETCH_FACTOR = 4
OVERFETCH_DOUBLINGS = 2
OVERFETCH_CAP = 1024

type _Hit = tuple[str, float, Mapping[str, str]]


def _owner_key(metadata: Mapping[str, str]) -> CandidateKey:
    """The unit or asset a record's metadata names as its owner."""
    kind = metadata.get("owner_kind")
    if kind not in ("unit", "asset") or not metadata.get("owner_id"):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Vector record metadata names no unit or asset owner",
            reason="canonical.binding_mismatch",
        )
    return (kind, metadata["owner_id"])


def _kept(raw: Mapping[str, Any], revisions: Mapping[str, object]) -> list[_Hit]:
    """The query's hits whose metadata revision the view grants."""
    return [
        (record_id, distance, metadata)
        for record_id, distance, metadata in zip(
            raw["ids"][0], raw["distances"][0], raw["metadatas"][0], strict=True
        )
        if metadata is not None and metadata.get("revision_id") in revisions
    ]


def _owners(hits: Sequence[_Hit]) -> tuple[RankedOwner, ...]:
    """Every owner of the page, ranked per kind by its nearest record.

    No cut here: admission can drop records, so the caller cuts each canonical
    kind after it; the page itself is bounded by ``OVERFETCH_CAP``.
    """
    grouped: dict[CandidateKey, list[tuple[float, str]]] = defaultdict(list)
    for record_id, distance, metadata in hits:
        grouped[_owner_key(metadata)].append((distance, record_id))
    ranked: list[RankedOwner] = []
    for kind in ("unit", "asset"):
        pool = sorted(
            (sorted(rows), key) for key, rows in grouped.items() if key[0] == kind
        )
        ranked.extend(
            RankedOwner(
                key=key,
                rank=rank,
                score=-rows[0][0],
                window_ids=tuple(record for _, record in rows),
                window_scores=tuple(-distance for distance, _ in rows),
            )
            for rank, (rows, key) in enumerate(pool, 1)
        )
    return tuple(ranked)


def _enough(hits: Sequence[_Hit], limit: int) -> bool:
    """Whether every owner kind in the page already has ``limit`` owners."""
    counts: dict[str, set[str]] = defaultdict(set)
    for _, _, metadata in hits:
        kind, owner = _owner_key(metadata)
        counts[kind].add(owner)
    return bool(counts) and all(len(owners) >= limit for owners in counts.values())


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
    ) -> dict[str, tuple[str, str, bytes]]:
        collection, _ = self._require()
        found: dict[str, tuple[str, str, bytes]] = {}
        for chunk in self._chunks(ids):
            if not chunk:
                continue
            if ctx is not None:
                ctx.checkpoint()
            result = collection.get(
                ids=chunk,
                include=["metadatas", "embeddings"] if embeddings else ["metadatas"],
            )
            if ctx is not None:
                ctx.checkpoint()
            vectors = result["embeddings"] if embeddings else None
            for index, record_id in enumerate(result["ids"]):
                metadata = result["metadatas"][index]
                raw = _bytes(vectors[index]) if vectors is not None else b""
                found[record_id] = (metadata["input_sha"], metadata["value_sha"], raw)
        return found

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
        for chunk in self._chunks(additions, partition_size(handle.spec.dimensions)):
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
        self, view: EligibilityView, vector: bytes, limit: int, ctx: ExecutionContext
    ) -> RankedOwners:
        """Every owner of the nearest page among the view's revisions.

        The store filters by revision when the view lists at most
        ``REVISION_FILTER_LIMIT`` revisions; otherwise hits are kept by their
        metadata revision. The page over-fetches ``OVERFETCH_FACTOR`` records
        per wanted owner and doubles while some owner kind has fewer than
        ``limit`` owners and the page did not hold every stored record;
        ``truncated`` when the doublings or the cap ran out first, and
        ``detail["exhausted"]`` is 1 when the page held every stored record.
        Canonical owners, admission and the per-kind cut are the caller's.
        """
        collection, handle = self._require()
        self._check_collection(collection, handle.spec)
        query = list(_float32(vector, handle.spec.dimensions))
        if not view.revisions or limit <= 0:
            return RankedOwners(
                owners=(),
                detail={"returned": 0, "queries": 0, "rescored": 0, "exhausted": 1},
            )
        revisions = sorted(view.revisions)
        stored = collection.count()
        wanted = min(OVERFETCH_FACTOR * limit, OVERFETCH_CAP)
        queries = 0
        while True:
            ctx.checkpoint()
            # A filtered HNSW page can come back short while matching records
            # remain, so a short page proves nothing. Exhaustion is proven only
            # by a page holding every stored record: once the whole collection
            # fits in the page it is queried unfiltered and filtered here.
            filtered = len(revisions) <= REVISION_FILTER_LIMIT and stored > wanted
            raw = collection.query(
                query_embeddings=[query],
                n_results=wanted,
                where={"revision_id": {"$in": revisions}} if filtered else None,
                include=["distances", "metadatas"],
            )
            queries += 1
            returned = len(raw["ids"][0])
            hits = _kept(raw, view.revisions)
            enough = _enough(hits, limit)
            exhausted = returned >= stored
            if (
                enough
                or exhausted
                or queries > OVERFETCH_DOUBLINGS
                or wanted >= OVERFETCH_CAP
            ):
                break
            wanted = min(2 * wanted, OVERFETCH_CAP)
        return RankedOwners(
            owners=_owners(hits),
            truncated=not enough and not exhausted,
            detail={
                "returned": returned,
                "queries": queries,
                "rescored": 0,
                "exhausted": int(exhausted),
            },
        )

    def drop_records(self, record_ids: Sequence[str], ctx: ExecutionContext) -> None:
        collection, handle = self._require()
        self._check_collection(collection, handle.spec)
        for chunk in self._chunks(sorted(set(record_ids))):
            ctx.checkpoint()
            collection.delete(ids=chunk)
