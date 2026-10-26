"""Every write to an eligibility input moves the policy epoch in its transaction."""

import ast
import re
import sqlite3
from pathlib import Path

import pytest

from tests.helpers.corpus import artifact, reader, registry, seed_bundle
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.contracts.corpus import ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.indexing.representations import (
    ChunkSpan,
    prepare_extraction,
    representation_facts,
)
from tychora.ports.execution import ExecutionContext

SOURCE = Path(__file__).resolve().parents[2] / "src" / "tychora"
ELIGIBILITY_TABLES = frozenset(
    {
        "review_events",
        "review_current",
        "collections",
        "sources",
        "revisions",
        "facet_values",
        "assertion_decisions",
        "kinds",
        "evidence_units",
        "assets",
    }
)
_WRITE = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+\"?(\w+)",
    re.IGNORECASE,
)
BUMPS = "bumps policy_epoch in the same transaction"
NEW_ROWS = "inserts rows no published snapshot selects yet"
ELIGIBILITY_INPUT_WRITES = {
    "adapters/sqlite/policy.py:PolicyRepository.record_review": BUMPS,
    "adapters/sqlite/policy.py:PolicyRepository.raise_access_floor": BUMPS,
    "adapters/sqlite/policy.py:PolicyRepository.withdraw_source": BUMPS,
    "adapters/sqlite/policy.py:PolicyRepository.record_assertion_decision": BUMPS,
    "adapters/sqlite/policy.py:PolicyRepository.set_allowed_uses_override": BUMPS,
    "adapters/sqlite/workspace_inventory.py:_seed_one": BUMPS + " (seed_collections)",
    "adapters/sqlite/workspace_inventory.py:_restore_source": BUMPS
    + " (restore_policy)",
    "adapters/sqlite/workspace_inventory.py:_restore_revision": BUMPS
    + " (restore_policy)",
    "application/collections.py:CollectionService.create.create": BUMPS + " (_mutate)",
    "application/collections.py:CollectionService.edit.edit": BUMPS + " (_mutate)",
    "application/collections.py:CollectionService.set_archived.archive": BUMPS
    + " (_mutate)",
    "application/consent.py:apply_upload_consent": BUMPS,
    "adapters/sqlite/corpus.py:CorpusStore.commit_extraction": BUMPS
    + " when it registers a collection or source; "
    + NEW_ROWS,
    "adapters/sqlite/corpus.py:CorpusStore._insert_facets": NEW_ROWS,
    "application/owner.py:CanonicalUploadPolicy.register_captured_source": NEW_ROWS,
    "api.py:LocalWorkspace._ensure_default_collection": "creates the default collection of an empty workspace",
}


def _writers(path: Path) -> set[str]:
    """Qualified names of the functions in ``path`` that write an eligibility table."""
    found: set[str] = set()

    def visit(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, [*stack, child.name])
                continue
            texts: list[str] = []
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                texts.append(child.value)
            if isinstance(child, ast.JoinedStr):
                texts.append(
                    "".join(
                        str(v.value)
                        for v in child.values
                        if isinstance(v, ast.Constant)
                    )
                )
            if any(
                match.group(1).lower() in ELIGIBILITY_TABLES
                for text in texts
                for match in _WRITE.finditer(text)
            ):
                found.add(".".join(stack))
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in {"_insert", "_register"}
                and child.args
                and isinstance(child.args[0], ast.Constant)
                and child.args[0].value in ELIGIBILITY_TABLES
            ):
                found.add(".".join(stack))
            visit(child, stack)

    visit(ast.parse(path.read_text()), [])
    return found


def test_every_eligibility_write_is_listed():
    found = {
        f"{path.relative_to(SOURCE)}:{name}"
        for path in SOURCE.rglob("*.py")
        for name in _writers(path)
    }
    assert found == set(ELIGIBILITY_INPUT_WRITES)


def test_listing_guard_finds_a_new_writer(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "class Repo:\n    def lower(self, db):\n"
        "        db.execute('UPDATE sources SET access_floor=0')\n"
    )
    assert _writers(sample) == {"Repo.lower"}


