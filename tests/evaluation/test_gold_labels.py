"""Gold labels resolve to canonical IDs or fail the case with a reason."""

import pytest

from tests.helpers.legacy import OutputCounter, add_text
from tychora import Tychora
from tychora.adapters.sqlite.gold_labels import SqliteGoldLabelResolver
from tychora.contracts.collections import CollectionCreate
from tychora.evaluation.dataset import (
    CharRangeAnchor,
    GoldCase,
    GoldLabel,
    GoldLabelError,
    NativeRefAnchor,
    PageAnchor,
    bind_cases,
    resolve_gold_binding,
    resolve_gold_label,
)
from tychora.ports.execution import ExecutionContext


class FakeResolver:
    def __init__(self, sources=("s1",), revisions=("r1",), units=("u1",)):
        self.sources, self.revisions, self.units = sources, revisions, units
        self.calls: list[tuple] = []

    def source_ids(self, collection, source_key):
        return self.sources

    def revision_ids(self, source_id, edition_key):
        return self.revisions

    def units_by_native_ref(self, revision_id, native_ref):
        self.calls.append(("native", native_ref))
        return self.units

    def units_by_char_range(self, revision_id, char_start, char_end):
        self.calls.append(("range", char_start, char_end))
        return self.units

    def units_by_page(self, revision_id, page):
        self.calls.append(("page", page))
        return self.units


def _label(anchor) -> GoldLabel:
    return GoldLabel(collection="default", source_key="guide", anchor=anchor)


def test_labels_parse_each_anchor_kind_from_json():
    for anchor, expected in (
        ({"native_ref": "block:3"}, NativeRefAnchor),
        ({"char_start": 4, "char_end": 9}, CharRangeAnchor),
        ({"page": 2}, PageAnchor),
    ):
        label = GoldLabel.from_mapping(
            {"collection": "default", "source_key": "guide", "anchor": anchor}
        )
        assert isinstance(label.anchor, expected)


def test_resolution_dispatches_on_the_anchor():
    resolver = FakeResolver()
    assert resolve_gold_label(
        _label(NativeRefAnchor(native_ref="block:3")), resolver
    ) == ("u1",)
    resolve_gold_label(_label(CharRangeAnchor(char_start=4, char_end=9)), resolver)
    resolve_gold_label(_label(PageAnchor(page=2)), resolver)
    assert resolver.calls == [("native", "block:3"), ("range", 4, 9), ("page", 2)]
    binding = resolve_gold_binding(_label(PageAnchor(page=2)), resolver)
    assert (binding.source_id, binding.revision_id) == ("s1", "r1")


@pytest.mark.parametrize(
    ("resolver", "reason"),
    [
        (FakeResolver(sources=()), "label.missing"),
        (FakeResolver(sources=("s1", "s2")), "label.ambiguous"),
        (FakeResolver(revisions=()), "label.missing"),
        (FakeResolver(revisions=("r1", "r2")), "label.ambiguous"),
        (FakeResolver(units=()), "label.missing"),
    ],
)
def test_unresolvable_labels_carry_a_reason(resolver, reason):
    with pytest.raises(GoldLabelError) as error:
        resolve_gold_label(_label(PageAnchor(page=1)), resolver)
    assert error.value.reason == reason


def _case(case_id: str, label: GoldLabel) -> GoldCase:
    return GoldCase.from_mapping(
        {
            "case_id": case_id,
            "family_key": case_id,
            "split": "test",
            "query": {"question": "guide", "modalities": ["lexical"]},
            "gold_labels": [label.model_dump(mode="json")],
            "forbidden_labels": [],
        }
    )


def test_bind_cases_fills_ids_and_reports_failures():
    good = _case("good", _label(PageAnchor(page=1)))
    bound, failures = bind_cases((good,), FakeResolver())
    assert bound[0].expected_unit_ids == frozenset({"u1"})
    assert bound[0].expected_source_ids == frozenset({"s1"})
    assert bound[0].expected_revision_ids == frozenset({"r1"})
    assert failures == ()
    bound, failures = bind_cases((good,), FakeResolver(sources=()))
    assert bound == ()
    assert [(item.case_id, item.reason) for item in failures] == [
        ("good", "label.missing")
    ]


TEXT = "Alpha beta zx00001q gamma.\n"


def test_sqlite_resolver_finds_the_marker_unit(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        snapshot = add_text(kb, "guide", TEXT).result_snapshot_id
        owner = kb._workspace.owner
        start = TEXT.index("zx00001q")
        label = _label(CharRangeAnchor(char_start=start, char_end=start + 8))

        def resolve():
            resolver = SqliteGoldLabelResolver(owner.db, snapshot)
            units = resolve_gold_label(label, resolver)
            text = owner.db.execute(
                "SELECT text FROM evidence_units WHERE unit_id=?", (units[0],)
            ).fetchone()[0]
            return units, text, resolver.source_ids("default", "absent")

        units, text, missing = kb._workspace.bridge.call(
            owner.run_blocking(resolve, ExecutionContext())
        )
    assert len(units) == 1
    assert "zx00001q" in text
    assert missing == ()


def _on_owner(kb, work):
    owner = kb._workspace.owner
    return kb._workspace.bridge.call(owner.run_blocking(work, ExecutionContext()))


def _add_edition(kb, key: str, text: str, edition: str) -> str:
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": "default",
            "kind": "document@1",
            "format": "text",
            "text": text,
            "metadata": {"title": key, "edition": edition},
        }
    )
    status = kb.jobs.wait(job.id)
    assert status.state == "succeeded", status.error_code
    return status.result_snapshot_id


