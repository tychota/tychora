from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from tests.helpers.corpus import seed_bundle
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import chunk_id, digest
from tychora.contracts.indexing import SearchWindow
from tychora.indexing.groups import build_groups, representation_input_sha
from tychora.indexing.representations import prepare_extraction
from tychora.indexing.windows import window_unit


@dataclass(frozen=True)
class ExactCounter:
    model: str
    tokenizer_id: str = "synthetic-exact-counter@1"

    def count(self, text: str) -> int:
        return len(text)


def _window(
    name: str,
    text: str,
    *,
    ordinal: int,
    extraction: str = "extraction-a",
    revision: str = "revision-a",
    boundary: str = "section-a",
    dependencies: frozenset[str] | None = None,
    kind: str = "manual@1",
    unit_type: str = "paragraph",
    title: str = "Synthetic Manual",
    heading: tuple[str, ...] = ("Overview",),
    symbol: tuple[str, ...] = (),
    tokenizer_id: str = "synthetic-exact-counter@1",
) -> SearchWindow:
    unit = f"unit-{name}-{revision}"
    fingerprint = "a" * 64
    return SearchWindow(
        chunk_id=chunk_id(unit, 0, len(text), fingerprint),
        unit_id=unit,
        extraction_id=extraction,
        revision_id=revision,
        unit_ordinal=ordinal,
        unit_type=unit_type,
        char_start=0,
        char_end=len(text),
        text=text,
        token_count=len(text),
        tokenizer_id=tokenizer_id,
        chunker_fingerprint=fingerprint,
        boundary_key=boundary,
        context_group_key=digest("context-group-v1", extraction, boundary),
        dependency_ids=dependencies or frozenset({revision, unit}),
        title=title,
        kind_id=kind,
        heading_path=heading,
        symbol_path=symbol,
    )


def test_joint_group_orders_inputs_and_binds_complete_dependencies(
    config_factory,
) -> None:
    config = config_factory()
    counter = ExactCounter("voyage-context-4")
    first = _window(
        "first",
        "alpha",
        ordinal=0,
        dependencies=frozenset(
            {"revision-a", "unit-first-revision-a", "parent", "restricted-child"}
        ),
    )
    second = _window("second", "beta", ordinal=1)

    (group,) = build_groups((second, first), config, counter)

    assert group.window_ids == (first.chunk_id, second.chunk_id)
    assert group.input_texts == (group.capsule, "alpha", "beta")
    assert group.dependency_ids == frozenset(
        {
            "revision-a",
            "parent",
            "restricted-child",
            first.unit_id,
            second.unit_id,
        }
    )
    assert group.window_input_shas == (group.input_sha, group.input_sha)
    assert group.token_count == sum(counter.count(text) for text in group.input_texts)
    assert group.options == FrozenJsonMap(
        {
            "input_type": "document",
            "enable_auto_chunking": False,
            "truncation": False,
        }
    )


def test_window_and_group_contracts_reject_unbound_immutable_identities(
    config_factory,
) -> None:
    window = _window("one", "source", ordinal=0)
    with pytest.raises(ValidationError, match="chunk identity"):
        SearchWindow.model_validate({**window.model_dump(), "chunk_id": "wrong"})
    with pytest.raises(ValidationError, match="dependencies"):
        SearchWindow.model_validate(
            {
                **window.model_dump(),
                "dependency_ids": frozenset({window.revision_id}),
            }
        )

    group = build_groups((window,), config_factory(), ExactCounter("voyage-context-4"))[
        0
    ]
    with pytest.raises(ValidationError, match="input identity"):
        type(group).model_validate(
            {**group.model_dump(), "input_texts": (group.capsule, "changed")}
        )


def test_context_change_invalidates_every_joint_window(config_factory) -> None:
    config = config_factory()
    counter = ExactCounter("voyage-context-4")
    first = _window("first", "stable", ordinal=0)
    before = build_groups(
        (first, _window("second", "before", ordinal=1)), config, counter
    )[0]
    after = build_groups(
        (first, _window("second", "after!", ordinal=1)), config, counter
    )[0]

    assert before.input_sha != after.input_sha
    assert before.window_input_shas == (before.input_sha, before.input_sha)
    assert after.window_input_shas == (after.input_sha, after.input_sha)


def test_unchanged_code_input_reuses_content_hash_with_fresh_revision_dependencies(
    config_factory,
) -> None:
    config = config_factory(model="voyage-code-4")
    counter = ExactCounter("voyage-code-4")
    old = _window(
        "symbol",
        "def stable(): pass",
        ordinal=0,
        extraction="extraction-old",
        revision="revision-old",
        boundary="src/example.py",
        dependencies=frozenset(
            {"revision-old", "unit-symbol-revision-old", "owner-old"}
        ),
        kind="code_file@1",
        unit_type="symbol",
        heading=(),
        symbol=("src/example.py", "stable"),
    )
    new = _window(
        "symbol",
        "def stable(): pass",
        ordinal=0,
        extraction="extraction-new",
        revision="revision-new",
        boundary="src/example.py",
        dependencies=frozenset(
            {"revision-new", "unit-symbol-revision-new", "owner-new"}
        ),
        kind="code_file@1",
        unit_type="symbol",
        heading=(),
        symbol=("src/example.py", "stable"),
    )

    old_group = build_groups((old,), config, counter)[0]
    new_group = build_groups((new,), config, counter)[0]

    assert old_group.input_sha == new_group.input_sha
    assert old_group.group_id != new_group.group_id
    assert old_group.dependency_ids == frozenset(
        {"revision-old", "unit-symbol-revision-old", "owner-old"}
    )
    assert new_group.dependency_ids == frozenset(
        {"revision-new", "unit-symbol-revision-new", "owner-new"}
    )


