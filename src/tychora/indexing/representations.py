"""Pure preparation of exact representation ownership from canonical rows."""

from collections import defaultdict
from collections.abc import Mapping
from hashlib import sha256
from itertools import pairwise
from typing import Any, cast

from tychora.contracts.corpus import ExtractionBundle
from tychora.contracts.evidence import CodeLocator, EvidenceUnit, TextLocator
from tychora.contracts.indexing import (
    OwnedInterval,
    PreparedExtraction,
    RepresentationUnit,
)


def _bundle_units(bundle: ExtractionBundle) -> dict[str, EvidenceUnit]:
    units = {unit.unit_id: unit for unit in bundle.units}
    if (
        len(units) != len(bundle.units)
        or bundle.extraction.revision_id != bundle.revision.revision_id
        or any(
            unit.extraction_id != bundle.extraction.extraction_id
            or sha256(unit.text.encode()).hexdigest() != unit.text_sha
            for unit in bundle.units
        )
    ):
        raise ValueError("Canonical extraction membership or text identity differs")
    rows: dict[str, list[tuple[int, object]]] = defaultdict(list)
    for row in bundle.locators:
        if row.unit_id not in units:
            raise ValueError("Canonical locator belongs outside extraction")
        rows[row.unit_id].append((row.fragment_ordinal, row.locator))
    for unit in bundle.units:
        ordered = sorted(rows.get(unit.unit_id, ()))
        if [ordinal for ordinal, _ in ordered] != list(range(len(ordered))) or tuple(
            locator for _, locator in ordered
        ) != unit.locators:
            raise ValueError("Canonical locator rows differ from evidence unit")
    if any(record.unit_id not in units for record in bundle.records):
        raise ValueError("Canonical record belongs outside extraction")
    assets = {asset.asset_id: asset for asset in bundle.assets}
    if len(assets) != len(bundle.assets) or any(
        asset.extraction_id != bundle.extraction.extraction_id
        for asset in bundle.assets
    ):
        raise ValueError("Duplicate canonical asset identity")
    proxies = {proxy.proxy_unit_id: proxy for proxy in bundle.proxies}
    if len(proxies) != len(bundle.proxies) or any(
        proxy.proxy_unit_id not in units
        or proxy.asset_id not in assets
        or not proxy.required_unit_ids
        or not proxy.required_unit_ids <= units.keys()
        or any(
            owner in proxies or units[owner].text_origin != "source"
            for owner in proxy.required_unit_ids
        )
        for proxy in bundle.proxies
    ):
        raise ValueError("Canonical proxy requires original source ownership")
    return units


def _locator_intervals(
    target: EvidenceUnit, units: Mapping[str, EvidenceUnit]
) -> list[tuple[int, int, str]] | None:
    locators = [
        locator
        for locator in target.locators
        if isinstance(locator, (TextLocator, CodeLocator))
    ]
    if not locators:
        return None
    if len(locators) != 1:
        raise ValueError("Text/code representation requires one source locator")
    locator = locators[0]
    if locator.char_end - locator.char_start != len(target.text):
        raise ValueError("Source locator range differs from exact unit text")
    result: list[tuple[int, int, str]] = []
    for owner in units.values():
        for candidate in owner.locators:
            same_domain = (
                isinstance(locator, TextLocator)
                and isinstance(candidate, TextLocator)
                and candidate.artifact_ref == locator.artifact_ref
                and candidate.native_ref == locator.native_ref
                or isinstance(locator, CodeLocator)
                and isinstance(candidate, CodeLocator)
                and candidate.artifact_ref == locator.artifact_ref
                and candidate.relative_path == locator.relative_path
            )
            if not same_domain:
                continue
            left = max(locator.char_start, candidate.char_start)
            right = min(locator.char_end, candidate.char_end)
            if left < right:
                result.append(
                    (
                        left - locator.char_start,
                        right - locator.char_start,
                        owner.unit_id,
                    )
                )
    return result


def _parts(unit: EvidenceUnit) -> tuple[Mapping[str, Any], ...]:
    return cast(tuple[Mapping[str, Any], ...], unit.attributes.get("source_parts", ()))


