"""Gold labels resolve to canonical IDs or fail the case with a reason."""

import pytest

from tests.helpers.legacy import OutputCounter, add_text
from tychora import Tychora
from tychora.adapters.sqlite.gold_labels import SqliteGoldLabelResolver
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