def test_independent_code_hash_does_not_bind_sibling_window(config_factory) -> None:
    config = config_factory(model="voyage-code-4")
    counter = ExactCounter("voyage-code-4")
    first = _window(
        "first",
        "def first(): pass",
        ordinal=0,
        kind="code_file@1",
        unit_type="symbol",
        symbol=("src/example.py", "first"),
    )
    before = build_groups(
        (
            first,
            _window(
                "second",
                "def second(): return 1",
                ordinal=1,
                kind="code_file@1",
                unit_type="symbol",
                symbol=("src/example.py", "second"),
            ),
        ),
        config,
        counter,
    )
    after = build_groups(
        (
            first,
            _window(
                "second",
                "def second(): return 2",
                ordinal=1,
                kind="code_file@1",
                unit_type="symbol",
                symbol=("src/example.py", "second"),
            ),
        ),
        config,
        counter,
    )

    assert len(before) == len(after) == 2
    assert before[0].input_sha == after[0].input_sha
    assert before[1].input_sha != after[1].input_sha
    assert all(group.contextualization == "independent" for group in before)


def test_model_dimension_representation_and_options_change_input_identity(
    config_factory,
) -> None:
    inputs = ("capsule", "source")
    base = config_factory()
    model = config_factory(model="voyage-code-4")
    dimension = config_factory(dimensions=512)
    representation = config_factory(representation_version="2")

    values = {
        representation_input_sha(inputs, base, "joint", FrozenJsonMap()),
        representation_input_sha(inputs, model, "joint", FrozenJsonMap()),
        representation_input_sha(inputs, dimension, "joint", FrozenJsonMap()),
        representation_input_sha(inputs, representation, "joint", FrozenJsonMap()),
        representation_input_sha(
            inputs,
            base,
            "joint",
            FrozenJsonMap({"input_type": "document", "mode": "alternate"}),
        ),
    }

    assert len(values) == 5


def test_group_and_request_caps_split_only_between_windows(config_factory) -> None:
    config = config_factory(max_group_tokens=180, max_request_tokens=220)
    counter = ExactCounter("voyage-context-4")
    windows = tuple(
        _window(str(index), chr(97 + index) * 70, ordinal=index) for index in range(3)
    )

    groups = build_groups(windows, config, counter)

    assert [group.window_ids for group in groups] == [
        (windows[0].chunk_id,),
        (windows[1].chunk_id,),
        (windows[2].chunk_id,),
    ]
    assert [group.request_index for group in groups] == [0, 1, 2]
    assert all(group.token_count <= 180 for group in groups)


def test_split_joint_groups_use_only_subgroup_capsule_owners(config_factory) -> None:
    config = config_factory(max_group_tokens=260, max_request_tokens=1_000)
    counter = ExactCounter("voyage-context-4")
    windows = tuple(
        _window(
            str(index),
            chr(97 + index) * 120,
            ordinal=index,
            heading=(f"Restricted path {index}",),
            dependencies=frozenset(
                {"revision-a", f"unit-{index}-revision-a", f"owner-{index}"}
            ),
        )
        for index in range(3)
    )

    groups = build_groups(windows, config, counter)

    assert [group.window_ids for group in groups] == [
        (window.chunk_id,) for window in windows
    ]
    for index, group in enumerate(groups):
        assert f"Restricted path {index}" in group.capsule
        assert all(
            f"Restricted path {other}" not in group.capsule
            for other in range(3)
            if other != index
        )
        assert group.dependency_ids == windows[index].dependency_ids
        assert group.token_count == sum(
            counter.count(text) for text in group.input_texts
        )


def test_oversized_atomic_group_input_fails_explicitly(config_factory) -> None:
    config = config_factory(max_group_tokens=100, max_request_tokens=120)
    counter = ExactCounter("voyage-context-4")

    with pytest.raises(TychoraError, match="atomic.*token"):
        build_groups((_window("large", "x" * 90, ordinal=0),), config, counter)


def test_group_rejects_counter_for_different_provider_model(config_factory) -> None:
    with pytest.raises(ValueError, match="model"):
        build_groups(
            (_window("one", "source", ordinal=0),),
            config_factory(),
            ExactCounter("voyage-code-4"),
        )


def test_group_counts_capsule_and_windows_with_verified_provider_tokenizer(
    config_factory, voyage_counter
) -> None:
    bundle = seed_bundle("Cafe\u0301 🧬 source text.\r\nSecond line.")
    windows = window_unit(
        prepare_extraction(bundle).representation(bundle.units[0].unit_id),
        voyage_counter,
    )

    (group,) = build_groups(windows, config_factory(), voyage_counter)

    assert group.tokenizer_id == voyage_counter.tokenizer_id
    assert group.token_count == sum(
        voyage_counter.count(text) for text in group.input_texts
    )
