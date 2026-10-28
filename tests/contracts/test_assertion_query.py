"""One assertion inspection request travels as a validated contract."""

from typing import Any

import pytest
from pydantic import ValidationError

from tychora.contracts.graph import AssertionQuery


def test_query_defaults_to_policy_directions_and_every_view():
    query = AssertionQuery(selected_unit_ids=("a",), predicates=("cites@1",))
    assert query.direction is None
    assert query.relation_view is None


@pytest.mark.parametrize(
    "changes",
    [
        {"selected_unit_ids": ("",)},
        {"predicates": ("",)},
        {"selected_unit_ids": ["a"]},
        {"direction": "sideways"},
        {"relation_view": "lexical"},
    ],
)
def test_query_rejects_malformed_fields(changes):
    values: dict[str, Any] = {
        "selected_unit_ids": ("a",),
        "predicates": ("cites@1",),
        **changes,
    }
    with pytest.raises(ValidationError):
        AssertionQuery.model_validate(values)
