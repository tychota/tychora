"""Pure preparation of exact representation ownership from canonical rows."""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from itertools import pairwise
from typing import Any, cast

from tychora.contracts.code_storage import CodeFileDescriptor
from tychora.contracts.corpus import (
    AssetProxyRow,
    AssetRow,
    ExtractionBundle,
)
from tychora.contracts.documents import DocumentSeparator, DocumentSourcePart
from tychora.contracts.evidence import (
    CodeLocator,
    DocumentLocator,
    EvidenceUnit,
    ImageLocator,
    TextLocator,
)
from tychora.contracts.identity import canonical_bytes, digest, unit_id
from tychora.contracts.indexing import (
    OwnedInterval,
    PreparedExtraction,
    RepresentationUnit,
)


@dataclass(frozen=True)
class _RetainedIndex:
    units: Mapping[str, EvidenceUnit]
    assets: Mapping[str, AssetRow]
    proxies: Mapping[str, AssetProxyRow]
    asset_owners: Mapping[str, frozenset[str]]
    code_files: Mapping[tuple[str, str], CodeFileDescriptor]
    document_parts: Mapping[str, tuple[DocumentSourcePart, ...]]


def _document_facts(
    bundle: ExtractionBundle, unit: EvidenceUnit
) -> tuple[DocumentSourcePart, ...]:
    refs = cast(tuple[str, ...], unit.attributes.get("source_refs", ()))
    if not refs or len(set(refs)) != len(refs):
        raise ValueError("Document representation requires retained source references")
    parts = tuple(
        DocumentSourcePart.model_validate_json(canonical_bytes(part))
        for part in cast(
            tuple[Mapping[str, Any], ...], unit.attributes.get("source_parts", ())
        )
    )
    separators = tuple(
        DocumentSeparator.model_validate_json(canonical_bytes(separator))
        for separator in cast(
            tuple[Mapping[str, Any], ...],
            unit.attributes.get("structural_separators", ()),
        )
    )
    coverage: list[tuple[int, int]] = []
    seen: dict[tuple[str, str, int | None], list[tuple[int, int]]] = defaultdict(list)
    for part in parts:
        if part.native_ref not in refs or part.source_domain != digest(
            "document-source-domain-v1",
            bundle.revision.revision_id,
            bundle.extraction.extraction_id,
            part.native_ref,
            part.field,
            part.cell_index,
        ):
            raise ValueError("Document source domain identity differs")
        if (
            part.unit_end <= part.unit_start
            or part.unit_end > len(unit.text)
            or any(
                part.source_start < end and part.source_end > start
                for start, end in seen[(part.native_ref, part.field, part.cell_index)]
            )
        ):
            raise ValueError("Document source parts conflict or exceed unit text")
        seen[(part.native_ref, part.field, part.cell_index)].append(
            (part.source_start, part.source_end)
        )
        coverage.append((part.unit_start, part.unit_end))
    for separator in separators:
        if unit.text[separator.unit_start : separator.unit_end] != separator.text:
            raise ValueError("Document structural separator differs from unit text")
        coverage.append((separator.unit_start, separator.unit_end))
    cursor = 0
    for start, end in sorted(coverage):
        if start != cursor or end <= start or end > len(unit.text):
            raise ValueError("Document source parts must exactly cover unit text")
        cursor = end
    if cursor != len(unit.text):
        raise ValueError("Document source parts must exactly cover unit text")
    if unit.attributes.get("overlapping_owner_ids") is None:
        raise ValueError("Document representation requires declared ownership")
    return parts


def _code_files(bundle: ExtractionBundle) -> dict[tuple[str, str], CodeFileDescriptor]:
    files: dict[tuple[str, str], CodeFileDescriptor] = {}
    artifacts = {item.sha256 for item in bundle.artifacts}
    for item in bundle.code_files:
        key = item.artifact_ref, item.relative_path
        if key in files or item.artifact_ref not in artifacts:
            raise ValueError("Retained code file descriptor identity differs")
        files[key] = item
    return files


