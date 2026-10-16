"""A declared per-facet global source remains subject to ordinary policy."""

from hashlib import sha256

import pytest

from tests.evaluation.conftest import SyntheticCounter
from tests.helpers.timing import scaled
from tychora import Tychora
from tychora.application.backup import BackupService
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.operations import MaintenanceRequest
from tychora.ports.execution import ExecutionContext


def _source(key, text, scope, **extra):
    metadata = {
        "title": key,
        "publisher": "Synthetic fixture",
        "edition": "1",
        "approval": "accepted",
        "publication_date": "2020-01-01",
        "effective_start": "2020-01-01",
        **extra,
    }
    if scope is not None:
        metadata["scope"] = scope
    return {
        "type": "text",
        "source_key": key,
        "collection_id": "default",
        "kind": "manual@1",
        "format": "text",
        "text": text,
        "metadata": metadata,
    }


def test_public_global_facet_requires_explicit_include_and_binding(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=SyntheticCounter()) as kb:
        for source in (
            _source("local.txt", "cobalt local project", {"project": "alpha"}),
            _source(
                "global.txt",
                "cobalt global reference",
                {
                    "global_facets": ["project"],
                },
            ),
            _source("missing.txt", "cobalt undeclared missing", None),
            _source("other.txt", "cobalt other project", {"project": "beta"}),
            _source(
                "rejected.txt",
                "cobalt rejected global",
                {
                    "global_facets": ["project"],
                },
                approval="rejected",
            ),
            _source(
                "future.txt",
                "cobalt future global",
                {
                    "global_facets": ["project"],
                },
                effective_start="2035-01-01",
            ),
        ):
            job = kb.sources.add(source)
            status = kb.jobs.wait(job.id, timeout=scaled(10))
            assert status.state == "succeeded", status
        base = {
            "question": "cobalt",
            "modalities": ["lexical"],
            "as_of": "2024-01-01",
            "filters": {"op": "eq", "field": "scope.project", "value": "alpha"},
        }
        local = kb.search(base)
        global_result = kb.search({**base, "include_global": True})
        assert {s.text for s in local.segments} == {"cobalt local project"}
        assert {s.text for s in global_result.segments} == {
            "cobalt local project",
            "cobalt global reference",
        }
        assert {
            s.text
            for s in kb.search(
                {
                    **base,
                    "include_global": True,
                    "as_of": "2024-01-01",
                }
            ).segments
        } == {"cobalt local project", "cobalt global reference"}
        owner = kb._workspace.owner
        rows = kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: owner.db.execute(
                    "SELECT s.source_key,r.metadata_json,ra.role,ra.artifact_sha "
                    "FROM sources s JOIN revisions r USING(source_id) "
                    "LEFT JOIN revision_artifacts ra ON ra.revision_id=r.revision_id "
                    "AND ra.role='global_facet_binding' ORDER BY s.source_key"
                ).fetchall(),
                ExecutionContext(),
            )
        )
        assert len(rows) == 6
        global_row = next(row for row in rows if row[0] == "global.txt")
        assert '"scope.project"' in global_row[1]
        assert global_row[2] == "global_facet_binding"
        assert len(global_row[3]) == 64
        local_id = kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: owner.db.execute(
                    "SELECT revision_id FROM revisions r JOIN sources s USING(source_id) "
                    "WHERE s.source_key='local.txt'"
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        assert {
            s.text
            for s in kb.search(
                {
                    **base,
                    "include_global": True,
                    "revision_ids": [local_id],
                }
            ).segments
        } == {"cobalt local project"}
        narrowed = kb.scope.model_copy(update={"collection_ids": frozenset()})
        assert not kb.search({**base, "include_global": True}, scope=narrowed).segments
        labels = kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: owner.db.execute(
                    "SELECT s.source_key,s.source_id,r.revision_id,u.unit_id "
                    "FROM evidence_units u JOIN extractions e USING(extraction_id) "
                    "JOIN revisions r USING(revision_id) JOIN sources s USING(source_id) "
                    "ORDER BY s.source_key"
                ).fetchall(),
                ExecutionContext(),
            )
        )
        assert len(labels) == 6 and len({row[0] for row in labels}) == 6
        by_key = {row[0]: row[1:] for row in labels}
        excluded = ["missing.txt", "other.txt", "rejected.txt", "future.txt"]
        cases = []
        for case_id, include, expected, forbidden in (
            ("mode-global-local", False, ["local.txt"], ["global.txt", *excluded]),
            ("mode-global-enabled", True, ["local.txt", "global.txt"], excluded),
        ):
            cases.append(
                {
                    "case_id": case_id,
                    "family_key": case_id,
                    "split": "test",
                    "benchmark": "integrity",
                    "label_provenance": "declared synthetic source scopes and canonical SQL identities",
                    "query": {**base, "include_global": include},
                    "expected_source_ids": [by_key[key][0] for key in expected],
                    "expected_revision_ids": [by_key[key][1] for key in expected],
                    "expected_unit_ids": [by_key[key][2] for key in expected],
                    "forbidden_source_ids": [by_key[key][0] for key in forbidden],
                    "forbidden_revision_ids": [by_key[key][1] for key in forbidden],
                    "forbidden_unit_ids": [by_key[key][2] for key in forbidden],
                }
            )
        cases_bytes = b"".join(canonical_bytes(case) + b"\n" for case in cases)
        gold = tmp_path / "global-gold"
        gold.mkdir()
        (gold / "cases.jsonl").write_bytes(cases_bytes)
        (gold / "manifest.json").write_bytes(
            canonical_bytes(
                {
                    "format": "tychora-gold@1",
                    "dataset_id": "synthetic-global-v1",
                    "name": "Synthetic per-facet global mechanics",
                    "version_sha": sha256(cases_bytes).hexdigest(),
                    "case_count": len(cases),
                }
            )
        )
        kb.evaluations.register_dataset(gold)
        completed = kb.management.run(
            MaintenanceRequest.from_mapping(
                {
                    "operation": "evaluate",
                    "dataset_id": "synthetic-global-v1",
                    "split": "test",
                }
            )
        )
        assert completed.job.state == "succeeded"
        report = kb.evaluations.get(completed.job.id)
        assert set(report["cases"]) == {case["case_id"] for case in cases}
        for entry in report["cases"].values():
            metrics = {
                metric["name"]: metric["value"]
                for metric in entry["runs"][0]["metrics"]["metrics"]
            }
            assert metrics["forbidden_leakage"] == 0.0
            assert metrics["locator_validity"] == 1.0

    with Tychora.open(tmp_path / "workspace", token_counter=SyntheticCounter()) as kb:
        assert {
            s.text
            for s in kb.search(
                {
                    **base,
                    "include_global": True,
                }
            ).segments
        } == {"cobalt local project", "cobalt global reference"}


