"""Repeated composition selectors reuse only validated results within one bundle."""

from types import SimpleNamespace

import pytest

from tychora.contracts.composition import SourceSelector
from tychora.ingestion import composition


def _unit(unit_id, native_ref, start, end, *, citable=True):
    return SimpleNamespace(
        unit_id=unit_id,
        text_origin="source",
        citable=citable,
        attributes={
            "source_refs": (native_ref,),
            "source_parts": (
                {
                    "native_ref": native_ref,
                    "field": "text",
                    "cell_index": None,
                    "source_start": start,
                    "source_end": end,
                },
            ),
        },
    )


def test_repeated_and_distinct_selectors_preserve_all_overlap_owners(monkeypatch):
    ref = "#/items/target"
    other = "#/items/other"
    parsed = SimpleNamespace(
        units=(
            SimpleNamespace(native_ref=ref, raw_item={"text": "abcdef"}),
            SimpleNamespace(native_ref=other, raw_item={"text": "other"}),
        )
    )
    bundle = SimpleNamespace(
        units=(
            *(
                _unit(f"noise-{index}", f"#/items/{index}", 0, 1)
                for index in range(1000)
            ),
            _unit("left", ref, 0, 3),
            _unit("right", ref, 2, 6),
            _unit("uncitable", ref, 2, 6, citable=False),
            _unit("other", other, 0, 5),
        )
    )
    repeated = SourceSelector(
        native_ref=ref, field="text", source_start=2, source_end=4
    )
    distinct = SourceSelector(
        native_ref=other, field="text", source_start=0, source_end=3
    )
    calls = 0
    original = composition._selected

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(composition, "_selected", counted)
    resolver = composition._SelectorResolver(parsed)
    assert resolver.selected(repeated, bundle) == ("left", "right")
    for _ in range(100):
        assert resolver.selected(repeated, bundle) == ("left", "right")
    assert resolver.selected(distinct, bundle) == ("other",)
    assert calls == 2
    changed = SimpleNamespace(units=bundle.units[:-1])
    assert resolver.selected(repeated, changed) == ("left", "right")
    assert calls == 3
    with pytest.raises(ValueError, match="original field interval"):
        resolver.selected(
            SourceSelector(native_ref=ref, field="text", source_start=6, source_end=7),
            bundle,
        )


def test_selector_memo_retains_at_most_256_entries():
    ref = "#/items/target"
    parsed = SimpleNamespace(
        units=(SimpleNamespace(native_ref=ref, raw_item={"text": "x" * 300}),)
    )
    bundle = SimpleNamespace(units=(_unit("all", ref, 0, 300),))
    resolver = composition._SelectorResolver(parsed)
    for start in range(260):
        assert resolver.selected(
            SourceSelector(
                native_ref=ref, field="text", source_start=start, source_end=start + 1
            ),
            bundle,
        ) == ("all",)
    assert len(resolver._cache) == 256
    assert resolver._retained_ids == 256
