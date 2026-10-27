"""Visuals remain original pixel evidence independent of text reranking."""

import pytest

from tests.helpers.corpus import (
    artifact,
    commit_with_facts,
    reader,
    registry,
    seed_bundle,
)
from tests.helpers.eligibility import revision_attempt, revision_view
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.contracts.corpus import AssetRow, RevisionArtifactRow, UnitAssetRow
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import ImageLocator, Segment
from tychora.contracts.identity import digest
from tychora.ports.execution import ExecutionContext
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
    revision_id = bundle.revision.revision_id
    view = revision_view({revision_id: bundle.extraction.extraction_id})
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
        frozenset({asset_id, revision_id, source_unit.unit_id}),
        {source_unit.unit_id: frozenset({revision_id, source_unit.unit_id})},
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
    commit_with_facts(store, fixture.rows)

    class Owner:
        def __init__(self):
            self.db = db

        async def run_write(self, call, ctx, priority="background"):
            ctx.checkpoint()
            return call()

        async def run_read(self, call, ctx):
            ctx.checkpoint()
            with store.uow.read() as tx:
                return call(tx)

    owner = Owner()
    attempt = revision_attempt(
        fixture.view,
        owner=owner,  # ty: ignore[invalid-argument-type] - isolated owner shim uses the same DB and blocking call
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
    # The asset slice carries the units linked to the asset and their persisted sets.
    source_unit = fixture.rows.units[0].unit_id
    assert [unit.unit_id for unit in candidates[0].rows.units] == [source_unit]
    assert set(candidates[0].unit_dependencies) == {source_unit}
    assert candidates[0].dependency_ids == fixture.dependency_ids
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
    current_view = revision_view({new_revision: new_extraction})
    current_unit = current_rows.units[0].unit_id
    current = replace(
        current,
        candidate=replace(current.candidate, key=("asset", new_asset)),
        rows=current_rows,
        view=current_view,
        dependency_ids=frozenset({new_asset, new_revision, current_unit}),
        unit_dependencies={current_unit: frozenset({new_revision, current_unit})},
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


# ---------------------------------------------------------------------------
# Exclusion on the visual path


def _with_proxy(case: AssetCandidate, required: str = "required-unit"):
    """The case with a derived proxy of its asset that requires an unlinked unit."""
    from dataclasses import replace

    from tychora.contracts.corpus import AssetProxyRow

    asset_id = case.candidate.key[1]
    revision_id = case.rows.revision.revision_id
    proxy = case.rows.units[0].model_copy(
        update={
            "unit_id": "proxy-unit",
            "unit_type": "asset_proxy",
            "citable": False,
            "text_origin": "derived",
            "text": "A pump diagram.",
            "ordinal": 1,
        }
    )
    rows = case.rows.model_copy(
        update={
            "units": (*case.rows.units, proxy),
            "proxies": (
                AssetProxyRow(
                    proxy_unit_id=proxy.unit_id,
                    asset_id=asset_id,
                    origin_type="description",
                    derivation_sha=digest("synthetic-derivation"),
                    required_unit_ids=frozenset({required}),
                ),
            ),
        }
    )
    # A proxy's persisted set names its asset and its required units.
    sets = {
        **case.unit_dependencies,
        proxy.unit_id: frozenset({revision_id, proxy.unit_id, asset_id, required}),
    }
    return replace(case, rows=rows, unit_dependencies=sets)


def _excluding(case: AssetCandidate, *units: str) -> AssetCandidate:
    from dataclasses import replace

    grants = {case.rows.revision.revision_id: case.rows.extraction.extraction_id}
    return replace(case, view=revision_view(grants, excluded_units=units))


def test_proxy_keeps_an_unlinked_required_unit_while_it_is_eligible():
    case = _with_proxy(visual_case())
    (visual,) = select_visuals((case,), ())
    assert [proxy.proxy_id for proxy in visual.proxies] == ["proxy-unit"]
    (visual,) = select_visuals((_excluding(case, "required-unit"),), ())
    assert visual.proxies == ()


def test_excluded_proxy_is_filtered_and_excluded_source_hides_the_visual():
    case = _with_proxy(visual_case())
    (visual,) = select_visuals((_excluding(case, "proxy-unit"),), ())
    assert visual.proxies == ()
    source = case.rows.units[0].unit_id
    assert select_visuals((_excluding(case, source),), ()) == ()


def _retained_owner(tmp_path, fixture: AssetCandidate):
    """An owner shim over a fresh store holding the fixture's rows and facts."""
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    store = CorpusStore(db, registry=registry(), artifacts=reader())
    commit_with_facts(store, fixture.rows)

    class Owner:
        def __init__(self):
            self.db = db

        async def run_read(self, call, ctx):
            ctx.checkpoint()
            with store.uow.read() as tx:
                return call(tx)

    return db, Owner()


async def _prepared(owner, view, candidate):
    attempt = revision_attempt(view, owner=owner)
    ranked = RankedEvidence((RankedEvidenceItem(candidate, None, None, "independent"),))
    return await prepare_visual_candidates(owner, attempt, ranked, ExecutionContext())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("excluded_asset", "scope.dependency_ineligible"),
        ("other_extraction", "scope.dependency_ineligible"),
        ("absent_asset", "canonical.row_invalid"),
        ("missing_unit_set", "canonical.facts_missing"),
    ],
)
async def test_visual_producer_refuses_what_it_cannot_admit(tmp_path, change, reason):
    from dataclasses import replace

    fixture = visual_case("refused.png")
    db, owner = _retained_owner(tmp_path, fixture)
    revision_id = fixture.rows.revision.revision_id
    asset_id = fixture.candidate.key[1]
    view, candidate = fixture.view, fixture.candidate
    if change == "excluded_asset":
        view = revision_view(
            {revision_id: fixture.rows.extraction.extraction_id},
            excluded_assets=(asset_id,),
        )
    elif change == "other_extraction":
        # The revision is selected, but through another of its extractions.
        view = revision_view({revision_id: "another-extraction"})
    elif change == "absent_asset":
        candidate = replace(candidate, key=("asset", "absent-asset"))
    else:
        db.execute(
            "DELETE FROM representation_dependencies WHERE owner_id=?",
            (fixture.rows.units[0].unit_id,),
        )
    with pytest.raises(TychoraError) as error:
        await _prepared(owner, view, candidate)
    assert error.value.reason == reason
    assert error.value.code == (
        "FORBIDDEN_SCOPE" if reason.startswith("scope.") else "INTEGRITY_ERROR"
    )
    db.close()
