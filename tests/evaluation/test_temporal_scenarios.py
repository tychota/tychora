"""Declared editions and review state traverse public ingestion and query."""

from hashlib import sha256

import pytest
from httpx import ASGITransport, AsyncClient

from tests.evaluation.conftest import SyntheticCounter
from tests.helpers.timing import scaled
from tychora import Tychora
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.operations import MaintenanceRequest
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth
from tychora.ports.execution import ExecutionContext


def _source(text, *, source_key="dated.txt", **metadata):
    return {
        "type": "text",
        "text": text,
        "format": "text",
        "kind": "manual@1",
        "source_key": source_key,
        "collection_id": "default",
        "metadata": {
            "title": "Synthetic dated manual",
            "publisher": "Synthetic fixture",
            **metadata,
        },
    }


def _add(kb, value):
    job = kb.sources.add(value)
    status = kb.jobs.wait(job.id, timeout=scaled(10))
    assert status.state == "succeeded", (status.error_code, status.message)


def test_republishing_identical_older_edition_makes_it_default(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=SyntheticCounter()) as kb:
        first = _source("cobalt edition 1", edition="1", approval="accepted")
        second = _source("cobalt edition 2", edition="2", approval="accepted")
        _add(kb, first)
        owner = kb._workspace.owner
        original_snapshot = kb._workspace.bridge.call(
            owner.run_write(
                lambda: owner.store.resolve_snapshot(None), ExecutionContext()
            )
        )
        first_revision = original_snapshot.members[0].revision_id
        _add(kb, second)
        second_snapshot = kb._workspace.bridge.call(
            owner.run_write(
                lambda: owner.store.resolve_snapshot(None), ExecutionContext()
            )
        )
        second_revision = next(
            member.revision_id
            for member in second_snapshot.members
            if member.is_default
        )
        _add(kb, first)
        current, retained_first, retained_second = kb._workspace.bridge.call(
            owner.run_write(
                lambda: (
                    owner.store.resolve_snapshot(None),
                    owner.store.resolve_snapshot(original_snapshot.snapshot_id),
                    owner.store.resolve_snapshot(second_snapshot.snapshot_id),
                ),
                ExecutionContext(),
            )
        )
        assert {member.revision_id for member in current.members} == {
            first_revision,
            second_revision,
        }
        assert {
            member.revision_id for member in current.members if member.is_default
        } == {first_revision}
        assert {
            member.revision_id for member in retained_first.members if member.is_default
        } == {first_revision}
        assert {
            member.revision_id
            for member in retained_second.members
            if member.is_default
        } == {second_revision}
        assert current.families == second_snapshot.families
        query = {"question": "cobalt", "modalities": ["lexical"]}
        assert {segment.revision_id for segment in kb.search(query).segments} == {
            first_revision
        }
        assert {
            segment.revision_id
            for segment in kb.search({**query, "revision_selector": "all"}).segments
        } == {first_revision, second_revision}


