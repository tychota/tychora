"""Original pixel evidence selected independently of text rerank scores."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tychora.contracts.corpus import ExtractionBundle, RetainedRepresentationRows
from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import (
    Citation,
    ImageLocator,
    LabelledProxy,
    Segment,
    VisualEvidence,
)
from tychora.ports.execution import ExecutionContext
from tychora.ports.repositories import OwnerFact, ReadTransaction
from tychora.retrieval.fusion import FusedCandidate
from tychora.retrieval.rerank import RankedEvidence, RankedEvidenceItem

if TYPE_CHECKING:
    from tychora.application.owner import WorkspaceOwner
    from tychora.retrieval.attempt import EligibilityAttempt


@dataclass(frozen=True)
class AssetCandidate:
    """One original asset with its slice rows, its view and its units' persisted sets."""

    candidate: FusedCandidate
    rows: ExtractionBundle | RetainedRepresentationRows
    view: EligibilityView
    dependency_ids: frozenset[str]
    unit_dependencies: Mapping[str, frozenset[str]]
    native_multimodal: bool = False

    def unit_readable(self, unit_id: str) -> bool:
        """Whether the view allows the unit's persisted set."""
        sets = self.unit_dependencies.get(unit_id)
        return sets is not None and self.view.allows(sets)


def _visual(
    candidate: AssetCandidate, reasons: tuple[str, ...]
) -> VisualEvidence | None:
    rows = candidate.rows
    asset_id = candidate.candidate.key[1]
    if candidate.candidate.key[0] != "asset":
        return None
    asset = next((asset for asset in rows.assets if asset.asset_id == asset_id), None)
    readable = candidate.unit_readable
    if (
        not {asset_id, rows.revision.revision_id} <= candidate.dependency_ids
        or not candidate.view.allows(candidate.dependency_ids)
        or asset is None
        or not isinstance(asset.locator, ImageLocator)
        or rows.revision.revision_id not in candidate.view.revisions
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
        or not readable(unit_id)
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
        and readable(proxy.proxy_unit_id)
        and proxy.proxy_unit_id in units
        and not units[proxy.proxy_unit_id].citable
        and units[proxy.proxy_unit_id].text_origin != "source"
        and all(readable(unit_id) for unit_id in proxy.required_unit_ids)
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
                and readable(link.unit_id)
                and link.unit_id in units
                and units[link.unit_id].citable
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


def _asset_candidate(
    tx: ReadTransaction,
    view: EligibilityView,
    item: RankedEvidenceItem,
    fact: OwnerFact | None,
) -> AssetCandidate:
    """One eligible asset's slice and its units' persisted sets."""
    asset_id = item.candidate.key[1]
    if fact is None or fact.kind != "asset":
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Visual asset is absent.",
            reason="canonical.row_invalid",
        )
    if not view.allows(fact.dependency_ids):
        raise TychoraError(
            "FORBIDDEN_SCOPE",
            "Visual asset is not eligible.",
            reason="scope.dependency_ineligible",
        )
    piece = tx.representations.slice(fact.extraction_id, asset_ids=(asset_id,))
    return AssetCandidate(
        item.candidate,
        piece.rows,
        view,
        fact.dependency_ids,
        tx.representations.dependencies(tuple(u.unit_id for u in piece.rows.units)),
        native_multimodal="visual" in item.candidate.ranks,
    )


async def prepare_visual_candidates(
    owner: WorkspaceOwner,
    attempt: EligibilityAttempt,
    ranked: RankedEvidence,
    ctx: ExecutionContext,
) -> tuple[AssetCandidate, ...]:
    """Read each original asset's slice and persisted sets on the read pool."""
    if attempt.owner is not owner or len(ranked.items) > 64:
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Visual attempt owner differs.",
            reason="invariant.unexpected",
        )
    view = attempt.view
    assets = tuple(item for item in ranked.items if item.candidate.key[0] == "asset")

    def load(tx: ReadTransaction) -> tuple[AssetCandidate, ...]:
        facts = {
            fact.owner_id: fact
            for fact in tx.eligibility.owner_facts(
                tuple(item.candidate.key[1] for item in assets)
            )
        }
        output: list[AssetCandidate] = []
        for item in assets:
            ctx.checkpoint()
            fact = facts.get(item.candidate.key[1])
            output.append(_asset_candidate(tx, view, item, fact))
        return tuple(output)

    if not assets:
        return ()
    return await owner.run_read(load, ctx)
