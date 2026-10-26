"""Edition selection over the retained revisions of each source."""

from dataclasses import dataclass
from datetime import date
from typing import Literal


@dataclass(frozen=True)
class RevisionFact:
    """Detached selected edition and current canonical authorization facts."""

    source_id: str
    revision_id: str
    extraction_id: str
    collection_id: str
    kind_id: str
    access_rank: int
    source_floor: int
    collection_floor: int
    allowed_uses: frozenset[str]
    review_state: str
    withdrawn: bool
    published_on: str | None
    effective_from: str | None
    effective_until: str | None
    is_default: bool
    kind_allowed_uses: frozenset[str] | None = None
    global_facets: frozenset[str] = frozenset()
    collection_archived: bool = False


def _parsed_date(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


def select_temporal_revisions(
    revisions: tuple[RevisionFact, ...],
    *,
    explicit_ids: tuple[str, ...] = (),
    selector: Literal["current", "all"] = "current",
    as_of: date | None = None,
    cutoff: date | None = None,
    unknown_dates: Literal["strict", "permissive"] = "strict",
) -> tuple[frozenset[str], tuple[str, ...]]:
    """Select retained editions after independently applying both date constraints."""
    selected: set[str] = set()
    warnings: list[str] = []
    grouped: dict[str, list[RevisionFact]] = {}
    explicit = set(explicit_ids)
    for revision in revisions:
        if explicit and revision.revision_id not in explicit:
            continue
        grouped.setdefault(revision.source_id, []).append(revision)
    for source, editions in grouped.items():
        known: list[tuple[date, RevisionFact]] = []
        unknown: list[RevisionFact] = []
        for edition in editions:
            start = _parsed_date(edition.effective_from)
            end = _parsed_date(edition.effective_until)
            published = _parsed_date(edition.published_on)
            if as_of is not None and end is not None and as_of >= end:
                continue
            if cutoff is not None and published is not None and published > cutoff:
                continue
            missing = (
                as_of is not None
                and start is None
                or cutoff is not None
                and published is None
            )
            if missing:
                if unknown_dates == "permissive":
                    unknown.append(edition)
                continue
            if as_of is not None and start is not None and as_of < start:
                continue
            known.append((start or published or date.min, edition))
        if explicit or selector == "all":
            selected.update(item.revision_id for _, item in known)
            selected.update(item.revision_id for item in unknown)
        elif as_of is not None or cutoff is not None:
            if known:
                latest = max(key for key, _ in known)
                winners = [item for key, item in known if key == latest]
                selected.update(item.revision_id for item in winners)
                if len(known) > 1:
                    warnings.append(
                        f"Overlapping retained editions for source {source}"
                    )
            selected.update(item.revision_id for item in unknown)
        else:
            selected.update(item.revision_id for _, item in known if item.is_default)
    return frozenset(selected), tuple(warnings)