def test_public_source_dates_editions_and_review_filter(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=SyntheticCounter()) as kb:
        _add(
            kb,
            _source(
                "cobalt old edition",
                edition="2022",
                publication_date="2022-01-01",
                effective_start="2022-01-01",
                effective_end="2024-01-01",
                approval="accepted",
            ),
        )
        owner = kb._workspace.owner
        old_snapshot = kb._workspace.bridge.call(
            owner.run_write(
                lambda: owner.store.resolve_snapshot(None),
                ExecutionContext(),
            )
        )
        _add(
            kb,
            _source(
                "cobalt new edition",
                edition="2024",
                publication_date="2024-01-01",
                effective_start="2024-01-01",
                approval="accepted",
            ),
        )
        rows = kb._workspace.bridge.call(
            owner.run_write(
                lambda: owner.db.execute(
                    "SELECT revision_id,edition_key,published_on,effective_from,effective_until,"
                    "declared_review_state FROM revisions ORDER BY edition_key"
                ).fetchall(),
                ExecutionContext(),
            )
        )
        assert [row[1:] for row in rows] == [
            ("2022", "2022-01-01", "2022-01-01", "2024-01-01", "accepted"),
            ("2024", "2024-01-01", "2024-01-01", None, "accepted"),
        ]
        old_id, new_id = rows[0][0], rows[1][0]
        old = kb.search(
            {
                "question": "cobalt",
                "modalities": ["lexical"],
                "as_of": "2023-01-01",
            }
        )
        current_snapshot, retained_old = kb._workspace.bridge.call(
            owner.run_write(
                lambda: (
                    owner.store.resolve_snapshot(None),
                    owner.store.resolve_snapshot(old_snapshot.snapshot_id),
                ),
                ExecutionContext(),
            )
        )
        assert [member.revision_id for member in retained_old.members] == [old_id]
        assert {member.revision_id for member in current_snapshot.members} == {
            old_id,
            new_id,
        }
        assert {
            member.revision_id
            for member in current_snapshot.members
            if member.is_default
        } == {new_id}
        assert any("old edition" in s.text for s in old.segments)
        assert all("new edition" not in s.text for s in old.segments)
        for request, expected in (
            ({}, {new_id}),
            ({"revision_selector": "all"}, {old_id, new_id}),
            ({"cutoff": "2023-01-01"}, {old_id}),
            ({"revision_ids": [old_id]}, {old_id}),
        ):
            result = kb.search(
                {
                    "question": "cobalt",
                    "modalities": ["lexical"],
                    **request,
                }
            )
            assert {segment.revision_id for segment in result.segments} == expected

        _add(
            kb,
            _source(
                "cobalt refreshed new edition",
                edition="2024",
                publication_date="2024-01-01",
                effective_start="2024-01-01",
                approval="accepted",
            ),
        )
        refreshed = kb.search(
            {
                "question": "cobalt",
                "modalities": ["lexical"],
                "revision_selector": "all",
            }
        )
        refreshed_ids = {segment.revision_id for segment in refreshed.segments}
        assert old_id in refreshed_ids and new_id not in refreshed_ids
        assert len(refreshed_ids) == 2
        _add(
            kb,
            _source(
                "opal unknown date",
                source_key="unknown.txt",
                edition="1",
                publication_date=None,
                approval="accepted",
            ),
        )
        assert not kb.search(
            {
                "question": "opal",
                "modalities": ["lexical"],
                "as_of": "2023-01-01",
            }
        ).segments
        assert kb.search(
            {
                "question": "opal",
                "modalities": ["lexical"],
                "as_of": "2023-01-01",
                "unknown_dates": "permissive",
            }
        ).segments
        _add(
            kb,
            _source(
                "garnet rejected evidence",
                source_key="rejected.txt",
                edition="1",
                approval="rejected",
            ),
        )
        assert not kb.search(
            {
                "question": "garnet",
                "modalities": ["lexical"],
            }
        ).segments

        def labels():
            return owner.db.execute(
                "SELECT s.source_key,r.edition_key,s.source_id,r.revision_id,u.unit_id "
                "FROM evidence_units u JOIN extractions e USING(extraction_id) "
                "JOIN revisions r USING(revision_id) JOIN sources s USING(source_id) "
                "ORDER BY s.source_key,r.edition_key"
            ).fetchall()

        labelled = kb._workspace.bridge.call(
            owner.run_write(
                labels,
                ExecutionContext(),
            )
        )
        by_key = {
            (key, edition): (source, revision, unit)
            for key, edition, source, revision, unit in labelled
        }
        old_source, old_revision, old_unit = by_key[("dated.txt", "2022")]
        _, current_revision, current_unit = next(
            (source, revision, unit)
            for key, edition, source, revision, unit in labelled
            if key == "dated.txt" and edition == "2024" and revision != new_id
        )
        unknown_source, unknown_revision, unknown_unit = by_key[("unknown.txt", "1")]
        rejected_source, rejected_revision, rejected_unit = by_key[
            ("rejected.txt", "1")
        ]

        def case(
            case_id,
            query,
            *,
            expected_sources=(),
            expected_revisions=(),
            expected_units=(),
            forbidden_sources=(),
            forbidden_revisions=(),
            forbidden_units=(),
        ):
            return {
                "case_id": case_id,
                "family_key": case_id,
                "split": "test",
                "benchmark": "integrity",
                "label_provenance": "declared synthetic source and retained canonical SQL identities",
                "query": {"question": "cobalt", "modalities": ["lexical"], **query},
                "expected_source_ids": expected_sources,
                "expected_revision_ids": expected_revisions,
                "expected_unit_ids": expected_units,
                "forbidden_source_ids": forbidden_sources,
                "forbidden_revision_ids": forbidden_revisions,
                "forbidden_unit_ids": forbidden_units,
            }

        cases = [
            case(
                "historic-cutoff",
                {"cutoff": "2023-01-01"},
                expected_sources=[old_source],
                expected_revisions=[old_revision],
                expected_units=[old_unit],
                forbidden_revisions=[current_revision],
                forbidden_units=[current_unit],
            ),
            case(
                "related-edition",
                {"revision_selector": "all"},
                expected_sources=[old_source],
                expected_revisions=[old_revision, current_revision],
                expected_units=[old_unit, current_unit],
            ),
            case(
                "unknown-review-unknown",
                {"question": "opal", "as_of": "2023-01-01"},
                forbidden_sources=[unknown_source],
                forbidden_revisions=[unknown_revision],
                forbidden_units=[unknown_unit],
            ),
            case(
                "unknown-review-rejected",
                {"question": "garnet"},
                forbidden_sources=[rejected_source],
                forbidden_revisions=[rejected_revision],
                forbidden_units=[rejected_unit],
            ),
        ]
        cases_bytes = b"".join(canonical_bytes(item) + b"\n" for item in cases)
        gold = tmp_path / "temporal-gold"
        gold.mkdir()
        (gold / "cases.jsonl").write_bytes(cases_bytes)
        (gold / "manifest.json").write_bytes(
            canonical_bytes(
                {
                    "format": "tychora-gold@1",
                    "dataset_id": "synthetic-temporal-v1",
                    "name": "Synthetic temporal and review mechanics",
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
                    "dataset_id": "synthetic-temporal-v1",
                    "split": "test",
                }
            )
        )
        assert completed.job.state == "succeeded"
        report = kb.evaluations.get(completed.job.id)
        assert set(report["cases"]) == {item["case_id"] for item in cases}
        for case_id, entry in report["cases"].items():
            scored = {
                metric["name"]: metric
                for metric in entry["runs"][0]["metrics"]["metrics"]
            }
            if case_id.startswith("unknown-review"):
                assert not entry["runs"][0]["result"]["segments"]
                assert scored["forbidden_leakage"]["value"] is None
            elif case_id == "historic-cutoff":
                assert scored["forbidden_leakage"]["value"] == 0.0
            assert scored["locator_validity"]["value"] in (None, 1.0)


