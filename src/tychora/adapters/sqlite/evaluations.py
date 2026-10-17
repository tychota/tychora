"""Canonical SQL and CAS bindings for versioned gold and evaluation runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO

from tychora.adapters.sqlite.repositories import SqliteWorkspaceRepository, artifact_ref
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import JsonValue, canonical_bytes, digest
from tychora.contracts.operations import EvaluationReport, EvaluationRequest
from tychora.contracts.requests import RequestScope
from tychora.evaluation.dataset import EvaluationDataset, GoldCase


class EvaluationRepository:
    def __init__(self, owner) -> None:
        self.owner = owner
        self.db = owner.db
        self.artifacts = owner.artifacts

    def _scope_sha(self, scope: RequestScope | None) -> str:
        grant = self.owner.scope.narrow(scope or self.owner.scope)
        return digest(
            "evaluation-dataset-scope-v1",
            self.owner.workspace_id,
            grant.principal_id,
            sorted(grant.collection_ids),
            grant.access_ceiling,
            sorted(grant.uses),
            sorted(grant.capabilities),
            sorted(grant.upload_providers),
        )

    def _put(self, payload: JsonValue) -> ArtifactRef:
        return self.artifacts.put_stream(
            BytesIO(canonical_bytes(payload)), "application/json"
        )

    def _bind(self, ref: ArtifactRef) -> None:
        row = self.db.execute(
            "SELECT size_bytes,media_type,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
            (ref.sha256,),
        ).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO artifacts (artifact_sha,size_bytes,media_type,storage_key,created_at) VALUES (?,?,?,?,?)",
                (
                    ref.sha256,
                    ref.size_bytes,
                    ref.media_type,
                    ref.storage_key,
                    datetime.now(UTC).isoformat(),
                ),
            )
        elif row != (ref.size_bytes, ref.media_type, ref.storage_key, "present"):
            raise TychoraError("INTEGRITY_ERROR", "Evaluation artifact binding differs")

    def _read(self, sha: str) -> dict:
        row = self.db.execute(
            "SELECT size_bytes,media_type,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
            (sha,),
        ).fetchone()
        if row is None or row[3] != "present":
            raise TychoraError("INTEGRITY_ERROR", "Evaluation artifact is unavailable")
        ref = artifact_ref((sha, *row[:3]))
        with self.artifacts.open_verified(ref) as stream:
            return json.load(stream)

    def register(
        self, dataset: EvaluationDataset, scope: RequestScope | None = None
    ) -> EvaluationDataset:
        current = self.db.execute(
            "SELECT version_sha FROM evaluation_datasets WHERE dataset_id=?",
            (dataset.dataset_id,),
        ).fetchone()
        if current is not None:
            if current[0] != dataset.version_sha:
                raise TychoraError(
                    "IDEMPOTENCY_CONFLICT", "Gold dataset ID has another version"
                )
            return self.load(dataset.dataset_id, scope=scope)
        refs = tuple(self._put(case.model_dump(mode="json")) for case in dataset.cases)
        manifest = self._put(
            {
                "type": "evaluation",
                "schema_version": "1",
                "required_artifacts": [ref.model_dump(mode="json") for ref in refs],
                "data": {
                    "dataset_id": dataset.dataset_id,
                    "name": dataset.name,
                    "version_sha": dataset.version_sha,
                    "cases": [case.case_id for case in dataset.cases],
                    "scope_sha": self._scope_sha(scope),
                },
            }
        )
        if self.db.in_transaction:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Evaluation registration requires a fresh transaction",
            )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for ref in (*refs, manifest):
                self._bind(ref)
            self.db.execute(
                "INSERT INTO evaluation_datasets VALUES (?,?,?,?,?)",
                (
                    dataset.dataset_id,
                    dataset.name,
                    dataset.version_sha,
                    manifest.sha256,
                    datetime.now(UTC).isoformat(),
                ),
            )
            for case, ref in zip(dataset.cases, refs, strict=True):
                self.db.execute(
                    "INSERT INTO evaluation_cases VALUES (?,?,?,?,?)",
                    (
                        dataset.dataset_id,
                        case.case_id,
                        case.split,
                        case.family_key,
                        ref.sha256,
                    ),
                )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return dataset

    def load(
        self, dataset_id: str, scope: RequestScope | None = None
    ) -> EvaluationDataset:
        row = self.db.execute(
            "SELECT name,version_sha,manifest_sha FROM evaluation_datasets WHERE dataset_id=?",
            (dataset_id,),
        ).fetchone()
        if row is None:
            raise TychoraError("NOT_FOUND", "Gold dataset is not registered")
        manifest = self._read(row[2])
        if (
            manifest.get("type") != "evaluation"
            or manifest.get("schema_version") != "1"
        ):
            raise TychoraError("INTEGRITY_ERROR", "Evaluation manifest type differs")
        data = manifest.get("data")
        if not isinstance(data, dict) or data.get("scope_sha") != self._scope_sha(
            scope
        ):
            raise TychoraError("FORBIDDEN_SCOPE", "Gold dataset is outside the grant")
        cases = tuple(
            GoldCase.model_validate_json(canonical_bytes(self._read(sha)))
            for (sha,) in self.db.execute(
                "SELECT payload_sha FROM evaluation_cases WHERE dataset_id=? ORDER BY rowid",
                (dataset_id,),
            )
        )
        dataset = EvaluationDataset(
            dataset_id=dataset_id, name=row[0], version_sha=row[1], cases=cases
        )
        if not isinstance(data, dict) or data != {
            "dataset_id": dataset.dataset_id,
            "name": dataset.name,
            "version_sha": dataset.version_sha,
            "cases": [case.case_id for case in cases],
            "scope_sha": self._scope_sha(scope),
        }:
            raise TychoraError(
                "INTEGRITY_ERROR", "Evaluation manifest differs from SQL"
            )
        return dataset

    def persist(
        self,
        *,
        evaluation_id: str,
        request: EvaluationRequest,
        job_id: str,
        policy_epoch: int,
        lexical_epoch: int,
        config_sha: str,
        hardware: dict,
        case_results: dict[str, list[dict]],
        report: EvaluationReport,
    ) -> None:
        """Final epoch check and valid-result commit share one owner callback."""
        workspace = SqliteWorkspaceRepository(self.db)
        current = (workspace.policy_epoch(), workspace.lexical_epoch())
        if current != (policy_epoch, lexical_epoch):
            raise TychoraError("SCOPE_CHANGED", "Evaluation comparison epochs changed")
        ref_by_case = {
            case_id: self._put(
                {"schema_version": "1", "case_id": case_id, "runs": runs}
            )
            for case_id, runs in case_results.items()
        }
        if self.db.in_transaction:
            raise TychoraError(
                "INTEGRITY_ERROR", "Evaluation persistence requires a fresh transaction"
            )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for ref in ref_by_case.values():
                self._bind(ref)
            self.db.execute(
                "INSERT INTO evaluation_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    evaluation_id,
                    request.dataset_id,
                    request.snapshot_id,
                    job_id,
                    policy_epoch,
                    lexical_epoch,
                    config_sha,
                    canonical_bytes(hardware).decode(),
                    canonical_bytes(report.model_dump(mode="json")).decode(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            for case_id, runs in case_results.items():
                self.db.execute(
                    "INSERT INTO evaluation_results VALUES (?,?,?,?)",
                    (
                        evaluation_id,
                        case_id,
                        ref_by_case[case_id].sha256,
                        canonical_bytes(
                            {
                                "runs": [
                                    {
                                        "variant": run["variant"],
                                        "repetition": run["repetition"],
                                        "metrics": run["metrics"],
                                    }
                                    for run in runs
                                ],
                            }
                        ).decode(),
                    ),
                )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get(self, evaluation_id: str, scope, jobs) -> dict:
        row = self.db.execute(
            "SELECT dataset_id,snapshot_id,job_id,policy_epoch,lexical_statistics_epoch,config_sha,hardware_json,aggregate_json "
            "FROM evaluation_runs WHERE evaluation_id=?",
            (evaluation_id,),
        ).fetchone()
        if row is None:
            raise TychoraError("NOT_FOUND", "Evaluation run does not exist")
        jobs.authorize(row[2], self.owner.scope.narrow(scope), write=True)
        epoch = SqliteWorkspaceRepository(self.db).policy_epoch()
        if epoch != row[3]:
            raise TychoraError(
                "SCOPE_CHANGED",
                "Evaluation evidence must be reauthorized after policy change",
            )
        return {
            "evaluation_id": evaluation_id,
            "dataset_id": row[0],
            "snapshot_id": row[1],
            "job_id": row[2],
            "policy_epoch": row[3],
            "lexical_statistics_epoch": row[4],
            "config_sha": row[5],
            "hardware": json.loads(row[6]),
            "report": json.loads(row[7]),
            "cases": {
                case_id: self._read(sha)
                for case_id, sha in self.db.execute(
                    "SELECT case_id,result_artifact_sha FROM evaluation_results WHERE evaluation_id=? ORDER BY case_id",
                    (evaluation_id,),
                )
            },
        }

    def list_runs(self, scope, jobs) -> list[dict]:
        rows = self.db.execute(
            "SELECT evaluation_id,dataset_id,snapshot_id,job_id,policy_epoch,created_at "
            "FROM evaluation_runs ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        current_epoch = SqliteWorkspaceRepository(self.db).policy_epoch()
        visible = []
        for evaluation_id, dataset_id, snapshot_id, job_id, epoch, created_at in rows:
            try:
                jobs.authorize(job_id, self.owner.scope.narrow(scope), write=True)
            except TychoraError:
                continue
            visible.append(
                {
                    "evaluation_id": evaluation_id,
                    "dataset_id": dataset_id,
                    "snapshot_id": snapshot_id,
                    "created_at": created_at,
                    "available": epoch == current_epoch,
                }
            )
        return visible
