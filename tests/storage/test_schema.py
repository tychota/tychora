import sqlite3

import pytest

from tests.helpers.corpus import reader, registry, seed_bundle
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.contracts.errors import TychoraError


def test_canonical_schema_integrity(tmp_path):
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    names = {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "snapshot_batches",
        "asset_proxy_dependencies",
        "original_payload_tombstones",
    } <= names
    assert len({n for n in names if not n.startswith("lexical_fts")}) == 39
    assert "lexical_fts" in names
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_open_missing_does_not_create(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(TychoraError, match="does not exist"):
        open_corpus(path)
    assert not path.exists()


@pytest.mark.parametrize("tamper", ["checksum", "version", "table", "unmanaged"])
def test_incompatible_open_never_migrates(tmp_path, tamper):
    path = tmp_path / "corpus.sqlite3"
    db = open_corpus(path, initialize=True)
    if tamper == "checksum":
        db.execute("UPDATE schema_migrations SET checksum='wrong'")
    elif tamper == "version":
        db.execute("UPDATE schema_migrations SET version=1")
    elif tamper == "table":
        db.execute("DROP TABLE evaluation_results")
    else:
        db.execute("DROP TABLE schema_migrations")
    before = db.execute(
        "SELECT type,name,sql FROM sqlite_master ORDER BY name"
    ).fetchall()
    db.close()
    with pytest.raises(TychoraError) as error:
        open_corpus(path, initialize=True)
    assert error.value.code == "PROFILE_INCOMPATIBLE"
    if tamper in ("checksum", "version"):
        assert error.value.reason == "workspace.format_old"
    with sqlite3.connect(path) as check:
        assert (
            check.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY name"
            ).fetchall()
            == before
        )


def test_connection_pragmas_and_reopen(tmp_path):
    path = tmp_path / "corpus.sqlite3"
    open_corpus(path, initialize=True).close()
    db = open_corpus(path)
    assert [
        db.execute("PRAGMA " + name).fetchone()[0]
        for name in ("journal_mode", "synchronous", "busy_timeout", "foreign_keys")
    ] == ["wal", 2, 2000, 1]
    assert not db.in_transaction


def insert(db, table, **row):
    db.execute(
        f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
        tuple(row.values()),
    )


@pytest.fixture
def populated(tmp_path):
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    store = CorpusStore(db, registry=registry(), artifacts=reader())
    a, b = seed_bundle(), seed_bundle("Other evidence.", source_key="other.md")
    store.commit_extraction(a)
    store.commit_extraction(b)
    sha = a.extraction.document_artifact_sha
    for fid in ("family-a", "family-b"):
        insert(
            db,
            "index_families",
            family_id=fid,
            channel="lexical",
            backend="sqlite",
            config_sha=sha,
            config_json="{}",
            backend_namespace=fid,
            created_at="now",
        )
    for bid, fid, bundle in [
        ("batch-a", "family-a", a),
        ("batch-b", "family-a", b),
        ("batch-c", "family-b", a),
    ]:
        insert(
            db,
            "index_batches",
            batch_id=bid,
            family_id=fid,
            extraction_id=bundle.extraction.extraction_id,
            input_manifest_sha=sha,
            expected_count=1,
            actual_count=1,
            state="ready",
        )
    insert(
        db,
        "snapshots",
        snapshot_id="snapshot",
        workspace_id=a.workspace.workspace_id,
        membership_sha=sha,
        config_sha=sha,
        manifest_sha=sha,
        published_at="now",
    )
    insert(
        db,
        "snapshot_members",
        snapshot_id="snapshot",
        source_id=a.source.source_id,
        revision_id=a.revision.revision_id,
        extraction_id=a.extraction.extraction_id,
        is_default=1,
    )
    insert(
        db,
        "snapshot_indexes",
        snapshot_id="snapshot",
        slot="lexical",
        family_id="family-a",
        required=1,
    )
    insert(
        db,
        "snapshot_batches",
        snapshot_id="snapshot",
        slot="lexical",
        family_id="family-a",
        batch_id="batch-a",
    )
    insert(
        db,
        "jobs",
        job_id="job",
        operation="remove",
        principal_id="owner",
        idempotency_key="key",
        request_digest=sha,
        effective_config_sha=sha,
        state="complete",
        created_at="now",
        updated_at="now",
    )
    insert(
        db,
        "assets",
        asset_id="asset",
        extraction_id=a.extraction.extraction_id,
        artifact_sha=sha,
        asset_type="figure",
        native_ref="figure:1",
        locator_json="{}",
        declared_review_state="unreviewed",
    )
    insert(
        db,
        "asset_proxies",
        proxy_unit_id=a.units[0].unit_id,
        asset_id="asset",
        origin_type="ocr",
        derivation_sha=sha,
    )
    insert(
        db,
        "facet_definitions",
        facet_key="test.text",
        value_type="text",
        operators_json='["eq"]',
        definition_hash=sha,
    )
    insert(
        db,
        "original_payload_tombstones",
        revision_id=a.revision.revision_id,
        artifact_sha=sha,
        removal_job_id="job",
        removed_at="now",
    )
    insert(
        db,
        "lexical_records",
        rowid=1,
        batch_id="batch-a",
        chunk_id=a.chunks[0].chunk_id,
        identifiers="",
        title="",
        headings="",
        body="record result",
        context="record context",
    )
    yield db, a, b
    db.close()


