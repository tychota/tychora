"""Privacy-limited query-run diagnostics on the canonical owner connection."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from io import BytesIO

from tychora.adapters.files.references import MAX_MANIFEST_BYTES, required_refs
from tychora.adapters.sqlite.repositories import artifact_ref
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import Contract, NonNegativeInt, Sha256
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.requests import (
    QueryRequest,
    RequestScope,
    query_run_scope_digest,
    query_run_scope_matches,
)
from tychora.ports.artifacts import Artifacts
from tychora.retrieval.response import DeliveredResult


class RunMetrics(Contract):
    """Only counts, elapsed time and irreversible request/config fingerprints."""

    policy_epoch: NonNegativeInt
    lexical_statistics_epoch: NonNegativeInt
    config_sha: Sha256
    request_fingerprint: Sha256
    duration_ms: NonNegativeInt
    candidate_count: NonNegativeInt
    cache_hit: bool = False


class HistoryRepository:
    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        retention_days: int = 30,
        retain_payloads: bool = False,
        artifacts: Artifacts | None = None,
    ) -> None:
        if retention_days <= 0:
            raise ValueError("History retention must be positive")
        self._db = db
        self._now = now
        self._retention = timedelta(days=retention_days)
        self._retain_payloads = retain_payloads
        self._artifacts = artifacts

    @property
    def retains_requests(self) -> bool:
        return self._retain_payloads

    def retain_request(
        self,
        run_id: str,
        request: QueryRequest,
        *,
        scope: RequestScope,
        snapshot_id: str,
        config_sha: str,
        projection_fingerprint: str,
        provider_fingerprint: str,
    ) -> ArtifactRef:
        """Store one explicitly opted-in typed local replay manifest."""
        if not self._retain_payloads or self._artifacts is None:
            raise TychoraError(
                "INVALID_REQUEST", "Full local query history is disabled."
            )
        body = canonical_bytes(
            {
                "type": "retained_run",
                "schema_version": "1",
                "required_artifacts": [],
                "data": {
                    "run_id": run_id,
                    "request": request.model_dump(mode="json"),
                    "principal_id": scope.principal_id,
                    "scope_digest": query_run_scope_digest(scope),
                    "snapshot_id": snapshot_id,
                    "config_sha": config_sha,
                    "projection_fingerprint": projection_fingerprint,
                    "provider_fingerprint": provider_fingerprint,
                },
            }
        )
        if len(body) > MAX_MANIFEST_BYTES:
            raise TychoraError("INVALID_REQUEST", "Retained query exceeds local limit.")
        manifest = json.loads(body)
        required_refs(manifest, capture_published=True)
        ref = self._artifacts.put_stream(BytesIO(body), "application/json")
        row = self._db.execute(
            "SELECT size_bytes,media_type,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
            (ref.sha256,),
        ).fetchone()
        if row is not None and row != (
            ref.size_bytes,
            ref.media_type,
            ref.storage_key,
            "present",
        ):
            raise TychoraError("INTEGRITY_ERROR", "Retained query artifact differs.")
        if row is None:
            self._db.execute(
                "INSERT INTO artifacts(artifact_sha,size_bytes,media_type,storage_key,created_at) VALUES (?,?,?,?,?)",
                (
                    ref.sha256,
                    ref.size_bytes,
                    ref.media_type,
                    ref.storage_key,
                    self._now().isoformat(),
                ),
            )
            self._db.commit()
        return ref

    def _retained_data(self, ref: ArtifactRef) -> dict[str, object]:
        if self._artifacts is None:
            raise TychoraError(
                "REPLAY_UNAVAILABLE", "Retained query storage is unavailable."
            )
        row = self._db.execute(
            "SELECT size_bytes,media_type,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
            (ref.sha256,),
        ).fetchone()
        if row != (ref.size_bytes, ref.media_type, ref.storage_key, "present"):
            raise TychoraError(
                "REPLAY_UNAVAILABLE", "Retained query artifact is unavailable."
            )
        with self._artifacts.open_verified(ref) as stream:
            body = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(body) > MAX_MANIFEST_BYTES:
            raise TychoraError(
                "REPLAY_UNAVAILABLE", "Retained query exceeds local limit."
            )
        try:
            manifest = json.loads(body)
            if canonical_bytes(manifest) != body:
                raise ValueError("Noncanonical retained query")
            required_refs(manifest, capture_published=True)
            if (
                manifest.get("type") != "retained_run"
                or manifest.get("schema_version") != "1"
            ):
                raise ValueError("Incorrect retained query type")
            data = manifest["data"]
            if not isinstance(data, dict) or not isinstance(data.get("request"), dict):
                raise TypeError("Invalid retained query data")
        except (ValueError, KeyError, TypeError):
            raise TychoraError(
                "REPLAY_UNAVAILABLE", "Retained query manifest is invalid."
            ) from None
        return data

    def record(
        self,
        result: DeliveredResult,
        metrics: RunMetrics,
        retained_payload: ArtifactRef | None,
    ) -> None:
        """Persist a typed metadata projection; full local retention is opt-in."""
        if retained_payload is not None and not self._retain_payloads:
            raise TychoraError(
                "INVALID_REQUEST", "Full local query history is disabled."
            )
        query = result.result
        if retained_payload is not None:
            data = self._retained_data(retained_payload)
            if (
                data.get("run_id") != query.run_id
                or data.get("snapshot_id") != query.snapshot_id
                or data.get("config_sha") != metrics.config_sha
                or data.get("principal_id") != query.applied_scope.principal_id
                or data.get("scope_digest")
                != query_run_scope_digest(query.applied_scope)
            ):
                raise TychoraError("INTEGRITY_ERROR", "Retained query binding differs.")
        ids = {
            "segment_ids": [segment.segment_id for segment in query.segments],
            "unit_ids": sorted(
                {uid for segment in query.segments for uid in segment.matched_unit_ids}
            ),
            "asset_ids": [visual.asset_id for visual in query.visuals],
            "citation_count": len(query.citations),
            "decision_ids": [
                decision.requirement_id for decision in query.decision_requirements
            ],
        }
        current = self._now()
        self._db.execute(
            "INSERT INTO query_runs "
            "(run_id,snapshot_id,principal_id,scope_digest,policy_epoch,"
            "lexical_statistics_epoch,config_sha,state,metrics_json,result_ids_json,"
            "retained_payload_sha,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                query.run_id,
                query.snapshot_id,
                query.applied_scope.principal_id,
                query_run_scope_digest(query.applied_scope),
                metrics.policy_epoch,
                metrics.lexical_statistics_epoch,
                metrics.config_sha,
                "complete" if query.coverage.status == "complete" else "partial",
                canonical_bytes(metrics.model_dump(mode="json")).decode("utf-8"),
                canonical_bytes(ids).decode("utf-8"),
                retained_payload.sha256 if retained_payload is not None else None,
                current.isoformat(),
                (current + self._retention).isoformat(),
            ),
        )
        self._db.commit()

    def purge(self, expired_before: datetime) -> int:
        """Delete expired run metadata only; artifacts/provenance remain durable."""
        cursor = self._db.execute(
            "DELETE FROM query_runs WHERE expires_at<?", (expired_before.isoformat(),)
        )
        self._db.commit()
        return cursor.rowcount

    def supersede(self, run_id: str) -> None:
        """Mark an attempt invalidated by final policy revalidation."""
        self._db.execute(
            "UPDATE query_runs SET state='superseded' WHERE run_id=?", (run_id,)
        )
        self._db.commit()

    def replay_request(
        self,
        run_id: str,
        *,
        scope: RequestScope | None = None,
        snapshot_id: str | None = None,
        config_sha: str | None = None,
        projection_fingerprint: str | None = None,
        provider_fingerprint: str | None = None,
        providers_available: bool | Callable[[QueryRequest], bool] = False,
    ) -> QueryRequest:
        row = self._db.execute(
            "SELECT retained_payload_sha,scope_digest FROM query_runs "
            "WHERE run_id=? AND expires_at>=? AND state!='superseded'",
            (run_id, self._now().isoformat()),
        ).fetchone()
        if row is None or row[0] is None:
            raise TychoraError(
                "REPLAY_UNAVAILABLE",
                "This run retained only diagnostics; its request cannot be replayed.",
            )
        artifact = self._db.execute(
            "SELECT size_bytes,media_type,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
            (row[0],),
        ).fetchone()
        if artifact is None or artifact[3] != "present":
            raise TychoraError(
                "REPLAY_UNAVAILABLE", "Retained query artifact is unavailable."
            )
        data = self._retained_data(artifact_ref((row[0], *artifact[:3])))
        if (
            scope is None
            or snapshot_id is None
            or config_sha is None
            or projection_fingerprint is None
            or provider_fingerprint is None
            or data.get("run_id") != run_id
            or data.get("principal_id") != scope.principal_id
            or not query_run_scope_matches(row[1], scope)
            or data.get("scope_digest") != row[1]
            or data.get("snapshot_id") != snapshot_id
            or data.get("config_sha") != config_sha
            or data.get("projection_fingerprint") != projection_fingerprint
            or data.get("provider_fingerprint") != provider_fingerprint
        ):
            raise TychoraError(
                "REPLAY_UNAVAILABLE",
                "Current access, snapshot, projection, configuration or provider differs.",
            )
        request = QueryRequest.from_mapping(
            data["request"] if isinstance(data["request"], Mapping) else {}
        )
        available = (
            providers_available(request)
            if callable(providers_available)
            else providers_available
        )
        if not available:
            raise TychoraError(
                "REPLAY_UNAVAILABLE", "Current query capabilities are unavailable."
            )
        return request