def _unit_text(db, unit_id: str) -> str:
    return db.execute(
        "SELECT text FROM evidence_units WHERE unit_id=?", (unit_id,)
    ).fetchone()[0]


def test_sqlite_resolver_native_ref_and_page_anchors(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        snapshot = add_text(kb, "guide", TEXT).result_snapshot_id

        def work():
            db = kb._workspace.owner.db
            resolver = SqliteGoldLabelResolver(db, snapshot)
            row = db.execute(
                "SELECT unit_id, native_ref FROM evidence_units LIMIT 1"
            ).fetchone()
            by_ref = resolve_gold_label(
                _label(NativeRefAnchor(native_ref=row[1])), resolver
            )
            try:
                resolve_gold_label(_label(PageAnchor(page=1)), resolver)
            except GoldLabelError as error:
                by_page = error.reason
            try:
                resolve_gold_label(_label(NativeRefAnchor(native_ref="nope")), resolver)
            except GoldLabelError as error:
                missing = error.reason
            return row[0], by_ref, by_page, missing

        unit, by_ref, by_page, missing = _on_owner(kb, work)
    assert unit in by_ref
    assert by_page == "label.missing"
    assert missing == "label.missing"


def test_sqlite_resolver_picks_the_named_edition(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        add_text(kb, "guide", "First edition zx00001q.\n")
        snapshot = _add_edition(kb, "guide", "Second edition zx00002q.\n", "2")

        def work():
            db = kb._workspace.owner.db
            resolver = SqliteGoldLabelResolver(db, snapshot)
            anchor = CharRangeAnchor(char_start=0, char_end=5)
            texts = {}
            for edition in ("1", "2"):
                label = GoldLabel(
                    collection="default",
                    source_key="guide",
                    edition_key=edition,
                    anchor=anchor,
                )
                try:
                    units = resolve_gold_label(label, resolver)
                    texts[edition] = _unit_text(db, units[0])
                except GoldLabelError as error:
                    texts[edition] = error.reason
            return texts

        texts = _on_owner(kb, work)
    assert "zx00002q" in texts["2"]
    assert "zx00001q" in texts["1"]


def test_sqlite_resolver_reports_missing_and_ambiguous(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        add_text(kb, "guide", TEXT)
        default_id = str(kb._workspace.default_collection_id)
        twin = kb._workspace.bridge.call(
            kb._workspace.services.collections.create(
                CollectionCreate(title="Twin", slug=default_id),
                kb.scope,
                ExecutionContext(),
                managed_owner_authorized=True,
            )
        )
        twin_collection = str(twin.collection_id)
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": "guide",
                "collection_id": twin_collection,
                "kind": "document@1",
                "format": "text",
                "text": TEXT,
                "metadata": {"title": "guide"},
            }
        )
        snapshot = kb.jobs.wait(job.id).result_snapshot_id

        def work():
            resolver = SqliteGoldLabelResolver(kb._workspace.owner.db, snapshot)
            by_slug = resolver.source_ids("default", "guide")
            return {
                "by_slug": by_slug,
                "by_id": resolver.source_ids(default_id, "guide"),
                "unknown": resolver.source_ids("nowhere", "guide"),
                "absent_edition": resolver.revision_ids(by_slug[0], "9"),
            }

        found = _on_owner(kb, work)
    assert len(found["by_slug"]) == 1
    assert len(found["by_id"]) == 2
    assert found["unknown"] == ()
    assert found["absent_edition"] == ()


def test_sqlite_resolver_resolves_sources_withdrawn_after_the_snapshot(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        snapshot = add_text(kb, "guide", TEXT).result_snapshot_id

        def work():
            db = kb._workspace.owner.db
            db.execute("UPDATE sources SET withdrawn_at='2026-02-01T00:00:00+00:00'")
            resolver = SqliteGoldLabelResolver(db, snapshot)
            return resolver.source_ids("default", "guide")

        found = _on_owner(kb, work)
    assert len(found) == 1


def test_bind_case_forbidden_labels_forbid_their_units():
    case = GoldCase.from_mapping(
        {
            "case_id": "c",
            "family_key": "c",
            "split": "test",
            "query": {"question": "guide", "modalities": ["lexical"]},
            "gold_labels": [],
            "forbidden_labels": [_label(PageAnchor(page=1)).model_dump(mode="json")],
        }
    )
    bound, _ = bind_cases((case,), FakeResolver(units=("u9",)))
    assert bound[0].forbidden_unit_ids == frozenset({"u9"})
    assert bound[0].forbidden_source_ids == frozenset()
    assert bound[0].forbidden_revision_ids == frozenset()
