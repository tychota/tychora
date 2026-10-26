"""The revision-level view answers readability from a persisted dependency set."""

from typing import Any

import pytest

from tychora.contracts.eligibility import EligibilityView, RevisionGrant
from tychora.contracts.identity import digest


def _view(**changes):
    values: dict[str, Any] = {
        "snapshot_id": "snapshot",
        "policy_epoch": 3,
        "scope_digest": digest("scope"),
        "eligibility_policy_digest": digest("policy"),
        "eligibility_request_digest": digest("request"),
        "revisions": {
            "rev-a": RevisionGrant(
                extraction_id="ext-a",
                source_id="src-a",
                kind_id="manual@1",
                lane="standard",
                collection_id="col",
            )
        },
        "excluded_unit_ids": frozenset({"unit-x"}),
        "excluded_asset_ids": frozenset({"asset-x"}),
        "selected_batches": {"lexical": ("batch-a",), "text": ("batch-t",)},
    }
    return EligibilityView(**(values | changes))


@pytest.mark.parametrize(
    ("dependencies", "expected"),
    [
        ({"rev-a", "unit-1"}, True),
        ({"rev-a", "unit-1", "unit-2", "asset-1"}, True),
        ({"rev-a", "unit-1", "unit-x"}, False),
        ({"rev-a", "asset-x"}, False),
        ({"rev-b", "unit-1"}, False),
        ({"unit-1"}, False),
        (set(), False),
    ],
)
def test_allows_checks_the_one_revision_and_the_exclusions(dependencies, expected):
    assert _view().allows(dependencies) is expected


def test_view_size_is_revisions_plus_exclusions():
    view = _view()
    assert (
        len(view.revisions) + len(view.excluded_unit_ids) + len(view.excluded_asset_ids)
        == 3
    )
    assert view.unit_filter is None


def test_derived_lookups():
    view = _view()
    assert view.revision_of_extraction == {"ext-a": "rev-a"}
    assert view.extraction_ids == frozenset({"ext-a"})
    assert view.batch_ids == frozenset({"batch-a", "batch-t"})
    assert view.all_members_eligible is False