@pytest.mark.asyncio
async def test_http_source_metadata_reaches_revision_fields(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=SyntheticCounter()) as kb:
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"owner": owner.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner"},
        ) as client:
            accepted = await client.post(
                "/v1/sources",
                json=_source(
                    "cobalt via HTTP",
                    edition="2025",
                    publication_date="2025-02-01",
                    effective_start="2025-02-01",
                    approval="accepted",
                ),
            )
            assert accepted.status_code == 202, accepted.text
            completed = await kb._workspace.services.jobs.wait(
                accepted.json()["id"], ExecutionContext(), timeout=scaled(10)
            )
            assert completed.state == "succeeded"
        rows = await owner.run_write(
            lambda: owner.db.execute(
                "SELECT edition_key,published_on,effective_from,declared_review_state "
                "FROM revisions"
            ).fetchall(),
            ExecutionContext(),
        )
        assert rows == [("2025", "2025-02-01", "2025-02-01", "accepted")]


def test_legacy_revision_projection_conflict_does_not_rewrite_history(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=SyntheticCounter()) as kb:
        source = _source(
            "cobalt immutable edition",
            edition="2025",
            publication_date="2025-01-01",
            effective_start="2025-01-01",
            approval="accepted",
        )
        _add(kb, source)
        owner = kb._workspace.owner

        def simulate_old_projection():
            owner.db.execute(
                "UPDATE revisions SET published_on=NULL,effective_from=NULL,"
                "declared_review_state='unreviewed'"
            )
            return owner.db.execute("SELECT revision_id FROM revisions").fetchone()[0]

        revision_id = kb._workspace.bridge.call(
            owner.run_write(
                simulate_old_projection,
                ExecutionContext(),
            )
        )
        job = kb.sources.add(source)
        status = kb.jobs.wait(job.id, timeout=scaled(10))
        assert status.state == "failed"
        assert status.error_code == "IDEMPOTENCY_CONFLICT"
        retained = kb._workspace.bridge.call(
            owner.run_write(
                lambda: owner.db.execute(
                    "SELECT revision_id,published_on,effective_from,declared_review_state "
                    "FROM revisions"
                ).fetchone(),
                ExecutionContext(),
            )
        )
        assert retained == (revision_id, None, None, "unreviewed")


def test_invalid_half_open_interval_is_rejected_before_publication(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=SyntheticCounter()) as kb:
        job = kb.sources.add(
            _source(
                "cobalt invalid dates",
                edition="2025",
                effective_start="2025-01-01",
                effective_end="2025-01-01",
            )
        )
        status = kb.jobs.wait(job.id, timeout=scaled(10))
        assert status.state == "failed"
        assert status.error_code == "INVALID_REQUEST"
        owner = kb._workspace.owner
        assert (
            kb._workspace.bridge.call(
                owner.run_write(
                    lambda: owner.db.execute(
                        "SELECT count(*) FROM revisions"
                    ).fetchone()[0],
                    ExecutionContext(),
                )
            )
            == 0
        )