SQL_NEGATIVES = [
    "batch_family",
    "member_source",
    "member_extraction",
    "parent_cross_extraction",
    "chunk_extraction",
    "chunk_start",
    "chunk_end",
    "unit_json",
    "review_none",
    "review_both",
    "vector_none",
    "vector_both",
    "proxy_self",
    "proxy_missing",
    "facet_scope",
    "facet_targets",
    "facet_values",
    "deleted_without_time",
    "present_with_time",
    "job_digest",
    "job_config",
    "job_key",
    "tombstone_duplicate",
    "tombstone_job",
    "family_extraction",
    "snapshot_wrong_family",
    "snapshot_default_source",
    "revision_interval",
    "strict_type",
    "lexical_update",
]


@pytest.mark.parametrize("case", SQL_NEGATIVES)
def test_canonical_sql_negative_constraints(populated, case):
    db, a, b = populated
    uid, rid, eid, sha = (
        a.units[0].unit_id,
        a.revision.revision_id,
        a.extraction.extraction_id,
        a.revision.original_sha,
    )
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError):
        if case == "batch_family":
            db.execute(
                "UPDATE index_batches SET family_id='missing' WHERE batch_id='batch-a'"
            )
        elif case == "member_source":
            db.execute("UPDATE snapshot_members SET source_id=?", (b.source.source_id,))
        elif case == "member_extraction":
            db.execute(
                "UPDATE snapshot_members SET extraction_id=?",
                (b.extraction.extraction_id,),
            )
        elif case == "parent_cross_extraction":
            db.execute(
                "UPDATE evidence_units SET parent_id=? WHERE unit_id=?",
                (b.units[0].unit_id, uid),
            )
        elif case == "chunk_extraction":
            db.execute(
                "UPDATE chunks SET extraction_id=? WHERE unit_id=?",
                (b.extraction.extraction_id, uid),
            )
        elif case == "chunk_start":
            db.execute("UPDATE chunks SET char_start=-1 WHERE unit_id=?", (uid,))
        elif case == "chunk_end":
            db.execute("UPDATE chunks SET char_end=char_start WHERE unit_id=?", (uid,))
        elif case == "unit_json":
            db.execute("UPDATE evidence_units SET attributes_json='bad-json'")
        elif case in ("review_none", "review_both"):
            insert(
                db,
                "review_events",
                revision_id=rid if case == "review_both" else None,
                unit_id=uid if case == "review_both" else None,
                state="accepted",
                actor_id="owner",
                created_at="now",
            )
        elif case in ("vector_none", "vector_both"):
            insert(
                db,
                "vector_records",
                record_id="vector",
                batch_id="batch-a",
                input_sha=sha,
                chunk_id=a.chunks[0].chunk_id if case == "vector_both" else None,
                asset_id="asset" if case == "vector_both" else None,
            )
        elif case in ("proxy_self", "proxy_missing"):
            insert(
                db,
                "asset_proxy_dependencies",
                proxy_unit_id=uid,
                required_unit_id=uid if case == "proxy_self" else "missing",
            )
        elif case == "facet_scope":
            db.execute("UPDATE facet_definitions SET filter_scope='invented'")
        elif case == "facet_targets":
            insert(
                db,
                "facet_values",
                revision_id=rid,
                unit_id=uid,
                facet_key="test.text",
                text_value="x",
            )
        elif case == "facet_values":
            insert(
                db,
                "facet_values",
                unit_id=uid,
                facet_key="test.text",
                text_value="x",
                number_value=1,
            )
        elif case == "deleted_without_time":
            db.execute(
                "UPDATE artifacts SET payload_state='deleted' WHERE artifact_sha=?",
                (sha,),
            )
        elif case == "present_with_time":
            db.execute(
                "UPDATE artifacts SET payload_deleted_at='now' WHERE artifact_sha=?",
                (sha,),
            )
        elif case == "job_digest":
            db.execute("UPDATE jobs SET request_digest='invalid'")
        elif case == "job_config":
            db.execute("UPDATE jobs SET effective_config_sha=NULL")
        elif case == "job_key":
            insert(
                db,
                "jobs",
                job_id="duplicate",
                operation="remove",
                principal_id="owner",
                idempotency_key="key",
                request_digest=sha,
                effective_config_sha=sha,
                state="queued",
                created_at="now",
                updated_at="now",
            )
        elif case == "tombstone_duplicate":
            insert(
                db,
                "original_payload_tombstones",
                revision_id=rid,
                artifact_sha=a.extraction.document_artifact_sha,
                removal_job_id="job",
                removed_at="later",
            )
        elif case == "tombstone_job":
            insert(
                db,
                "original_payload_tombstones",
                revision_id=rid,
                artifact_sha=sha,
                removal_job_id="missing",
                removed_at="now",
            )
        elif case == "family_extraction":
            insert(
                db,
                "index_batches",
                batch_id="duplicate",
                family_id="family-a",
                extraction_id=eid,
                input_manifest_sha=sha,
                expected_count=1,
                state="pending",
            )
        elif case == "snapshot_wrong_family":
            insert(
                db,
                "snapshot_batches",
                snapshot_id="snapshot",
                slot="lexical",
                family_id="family-a",
                batch_id="batch-c",
            )
        elif case == "snapshot_default_source":
            # A second valid revision of the same source is allowed, but not two defaults.
            db.execute(
                "UPDATE revisions SET source_id=? WHERE revision_id=?",
                (a.source.source_id, b.revision.revision_id),
            )
            insert(
                db,
                "snapshot_members",
                snapshot_id="snapshot",
                source_id=a.source.source_id,
                revision_id=b.revision.revision_id,
                extraction_id=b.extraction.extraction_id,
                is_default=1,
            )
        elif case == "revision_interval":
            db.execute(
                "UPDATE revisions SET effective_from='2024-02-02',effective_until='2024-02-01'"
            )
        elif case == "strict_type":
            db.execute("UPDATE evidence_units SET ordinal='not-an-integer'")
        elif case == "lexical_update":
            db.execute("UPDATE lexical_records SET body='changed' WHERE rowid=1")
        db.commit()  # Includes the deferred composite parent FK.
    db.rollback()
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not db.in_transaction