def test_global_declaration_rejects_duplicates_and_non_global_facets(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=SyntheticCounter()) as kb:
        for names in (["project", "scope.project"], ["code.language"]):
            source = _source("invalid.txt", "cobalt", {"global_facets": names})
            job = kb.sources.add(source)
            status = kb.jobs.wait(job.id, timeout=scaled(10))
            assert status.state == "failed"
            assert status.error_code == "INVALID_REQUEST"


def test_global_binding_backup_restore_requires_exact_artifact(tmp_path):
    workspace = tmp_path / "workspace"
    archive = tmp_path / "backup"
    source = _source("global.txt", "cobalt global", {"global_facets": ["project"]})
    with Tychora.open(workspace, token_counter=SyntheticCounter()) as kb:
        job = kb.sources.add(source)
        assert kb.jobs.wait(job.id, timeout=scaled(10)).state == "succeeded"
        owner = kb._workspace.owner
        marker_sha = kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: owner.db.execute(
                    "SELECT artifact_sha FROM revision_artifacts "
                    "WHERE role='global_facet_binding'"
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        BackupService(owner).export(archive, kb.scope, ctx=ExecutionContext())
    query = {
        "question": "cobalt",
        "modalities": ["lexical"],
        "filters": {"op": "eq", "field": "scope.project", "value": "alpha"},
        "include_global": True,
    }
    with Tychora.restore(archive, tmp_path / "restored"):
        pass
    with Tychora.open(tmp_path / "restored", token_counter=SyntheticCounter()) as kb:
        assert {s.text for s in kb.search(query).segments} == {"cobalt global"}
    marker_path = archive / "artifacts" / marker_sha
    original = marker_path.read_bytes()
    marker_path.unlink()
    with pytest.raises(TychoraError) as missing:
        Tychora.restore(archive, tmp_path / "missing")
    assert missing.value.code == "INTEGRITY_ERROR"
    assert not (tmp_path / "missing").exists()
    marker_path.write_bytes(b"tampered" + original)
    with pytest.raises(TychoraError) as tampered:
        Tychora.restore(archive, tmp_path / "tampered")
    assert tampered.value.code == "INTEGRITY_ERROR"
    assert not (tmp_path / "tampered").exists()


def test_legacy_global_revision_without_binding_fails_closed(tmp_path):
    workspace = tmp_path / "workspace"
    source = _source("global.txt", "cobalt global", {"global_facets": ["project"]})
    with Tychora.open(workspace, token_counter=SyntheticCounter()) as kb:
        job = kb.sources.add(source)
        assert kb.jobs.wait(job.id, timeout=scaled(10)).state == "succeeded"
        owner = kb._workspace.owner

        def remove_binding():
            owner.db.execute(
                "DELETE FROM revision_artifacts WHERE role='global_facet_binding'"
            )
            owner.db.commit()

        kb._workspace.bridge.call(
            owner.run_blocking(
                remove_binding,
                ExecutionContext(),
            )
        )
    with Tychora.open(workspace, token_counter=SyntheticCounter()) as kb:
        with pytest.raises(TychoraError) as error:
            kb.search(
                {
                    "question": "cobalt",
                    "modalities": ["lexical"],
                    "filters": {"op": "eq", "field": "scope.project", "value": "alpha"},
                    "include_global": True,
                }
            )
        assert error.value.code == "PROFILE_INCOMPATIBLE"


def test_legacy_metadata_facet_cannot_be_silently_backfilled(tmp_path):
    source = _source("local.txt", "cobalt local", {"project": "alpha"})
    with Tychora.open(tmp_path / "workspace", token_counter=SyntheticCounter()) as kb:
        job = kb.sources.add(source)
        assert kb.jobs.wait(job.id, timeout=scaled(10)).state == "succeeded"
        owner = kb._workspace.owner

        def remove_facet():
            owner.db.execute("DELETE FROM facet_values WHERE facet_key='scope.project'")
            owner.db.commit()

        kb._workspace.bridge.call(
            owner.run_blocking(
                remove_facet,
                ExecutionContext(),
            )
        )
        retry = kb.sources.add(source)
        result = kb.jobs.wait(retry.id, timeout=scaled(10))
        assert result.state == "failed"
        assert result.error_code == "PROFILE_INCOMPATIBLE"
