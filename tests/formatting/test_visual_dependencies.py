"""Visual proxy asset ownership is distinct from source-unit/graph authority."""

from dataclasses import replace

import pytest

from tests.helpers.legacy import OutputCounter
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import (
    ImageLocator,
    LabelledProxy,
    RelationChecks,
    VisualEvidence,
)
from tychora.contracts.requests import QueryRequest, RequestScope
from tychora.retrieval.response import ResponseAssembler, ResponseContext
from tychora.retrieval.segments import Selection


@pytest.mark.parametrize("denied", [None, "asset", "source"])
def test_visual_proxy_dependencies_keep_asset_and_source_authority_distinct(denied):
    visual = VisualEvidence(
        asset_id="asset",
        source_id="source",
        revision_id="revision",
        asset_ref="artifact",
        locator=ImageLocator(artifact_ref="artifact", native_ref="pixels"),
        proxies=(
            LabelledProxy(
                proxy_id="ocr",
                label="ocr",
                text="Synthetic derived OCR",
                origin="ocr",
                review_state="needs_review",
                dependencies=frozenset({"asset", "pixel-owner"}),
            ),
        ),
    )
    ctx = ResponseContext(
        run_id="run",
        snapshot_id="snapshot",
        scope=RequestScope(principal_id="synthetic"),
        counter=OutputCounter(),
        eligible_unit_ids=frozenset()
        if denied == "source"
        else frozenset({"pixel-owner"}),
        eligible_asset_ids=frozenset() if denied == "asset" else frozenset({"asset"}),
        visuals=(visual,),
    )
    selected = Selection(
        segments=(),
        reservation_priority=(),
        omitted_anchors=(),
        lane_coverage=(),
        text_budget=6000,
        visual_reserved_tokens=0,
    )

    def deliver(context):
        return ResponseAssembler().assemble(
            selected,
            (),
            RelationChecks(status="not_evaluated"),
            QueryRequest.from_mapping({"question": "synthetic OCR"}),
            context,
        )

    if denied:
        with pytest.raises(TychoraError, match="Visual proxy dependencies differ"):
            deliver(ctx)
    else:
        assert deliver(ctx).result.visuals == (visual,)
        # An asset grant cannot stand in for an excluded unit, or vice versa.
        with pytest.raises(TychoraError, match="Visual proxy dependencies differ"):
            deliver(
                replace(
                    ctx,
                    eligible_unit_ids=frozenset({"asset"}),
                    eligible_asset_ids=frozenset({"pixel-owner"}),
                )
            )