def _facts(bundle):
    return representation_facts(
        bundle,
        prepare_extraction(bundle),
        (
            ChunkSpan(c.chunk_id, c.unit_id, c.char_start, c.char_end)
            for c in bundle.chunks
        ),
    )


@pytest.fixture
def store(tmp_path):
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    corpus = CorpusStore(db, registry=registry(), artifacts=reader())
    yield corpus
    db.close()


ASSERTION = (
    b'{"type":"assertion","schema_version":"1",'
    b'"record_id":"record-1","assertion_id":"assertion-1"}\n'
)


@pytest.fixture
def bundle(store):
    seeded = seed_bundle("Seal checks are quarterly.")
    assertions = artifact(ASSERTION, "application/x-ndjson")
    seeded = seeded.model_copy(
        update={
            "artifacts": (*seeded.artifacts, assertions),
            "extraction": seeded.extraction.model_copy(
                update={"assertions_artifact_sha": assertions.sha256}
            ),
        }
    )
    store.commit_extraction(seeded, _facts(seeded))
    return seeded


def _epoch(store):
    return store.db.execute("SELECT policy_epoch FROM workspaces").fetchone()[0]


@pytest.mark.parametrize(
    "write",
    [
        lambda s, b: s.record_review(
            ReviewTarget(unit_id=b.units[0].unit_id), "accepted", "r"
        ),
        lambda s, b: s.raise_access_floor(
            ReviewTarget(revision_id=b.revision.revision_id), 2
        ),
        lambda s, b: s.withdraw_source(b.source.source_id),
        lambda s, b: s.set_allowed_uses_override(b.source.source_id, "never_generate"),
        lambda s, b: s.set_allowed_uses_override(b.source.source_id, ("lookup",)),
        lambda s, b: s.set_allowed_uses_override(b.source.source_id, ["audit"]),
        lambda s, b: s.set_allowed_uses_override(b.source.source_id, None),
        lambda s, b: s.record_assertion_decision(
            "assertion-1",
            b.extraction.assertions_artifact_sha,
            "record-1",
            "rejected",
            "r",
        ),
    ],
)
def test_every_eligibility_write_bumps_policy_epoch(store, bundle, write):
    before = _epoch(store)
    write(store, bundle)
    assert _epoch(store) == before + 1


def test_a_failed_policy_write_moves_nothing(store, bundle):
    before = _epoch(store)
    with pytest.raises(TychoraError):
        store.set_allowed_uses_override(bundle.source.source_id, ("teleport",))
    assert _epoch(store) == before
    row = store.db.execute(
        "SELECT allowed_uses_override FROM sources WHERE source_id=?",
        (bundle.source.source_id,),
    ).fetchone()
    assert row == (None,)


def test_review_current_tracks_latest_event(store, bundle):
    target = ReviewTarget(unit_id=bundle.units[0].unit_id)
    store.record_review(target, "needs_review", "r")
    latest = store.record_review(target, "accepted", "r")
    row = store.db.execute(
        "SELECT state,review_event_id FROM review_current "
        "WHERE target_kind='unit' AND target_id=?",
        (bundle.units[0].unit_id,),
    ).fetchone()
    assert row == ("accepted", latest)
    assert store.review_state(target) == "accepted"
    revision = ReviewTarget(revision_id=bundle.revision.revision_id)
    store.record_review(revision, "rejected", "r")
    assert store.review_state(target) == "rejected"


def test_allowed_uses_override_only_narrows(store, bundle):
    source = bundle.source.source_id

    def stored():
        return store.db.execute(
            "SELECT allowed_uses_override FROM sources WHERE source_id=?", (source,)
        ).fetchone()[0]

    store.set_allowed_uses_override(source, "never_generate")
    assert stored() == '["audit","lookup"]'
    store.set_allowed_uses_override(source, "reference_only")
    assert stored() == '["lookup"]'
    store.set_allowed_uses_override(source, ("audit",))
    assert stored() == '["audit"]'
    store.set_allowed_uses_override(source, None)
    assert stored() is None
    with pytest.raises(TychoraError) as error:
        store.set_allowed_uses_override(source, ("teleport",))
    assert (error.value.code, error.value.reason) == (
        "INVALID_REQUEST",
        "policy.use_unknown",
    )
    with pytest.raises(TychoraError) as error:
        store.set_allowed_uses_override("missing", None)
    assert (error.value.code, error.value.reason) == ("NOT_FOUND", "source.absent")