def _document_intervals(
    target: EvidenceUnit, units: Mapping[str, EvidenceUnit]
) -> list[tuple[int, int, str]] | None:
    if target.attributes.get("document_representation") != "docling-source-parts@1":
        return None
    coverage = [
        (cast(int, part["unit_start"]), cast(int, part["unit_end"]))
        for part in _parts(target)
    ]
    for separator in cast(
        tuple[Mapping[str, Any], ...],
        target.attributes.get("structural_separators", ()),
    ):
        start, end = (
            cast(int, separator["unit_start"]),
            cast(int, separator["unit_end"]),
        )
        if target.text[start:end] != separator["text"]:
            raise ValueError("Document structural separator differs from unit text")
        coverage.append((start, end))
    cursor = 0
    for start, end in sorted(coverage):
        if start != cursor or end <= start or end > len(target.text):
            raise ValueError("Document source parts must exactly cover unit text")
        cursor = end
    if cursor != len(target.text):
        raise ValueError("Document source parts must exactly cover unit text")
    result: list[tuple[int, int, str]] = []
    for part in _parts(target):
        unit_start, unit_end = (
            cast(int, part["unit_start"]),
            cast(int, part["unit_end"]),
        )
        source_start, source_end = (
            cast(int, part["source_start"]),
            cast(int, part["source_end"]),
        )
        if unit_end - unit_start != source_end - source_start:
            raise ValueError("Document source part is not an exact literal mapping")
        for owner in units.values():
            for candidate in _parts(owner):
                if candidate["source_domain"] != part["source_domain"]:
                    continue
                left = max(source_start, cast(int, candidate["source_start"]))
                right = min(source_end, cast(int, candidate["source_end"]))
                if left < right:
                    result.append(
                        (
                            unit_start + left - source_start,
                            unit_start + right - source_start,
                            owner.unit_id,
                        )
                    )
    return result


def _partition(
    unit: EvidenceUnit,
    revision_id: str,
    overlaps: list[tuple[int, int, str]],
) -> tuple[OwnedInterval, ...]:
    boundaries = {0, len(unit.text)}
    for start, end, _ in overlaps:
        if not 0 <= start < end <= len(unit.text):
            raise ValueError("Canonical owner interval exceeds unit text")
        boundaries.update((start, end))
    ordered = sorted(boundaries)
    intervals = []
    for start, end in pairwise(ordered):
        owners = {
            owner for left, right, owner in overlaps if left < end and start < right
        }
        owners.add(unit.unit_id)
        intervals.append(
            OwnedInterval(
                char_start=start,
                char_end=end,
                dependency_ids=frozenset({revision_id, *owners}),
            )
        )
    return tuple(intervals)


def _prepare_unit(
    bundle: ExtractionBundle,
    units: Mapping[str, EvidenceUnit],
    unit: EvidenceUnit,
) -> RepresentationUnit:
    if not unit.text:
        ownership: tuple[OwnedInterval, ...] = ()
    else:
        proxy = next(
            (row for row in bundle.proxies if row.proxy_unit_id == unit.unit_id), None
        )
        if proxy is not None:
            ownership = (
                OwnedInterval(
                    char_start=0,
                    char_end=len(unit.text),
                    dependency_ids=frozenset(
                        {
                            bundle.revision.revision_id,
                            unit.unit_id,
                            proxy.asset_id,
                            *proxy.required_unit_ids,
                        }
                    ),
                ),
            )
        else:
            overlaps = _document_intervals(unit, units)
            if overlaps is None:
                overlaps = _locator_intervals(unit, units)
            if overlaps is None:
                raise ValueError("Text unit has no retained source ownership mapping")
            ownership = _partition(unit, bundle.revision.revision_id, overlaps)
            actual = frozenset(
                dependency
                for interval in ownership
                for dependency in interval.dependency_ids
                if dependency not in {bundle.revision.revision_id}
            )
            declared = unit.attributes.get("range_dependency_ids")
            if declared is None:
                declared = unit.attributes.get("overlapping_owner_ids")
            if (
                declared is not None
                and frozenset(cast(tuple[str, ...], declared)) != actual
            ):
                raise ValueError("Retained source owner set differs from exact ranges")
    symbol_records = [
        record
        for record in bundle.records
        if record.unit_id == unit.unit_id and record.record_type == "code_symbol"
    ]
    if len(symbol_records) > 1:
        raise ValueError("Code unit has multiple symbol records")
    symbol_path: tuple[str, ...] = ()
    if symbol_records:
        data = symbol_records[0].data_json
        symbol_path = (
            cast(str, data["relative_path"]),
            cast(str, data["qualified_name"]),
        )
    return RepresentationUnit(
        unit=unit,
        revision_id=bundle.revision.revision_id,
        title=bundle.revision.title,
        kind_id=bundle.revision.kind_id,
        symbol_path=symbol_path,
        ownership=ownership,
    )


def prepare_extraction(bundle: ExtractionBundle) -> PreparedExtraction:
    """Validate and index retained ownership once for all extraction units."""
    units = _bundle_units(bundle)
    return PreparedExtraction(
        extraction_id=bundle.extraction.extraction_id,
        revision_id=bundle.revision.revision_id,
        units=tuple(
            _prepare_unit(bundle, units, unit)
            for unit in sorted(
                bundle.units, key=lambda item: (item.ordinal, item.unit_id)
            )
        ),
    )
