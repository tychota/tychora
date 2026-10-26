"""Visuals remain original pixel evidence independent of text reranking."""

import pytest

from tests.helpers.corpus import artifact, reader, registry, seed_bundle
from tests.helpers.eligibility import eligibility_view
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.contracts.corpus import AssetRow, RevisionArtifactRow, UnitAssetRow
from tychora.contracts.evidence import ImageLocator, Segment
from tychora.contracts.identity import digest
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.eligibility import EligibilityAttempt, EligibilityCompiler
from tychora.retrieval.fusion import FusedCandidate
from tychora.retrieval.rerank import RankedEvidence, RankedEvidenceItem
from tychora.retrieval.visuals import (
    AssetCandidate,
    prepare_visual_candidates,
    select_visuals,
)


def visual_case(source_key="image.png", rank=1, score=0.01, *, visual=True):
    bundle = seed_bundle("", source_key=source_key)
    pixels = artifact(b"synthetic encoded pixels", "image/png")
    locator = ImageLocator(artifact_ref=pixels.sha256, native_ref=source_key)
    asset_id = digest(
        "asset-v1", bundle.extraction.extraction_id, source_key, pixels.sha256
    )
    asset = AssetRow(
        asset_id=asset_id,
        extraction_id=bundle.extraction.extraction_id,
        artifact_sha=pixels.sha256,
        asset_type="image",
        native_ref=source_key,
        locator=locator,
    )
    source_unit = bundle.units[0].model_copy(
        update={"unit_type": "image_original", "locators": (locator,)}
    )
    bundle = bundle.model_copy(
        update={
            "units": (source_unit,),
            "locators": (bundle.locators[0].model_copy(update={"locator": locator}),),
            "assets": (asset,),
            "artifacts": (*bundle.artifacts, pixels),
            "revision_artifacts": (
                *bundle.revision_artifacts,
                RevisionArtifactRow(
                    revision_id=bundle.revision.revision_id,
                    role="sidecar",
                    source_ref=source_key,
                    artifact_sha=pixels.sha256,
                ),
            ),
            "unit_assets": (
                UnitAssetRow(
                    unit_id=source_unit.unit_id, asset_id=asset_id, role="source"
                ),
            ),
        }
    )
    view = eligibility_view(
        revision_ids=frozenset({bundle.revision.revision_id}),
        context_unit_ids=frozenset({source_unit.unit_id}),
    ).model_copy(update={"asset_ids": frozenset({asset_id})})
    candidate = FusedCandidate(
        ("asset", asset_id),
        rank,
        score,
        {"visual": rank} if visual else {"lexical": rank},
    )
    return AssetCandidate(
        candidate,
        bundle,
        view,
        frozenset({asset_id, bundle.revision.revision_id, source_unit.unit_id}),
        native_multimodal=visual,
    )


def test_zero_text_original_image_needs_no_proxy_or_text_score():
    native = visual_case()
    selected = select_visuals((native,), ())
    assert len(selected) == 1
    assert selected[0].asset_id == native.candidate.key[1]
    assert selected[0].proxies == ()
    assert selected[0].citations[0].locators == (native.rows.assets[0].locator,)
    assert selected[0].selection_reasons == ("multimodal_match", "reserved_multimodal")


def test_native_wins_tight_limit_and_companion_is_distinct():
    native = visual_case("native.png", score=0.001)
    companion = visual_case("companion.png", score=0.1, visual=False)
    strongest = Segment(
        segment_id="text-segment",
        source_id=companion.rows.source.source_id,
        revision_id=companion.rows.revision.revision_id,
        extraction_id=companion.rows.extraction.extraction_id,
        kind="manual@1",
        role="supporting_evidence",
        lane="standard",
        excerpts=(),
        text="matched text",
        matched_unit_ids=(),
    )
    assert (
        select_visuals((companion, native), (strongest,), limit=1)[0].asset_id
        == native.candidate.key[1]
    )
    selected = select_visuals((companion, native), (strongest,), limit=2)
    assert [item.asset_id for item in selected] == [
        native.candidate.key[1],
        companion.candidate.key[1],
    ]
    assert selected[1].selection_reasons == (
        "proxy_match",
        "source_link",
        "reserved_companion",
    )
    same_source = strongest.model_copy(
        update={
            "source_id": native.rows.source.source_id,
            "revision_id": native.rows.revision.revision_id,
            "extraction_id": native.rows.extraction.extraction_id,
        }
    )
    selected = select_visuals((native, companion), (same_source,), limit=2)
    assert [item.asset_id for item in selected] == [
        native.candidate.key[1],
        companion.candidate.key[1],
    ]
    assert selected[0].selection_reasons == (
        "multimodal_match",
        "reserved_multimodal",
        "source_link",
        "reserved_companion",
    )
    assert selected[1].selection_reasons == ("proxy_match",)