def _validate_locator(
    bundle: ExtractionBundle,
    unit: EvidenceUnit,
    locator: TextLocator | CodeLocator,
    code_files: Mapping[tuple[str, str], CodeFileDescriptor],
) -> None:
    if locator.char_end - locator.char_start != len(unit.text):
        raise ValueError("Source locator range differs from exact unit text")
    if isinstance(locator, TextLocator):
        if (
            locator.artifact_ref != bundle.revision.original_sha
            or locator.native_ref != bundle.source.source_key
        ):
            raise ValueError("Native source locator binding differs")
        return
    descriptor = code_files.get((locator.artifact_ref, locator.relative_path))
    if (
        descriptor is None
        or locator.native_ref != locator.relative_path
        or locator.char_end > descriptor.char_length
        or locator.byte_end > descriptor.byte_length
        or descriptor.codepoint_to_byte[locator.char_start] != locator.byte_start
        or descriptor.codepoint_to_byte[locator.char_end] != locator.byte_end
        or descriptor.byte_to_line[locator.byte_start] != locator.line_start
        or descriptor.byte_to_line[max(locator.byte_start, locator.byte_end - 1)]
        != locator.line_end
    ):
        raise ValueError("Code locator codepoint/byte/line mapping differs")
    if unit.attributes.get("range_dependency_ids") is None:
        raise ValueError("Code representation requires declared ownership")


def _bundle_index(bundle: ExtractionBundle) -> _RetainedIndex:
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
    code_files = _code_files(bundle)
    for unit in bundle.units:
        native_locators = [
            locator
            for locator in unit.locators
            if isinstance(locator, (TextLocator, CodeLocator))
        ]
        for locator in native_locators:
            _validate_locator(bundle, unit, locator, code_files)
        for locator in unit.locators:
            if isinstance(locator, DocumentLocator) and (
                locator.artifact_ref != bundle.revision.original_sha
                or locator.native_ref != bundle.source.source_key
                or locator.char_end > len(unit.text)
            ):
                raise ValueError("Document locator retained binding differs")
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
    if any(
        (unit.unit_type == "asset_proxy") != (unit.unit_id in proxies)
        for unit in bundle.units
    ):
        raise ValueError("Every asset proxy unit requires exactly one proxy row")
    asset_owners: dict[str, set[str]] = defaultdict(set)
    seen_links: set[tuple[str, str, str]] = set()
    for link in bundle.unit_assets:
        key = link.unit_id, link.asset_id, link.role
        if (
            key in seen_links
            or link.unit_id not in units
            or link.asset_id not in assets
        ):
            raise ValueError("Canonical unit-asset ownership differs")
        seen_links.add(key)
        if link.role in {"source", "caption"}:
            asset_owners[link.asset_id].add(link.unit_id)
    documents: dict[str, tuple[DocumentSourcePart, ...]] = {}
    for unit in bundle.units:
        if unit.attributes.get("document_representation") == "docling-source-parts@1":
            documents[unit.unit_id] = _document_facts(bundle, unit)
    provenance: dict[str, tuple[object, ...]] = {}
    for parts in documents.values():
        for part in parts:
            previous = provenance.setdefault(part.source_domain, part.provenance)
            if previous != part.provenance:
                raise ValueError("Document source provenance differs")
    return _RetainedIndex(
        units=units,
        assets=assets,
        proxies=proxies,
        asset_owners={
            asset_id: frozenset(owner_ids)
            for asset_id, owner_ids in asset_owners.items()
        },
        code_files=code_files,
        document_parts=documents,
    )


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
                target_text = target.text[
                    left - locator.char_start : right - locator.char_start
                ]
                owner_text = owner.text[
                    left - candidate.char_start : right - candidate.char_start
                ]
                if target_text != owner_text:
                    raise ValueError("Retained source slices differ across owners")
                result.append(
                    (
                        left - locator.char_start,
                        right - locator.char_start,
                        owner.unit_id,
                    )
                )
    return result