def test_allowed_uses_override_accepts_a_list(store, bundle):
    source = bundle.source.source_id
    store.set_allowed_uses_override(source, ["lookup", "audit", "lookup"])
    assert store.db.execute(
        "SELECT allowed_uses_override FROM sources WHERE source_id=?", (source,)
    ).fetchone() == ('["audit","lookup"]',)


@pytest.mark.parametrize(
    "override",
    ["lookup", b"lookup", 3, {"lookup": True}, ("lookup", 1), frozenset({"lookup"})],
)
def test_allowed_uses_override_refuses_a_shape_that_is_not_a_use_list(
    store, bundle, override
):
    before = _epoch(store)
    with pytest.raises(TychoraError) as error:
        store.set_allowed_uses_override(bundle.source.source_id, override)
    assert error.value.code == "INVALID_REQUEST"
    assert error.value.reason is None
    assert "list of uses" in str(error.value)
    assert _epoch(store) == before


@pytest.mark.parametrize("override", [(), []])
def test_allowed_uses_override_refuses_an_empty_list(store, bundle, override):
    before = _epoch(store)
    with pytest.raises(TychoraError) as error:
        store.set_allowed_uses_override(bundle.source.source_id, override)
    assert (error.value.code, error.value.reason) == (
        "INVALID_REQUEST",
        "policy.use_override_empty",
    )
    assert "None" in str(error.value)
    assert "withdraw_source" in str(error.value)
    assert _epoch(store) == before


def test_allowed_uses_override_refuses_a_narrowing_that_keeps_nothing(
    store, bundle, monkeypatch
):
    monkeypatch.setattr(store, "_known_uses", lambda: frozenset({"compose", "audit"}))
    with pytest.raises(TychoraError) as error:
        store.set_allowed_uses_override(bundle.source.source_id, "reference_only")
    assert error.value.reason == "policy.use_override_empty"


@pytest.mark.parametrize("value", ['{"uses":["lookup"]}', '"lookup"', "not json"])
def test_the_override_column_holds_only_a_json_array(store, bundle, value):
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute(
            "UPDATE sources SET allowed_uses_override=? WHERE source_id=?",
            (value, bundle.source.source_id),
        )


def test_a_source_outside_the_granted_collections_reads_as_absent(store, bundle):
    before = _epoch(store)
    with pytest.raises(TychoraError) as error:
        store.set_allowed_uses_override(
            bundle.source.source_id, "never_generate", collection_ids=frozenset()
        )
    assert (error.value.code, error.value.reason) == ("NOT_FOUND", "source.absent")
    assert _epoch(store) == before
    store.set_allowed_uses_override(
        bundle.source.source_id,
        "never_generate",
        collection_ids=frozenset({bundle.source.collection_id}),
    )
    assert _epoch(store) == before + 1


def test_clients_narrow_a_source_with_manage_only(tmp_path):
    from tests.helpers.legacy import OutputCounter
    from tests.helpers.owner import owner_row
    from tychora import Tychora

    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": "a.md",
                "collection_id": "default",
                "kind": "document@1",
                "format": "markdown",
                "text": "# A\n\nInspect the seal.\n",
                "metadata": {"title": "A"},
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        owner = kb._workspace.owner
        row = owner_row(owner, "SELECT source_id FROM sources")
        assert row is not None
        (source_id,) = row
        kb.sources.set_allowed_uses(source_id, "reference_only")
        assert owner_row(owner, "SELECT allowed_uses_override FROM sources") == (
            '["lookup"]',
        )
        reader = kb.scope.model_copy(update={"capabilities": frozenset({"read"})})
        with pytest.raises(TychoraError) as error:
            kb._workspace.bridge.call(
                kb._workspace.services.source_jobs.sources.set_allowed_uses(
                    source_id, None, reader, ExecutionContext()
                )
            )
    assert (error.value.code, error.value.reason) == (
        "FORBIDDEN_SCOPE",
        "scope.capability_missing",
    )