@pytest.mark.asyncio
async def test_visual_producer_reads_retained_zero_text_original(tmp_path):
    fixture = visual_case("retained.png")
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    store = CorpusStore(db, registry=registry(), artifacts=reader())
    store.commit_extraction(fixture.rows)

    class Owner:
        def __init__(self):
            self.db = db

        async def run_write(self, call, ctx, priority="background"):
            ctx.checkpoint()
            return call()

    owner = Owner()
    asset_id = fixture.candidate.key[1]
    attempt = EligibilityAttempt(
        owner=owner,  # ty: ignore[invalid-argument-type] - isolated owner shim uses the same DB and blocking call
        view=fixture.view,
        compiler=EligibilityCompiler(store),
        dependencies={
            asset_id: frozenset(
                {
                    asset_id,
                    fixture.rows.revision.revision_id,
                    fixture.rows.units[0].unit_id,
                }
            )
        },
    )
    ranked = RankedEvidence(
        (RankedEvidenceItem(fixture.candidate, None, None, "independent"),)
    )
    candidates = await prepare_visual_candidates(
        owner,  # ty: ignore[invalid-argument-type] - local owner shim isolates retained visual loading
        attempt,
        ranked,
        ExecutionContext(),
    )
    assert len(candidates) == 1
    assert candidates[0].rows.assets[0].asset_id == fixture.candidate.key[1]
    selected = select_visuals(candidates, ())
    assert selected[0].citations[0].locators == (fixture.rows.assets[0].locator,)
    db.close()


def test_companion_reservation_requires_selected_text_revision():
    from dataclasses import replace

    old = visual_case("shared.png", rank=1, score=0.9, visual=False)
    current = visual_case("shared.png", rank=2, score=0.1, visual=False)
    assert old.rows.source.source_id == current.rows.source.source_id
    new_revision = "revision-current"
    new_extraction = "extraction-current"
    new_asset = "asset-current"
    current_rows = current.rows.model_copy(
        update={
            "revision": current.rows.revision.model_copy(
                update={"revision_id": new_revision}
            ),
            "extraction": current.rows.extraction.model_copy(
                update={"extraction_id": new_extraction, "revision_id": new_revision}
            ),
            "assets": (
                current.rows.assets[0].model_copy(
                    update={"asset_id": new_asset, "extraction_id": new_extraction}
                ),
            ),
            "unit_assets": (
                current.rows.unit_assets[0].model_copy(update={"asset_id": new_asset}),
            ),
        }
    )
    current_view = type(
        "View",
        (),
        {
            "asset_ids": frozenset({new_asset}),
            "revision_ids": frozenset({new_revision}),
            "context_unit_ids": frozenset({current_rows.units[0].unit_id}),
            "structural_unit_ids": frozenset(),
            "proxy_unit_ids": frozenset(),
        },
    )()
    current = replace(
        current,
        candidate=replace(current.candidate, key=("asset", new_asset)),
        rows=current_rows,
        view=current_view,
        dependency_ids=frozenset(
            {new_asset, new_revision, current_rows.units[0].unit_id}
        ),
    )
    strongest = Segment(
        segment_id="current-text",
        source_id=current_rows.source.source_id,
        revision_id=new_revision,
        extraction_id=new_extraction,
        kind="manual@1",
        role="supporting_evidence",
        lane="standard",
        excerpts=(),
        text="current text",
        matched_unit_ids=(),
    )
    selected = select_visuals((old, current), (strongest,), limit=1)
    assert selected[0].asset_id == new_asset
    assert selected[0].selection_reasons == (
        "proxy_match",
        "source_link",
        "reserved_companion",
    )
    selected = select_visuals((old, current), (strongest,), limit=2)
    assert selected[1].asset_id == old.candidate.key[1]
    assert selected[1].selection_reasons == ("proxy_match",)