def _document_intervals(
    target: EvidenceUnit, index: _RetainedIndex
) -> list[tuple[int, int, str]] | None:
    target_parts = index.document_parts.get(target.unit_id)
    if target_parts is None:
        return None
    result: list[tuple[int, int, str]] = []
    for part in target_parts:
        for owner_id, candidates in index.document_parts.items():
            owner = index.units[owner_id]
            for candidate in candidates:
                if candidate.source_domain != part.source_domain:
                    continue
                left = max(part.source_start, candidate.source_start)
                right = min(part.source_end, candidate.source_end)
                if left < right:
                    target_start = part.unit_start + left - part.source_start
                    owner_start = candidate.unit_start + left - candidate.source_start
                    length = right - left
                    if (
                        target.text[target_start : target_start + length]
                        != owner.text[owner_start : owner_start + length]
                    ):
                        raise ValueError(
                            "Retained document source slices differ across owners"
                        )
                    result.append(
                        (
                            target_start,
                            target_start + length,
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


def _representation(
    bundle: ExtractionBundle,
    unit: EvidenceUnit,
    ownership: tuple[OwnedInterval, ...],
) -> RepresentationUnit:
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


def _prepare_source_unit(
    bundle: ExtractionBundle,
    index: _RetainedIndex,
    unit: EvidenceUnit,
) -> RepresentationUnit:
    if not unit.text:
        ownership: tuple[OwnedInterval, ...] = ()
    else:
        overlaps = _document_intervals(unit, index)
        if overlaps is None:
            overlaps = _locator_intervals(unit, index.units)
        if overlaps is None:
            raise ValueError("Text unit has no retained source ownership mapping")
        ownership = _partition(unit, bundle.revision.revision_id, overlaps)
        actual = frozenset(
            dependency
            for interval in ownership
            for dependency in interval.dependency_ids
            if dependency != bundle.revision.revision_id
        )
        declared = unit.attributes.get("range_dependency_ids")
        if declared is None:
            declared = unit.attributes.get("overlapping_owner_ids")
        if (
            declared is not None
            and frozenset(cast(tuple[str, ...], declared)) != actual
        ):
            raise ValueError("Retained source owner set differs from exact ranges")
    return _representation(bundle, unit, ownership)


def _proxy_locator(asset: AssetRow) -> ImageLocator:
    if isinstance(asset.locator, ImageLocator):
        return asset.locator
    return ImageLocator(
        artifact_ref=asset.locator.artifact_ref,
        native_ref=asset.locator.native_ref,
    )


def _prepare_proxy_unit(
    bundle: ExtractionBundle,
    index: _RetainedIndex,
    unit: EvidenceUnit,
    sources: Mapping[str, RepresentationUnit],
) -> RepresentationUnit:
    proxy = index.proxies[unit.unit_id]
    asset = index.assets[proxy.asset_id]
    required = set(proxy.required_unit_ids)
    linked = set(index.asset_owners.get(proxy.asset_id, ()))
    if proxy.derivation_sha not in {item.sha256 for item in bundle.artifacts} or any(
        owner not in sources for owner in required | linked
    ):
        raise ValueError("Canonical proxy requires original source ownership")
    closure = required | linked
    pending = list(closure)
    while pending:
        owner_id = pending.pop()
        for interval in sources[owner_id].ownership:
            for dependency in interval.dependency_ids:
                if dependency in sources and dependency not in closure:
                    closure.add(dependency)
                    pending.append(dependency)
    if required != closure:
        raise ValueError("Canonical proxy omits complete original ownership")
    expected_origin = {
        "caption_copy": "source",
        "ocr": "ocr",
        "description": "derived",
        "structured_values": "derived",
    }[proxy.origin_type]
    expected_review = (
        "unreviewed" if proxy.origin_type == "caption_copy" else "needs_review"
    )
    native_ref = "asset-proxy:" + digest(
        "asset-proxy-input-v1",
        proxy.asset_id,
        proxy.origin_type,
        unit.text,
        sorted(required),
        proxy.derivation_sha,
    )
    expected_id = unit_id(
        bundle.extraction.extraction_id,
        native_ref,
        unit.ordinal,
        unit.text_sha,
    )
    if (
        unit.unit_type != "asset_proxy"
        or unit.unit_id != expected_id
        or unit.native_ref != native_ref
        or unit.boundary_key != native_ref
        or unit.parent_id is not None
        or unit.text_origin != expected_origin
        or unit.citable
        or unit.review_state != expected_review
        or unit.attributes.get("asset_id") != proxy.asset_id
        or unit.attributes.get("proxy_kind") != proxy.origin_type
        or unit.locators != (_proxy_locator(asset),)
    ):
        raise ValueError("Canonical proxy unit differs from row/asset binding")
    ownership = (
        (
            OwnedInterval(
                char_start=0,
                char_end=len(unit.text),
                dependency_ids=frozenset(
                    {
                        bundle.revision.revision_id,
                        unit.unit_id,
                        proxy.asset_id,
                        *required,
                    }
                ),
            ),
        )
        if unit.text
        else ()
    )
    return _representation(bundle, unit, ownership)


def prepare_extraction(bundle: ExtractionBundle) -> PreparedExtraction:
    """Validate and index retained ownership once for all extraction units."""
    index = _bundle_index(bundle)
    sources = {
        unit.unit_id: _prepare_source_unit(bundle, index, unit)
        for unit in bundle.units
        if unit.unit_id not in index.proxies
    }
    prepared = {
        **sources,
        **{
            unit_id: _prepare_proxy_unit(bundle, index, index.units[unit_id], sources)
            for unit_id in index.proxies
        },
    }
    return PreparedExtraction(
        extraction_id=bundle.extraction.extraction_id,
        revision_id=bundle.revision.revision_id,
        units=tuple(
            prepared[unit.unit_id]
            for unit in sorted(
                bundle.units, key=lambda item: (item.ordinal, item.unit_id)
            )
        ),
    )