def test_fts_membership_grouping_and_transactional_statistics(populated):
    db, a, b = populated
    uid = a.units[0].unit_id
    insert(
        db,
        "chunks",
        chunk_id="extra-chunk",
        unit_id=uid,
        extraction_id=a.extraction.extraction_id,
        ordinal_in_unit=1,
        char_start=1,
        char_end=4,
        context_group_key="same",
        chunker_fingerprint="other",
        token_count=1,
    )
    for rowid, bid, cid in [
        (2, "batch-a", "extra-chunk"),
        (3, "batch-b", b.chunks[0].chunk_id),
    ]:
        insert(
            db,
            "lexical_records",
            rowid=rowid,
            batch_id=bid,
            chunk_id=cid,
            identifiers="",
            title="",
            headings="",
            body="record",
            context="",
        )
    assert (
        db.execute("SELECT lexical_statistics_epoch FROM workspaces").fetchone()[0] == 3
    )
    found = db.execute(
        "SELECT DISTINCT c.unit_id FROM lexical_fts f JOIN lexical_records l ON l.rowid=f.rowid "
        "JOIN chunks c USING(chunk_id) JOIN snapshot_batches s USING(batch_id) "
        "WHERE lexical_fts MATCH ? AND s.snapshot_id=?",
        ("record", "snapshot"),
    ).fetchall()
    assert found == [(uid,)]
    db.execute("BEGIN IMMEDIATE")
    db.execute("DELETE FROM lexical_records WHERE rowid=1")
    assert (
        db.execute("SELECT lexical_statistics_epoch FROM workspaces").fetchone()[0] == 4
    )
    db.rollback()
    assert (
        db.execute("SELECT lexical_statistics_epoch FROM workspaces").fetchone()[0] == 3
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM lexical_fts WHERE lexical_fts MATCH 'record'"
        ).fetchone()[0]
        == 3
    )
    db.execute("DELETE FROM lexical_records WHERE rowid=1")
    assert (
        db.execute("SELECT lexical_statistics_epoch FROM workspaces").fetchone()[0] == 4
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM lexical_fts WHERE lexical_fts MATCH 'record'"
        ).fetchone()[0]
        == 2
    )
