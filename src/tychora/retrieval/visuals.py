"""Original pixel evidence selected independently of text rerank scores."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tychora.application.reranking import (
    RetainedReadBudget,
    load_retained_representation,
)
from tychora.contracts.errors import TychoraError
from tychora.indexing.representations import prepare_extraction
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.rerank import RankedEvidence

if TYPE_CHECKING:
    from tychora.application.owner import WorkspaceOwner
    from tychora.retrieval.eligibility import EligibilityAttempt

from tychora.contracts.corpus import ExtractionBundle, RetainedRepresentationRows
from tychora.contracts.evidence import (
    Citation,
    ImageLocator,
    LabelledProxy,
    Segment,
    VisualEvidence,
)
from tychora.contracts.graph import EligibilityView
from tychora.retrieval.fusion import FusedCandidate


@dataclass(frozen=True)
class AssetCandidate:
    """One original asset with detached canonical rows and a pinned grant."""

    candidate: FusedCandidate
    rows: ExtractionBundle | RetainedRepresentationRows
    view: EligibilityView
    dependency_ids: frozenset[str]
    native_multimodal: bool = False


def _visual(
    candidate: AssetCandidate, reasons: tuple[str, ...]
) -> VisualEvidence | None:
    rows = candidate.rows
    asset_id = candidate.candidate.key[1]
    if candidate.candidate.key[0] != "asset":
        return None
    asset = next((asset for asset in rows.assets if asset.asset_id == asset_id), None)
    allowed = (
        candidate.view.revision_ids
        | candidate.view.context_unit_ids
        | candidate.view.structural_unit_ids
        | candidate.view.asset_ids
    )
    if (
        not {asset_id, rows.revision.revision_id} <= candidate.dependency_ids
        or not candidate.dependency_ids <= allowed
        or asset is None
        or not isinstance(asset.locator, ImageLocator)
        or asset.asset_id not in candidate.view.asset_ids
        or rows.revision.revision_id not in candidate.view.revision_ids
    ):
        return None
    units = {unit.unit_id: unit for unit in rows.units}
    sources = tuple(
        link.unit_id
        for link in rows.unit_assets
        if link.asset_id == asset_id and link.role == "source"
    )
    if not sources or any(
        unit_id not in units
        or unit_id
        not in candidate.view.context_unit_ids | candidate.view.structural_unit_ids
        or not units[unit_id].citable
        or units[unit_id].text_origin != "source"
        for unit_id in sources
    ):
        return None
    proxies = tuple(
        LabelledProxy(
            proxy_id=proxy.proxy_unit_id,
            label=proxy.origin_type,
            text=units[proxy.proxy_unit_id].text,
            origin=units[proxy.proxy_unit_id].text_origin,
            dependencies=proxy.required_unit_ids | {asset_id},
            review_state=units[proxy.proxy_unit_id].review_state,
        )
        for proxy in rows.proxies
        if proxy.asset_id == asset_id
        and proxy.proxy_unit_id in candidate.view.proxy_unit_ids
        and proxy.proxy_unit_id in units
        and not units[proxy.proxy_unit_id].citable
        and units[proxy.proxy_unit_id].text_origin != "source"
        and proxy.required_unit_ids <= allowed
    )
    return VisualEvidence(
        asset_id=asset.asset_id,
        source_id=rows.source.source_id,
        revision_id=rows.revision.revision_id,
        asset_ref=asset.artifact_sha,
        locator=asset.locator,
        caption=(
            asset.caption or None
            if any(
                link.asset_id == asset_id
                and link.role == "caption"
                and link.unit_id in candidate.view.context_unit_ids
                for link in rows.unit_assets
            )
            else None
        ),
        proxies=proxies,
        selection_reasons=reasons,
        citations=(
            Citation(
                asset_id=asset.asset_id,
                source_id=rows.source.source_id,
                revision_id=rows.revision.revision_id,
                source_ref=next(
                    (
                        ref.source_ref
                        for ref in rows.revision_artifacts
                        if ref.artifact_sha == asset.locator.artifact_ref
                    ),
                    rows.source.source_key,
                ),
                physical_page=asset.locator.physical_page,
                printed_page=asset.locator.printed_page,
                locators=(asset.locator,),
            ),
        ),
    )


def select_visuals(
    assets: tuple[AssetCandidate, ...], segments: tuple[Segment, ...], limit: int = 3
) -> tuple[VisualEvidence, ...]:
    """Choose native pixels, then a distinct text companion, then asset RRF."""
    if not 0 <= limit <= 3:
        raise ValueError("Visual limit must be between zero and three")
    if limit == 0:
        return ()
    ordered = sorted(
        assets,
        key=lambda item: (
            -item.candidate.score,
            item.candidate.rank,
            item.candidate.key,
        ),
    )
    native = [
        item
        for item in ordered
        if item.native_multimodal or "visual" in item.candidate.ranks
    ]
    selected: list[VisualEvidence] = []
    used: set[str] = set()

    def reasons(item: AssetCandidate, extra: tuple[str, ...]) -> tuple[str, ...]:
        base = []
        if item.native_multimodal or "visual" in item.candidate.ranks:
            base.append("multimodal_match")
        if any(channel != "visual" for channel in item.candidate.ranks):
            base.append("proxy_match")
        return tuple(dict.fromkeys((*base, *extra)))

    def add(item: AssetCandidate, extra: tuple[str, ...]) -> bool:
        asset_id = item.candidate.key[1]
        if asset_id in used or len(selected) >= limit:
            return False
        visual = _visual(item, reasons(item, extra))
        if visual is None:
            return False
        selected.append(visual)
        used.add(asset_id)
        return True

    for item in native:
        if add(item, ("reserved_multimodal",)):
            break
    if segments:
        strongest = segments[0]

        def companion_order(item: AssetCandidate) -> tuple[int, int, str]:
            linked = {
                link.unit_id: link.role
                for link in item.rows.unit_assets
                if link.asset_id == item.candidate.key[1]
            }
            priority = 2
            for unit_id in strongest.matched_unit_ids:
                if linked.get(unit_id) == "source":
                    priority = 0
                    break
                if linked.get(unit_id) == "caption":
                    priority = min(priority, 1)
            ordinals = [
                unit.ordinal for unit in item.rows.units if unit.unit_id in linked
            ]
            return priority, min(ordinals, default=0), item.candidate.key[1]

        companions = sorted(
            (
                item
                for item in ordered
                if item.rows.source.source_id == strongest.source_id
                and item.rows.revision.revision_id == strongest.revision_id
                and item.rows.extraction.extraction_id == strongest.extraction_id
            ),
            key=companion_order,
        )
        companion_added = False
        for companion in companions:
            if (
                companion.candidate.key[1] not in used
                and len(selected) < limit
                and add(companion, ("source_link", "reserved_companion"))
            ):
                companion_added = True
                break
        if not companion_added and companions:
            companion_ids = {item.candidate.key[1] for item in companions}
            for index, visual in enumerate(selected):
                if visual.asset_id in companion_ids:
                    selected[index] = visual.model_copy(
                        update={
                            "selection_reasons": tuple(
                                dict.fromkeys(
                                    (
                                        *visual.selection_reasons,
                                        "source_link",
                                        "reserved_companion",
                                    )
                                )
                            )
                        }
                    )
                    break
    for item in ordered:
        if len(selected) >= limit:
            break
        add(item, ())
    return tuple(selected)


async def prepare_visual_candidates(
    owner: WorkspaceOwner,
    attempt: EligibilityAttempt,
    ranked: RankedEvidence,
    ctx: ExecutionContext,
) -> tuple[AssetCandidate, ...]:
    """Read original assets through Task 30's bounded retained-facts loader."""
    if attempt.owner is not owner or len(ranked.items) > 64:
        raise TychoraError("INTEGRITY_ERROR", "Visual attempt owner differs.")
    view = attempt.view
    allowed = (
        view.revision_ids
        | view.context_unit_ids
        | view.structural_unit_ids
        | view.asset_ids
    )

    def load() -> tuple[AssetCandidate, ...]:
        budget = RetainedReadBudget()
        retained: dict[str, RetainedRepresentationRows] = {}
        output: list[AssetCandidate] = []
        for item in ranked.items:
            ctx.checkpoint()
            if item.candidate.key[0] != "asset":
                continue
            asset_id = item.candidate.key[1]
            if (
                asset_id not in view.asset_ids
                or not attempt.dependencies_for(asset_id) <= allowed
            ):
                raise TychoraError("FORBIDDEN_SCOPE", "Visual asset is not eligible.")
            row = owner.db.execute(
                "SELECT extraction_id FROM assets WHERE asset_id=?", (asset_id,)
            ).fetchone()
            if row is None:
                raise TychoraError("INTEGRITY_ERROR", "Visual asset is absent.")
            extraction_id = row[0]
            budget.accept({"extraction_id": extraction_id})
            if extraction_id not in retained:
                rows = load_retained_representation(
                    owner.db, extraction_id, budget, ctx
                )
                try:
                    prepare_extraction(rows)
                except (ValueError, TypeError, KeyError, AttributeError):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Retained visual provenance is invalid."
                    ) from None
                retained[extraction_id] = rows
            rows = retained[extraction_id]
            if rows.revision.revision_id not in view.revision_ids or not any(
                asset.asset_id == asset_id for asset in rows.assets
            ):
                raise TychoraError("INTEGRITY_ERROR", "Retained visual asset differs.")
            output.append(
                AssetCandidate(
                    item.candidate,
                    rows,
                    view,
                    attempt.dependencies_for(asset_id),
                    native_multimodal="visual" in item.candidate.ranks,
                )
            )
        return tuple(output)

    return await owner.run_blocking(load, ctx)
