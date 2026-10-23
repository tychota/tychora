"""Pure preparation of exact representation ownership from canonical rows."""

import codecs
import weakref
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from itertools import pairwise
from typing import Any, NamedTuple, cast

from tychora.contracts.code_storage import CodeFileDescriptor
from tychora.contracts.corpus import (
    AssetProxyRow,
    AssetRow,
    ExtractionBundle,
    RetainedRepresentationRows,
    StructuredRecordRow,
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
    IntervalRow,
    OwnedInterval,
    OwnerDependencies,
    PreparedExtraction,
    RepresentationFacts,
    RepresentationUnit,
    dependency_digest,
)
from tychora.contracts.sections import boundary_key

from .intervals import overlapping_pairs

type RepresentationRows = ExtractionBundle | RetainedRepresentationRows
type _LocatorPair = tuple[EvidenceUnit, TextLocator | CodeLocator]
type _PartPair = tuple[str, DocumentSourcePart]


@dataclass(frozen=True)
class _RetainedIndex:
    units: Mapping[str, EvidenceUnit]
    assets: Mapping[str, AssetRow]
    proxies: Mapping[str, AssetProxyRow]
    asset_owners: Mapping[str, frozenset[str]]
    code_maps: Mapping[str, "_LocalCodeMap"]
    document_parts: Mapping[str, tuple[DocumentSourcePart, ...]]
    locator_overlaps: Mapping[str, tuple[_LocatorPair, ...]]
    part_overlaps: Mapping[tuple[str, int], tuple[_PartPair, ...]]


@dataclass(frozen=True)
class _RetainedCodeFile:
    unit_id: str
    artifact_ref: str
    relative_path: str
    language: str
    encoding: str
    coverage: str
    adapter_fingerprint: str


@dataclass(frozen=True)
class _LocalCodeMap:
    byte_offsets: tuple[int, ...]
    line_offsets: tuple[int, ...]


def _document_facts(
    bundle: RepresentationRows, unit: EvidenceUnit
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


def _code_files(
    bundle: RepresentationRows,
) -> dict[tuple[str, str], CodeFileDescriptor]:
    files: dict[tuple[str, str], CodeFileDescriptor] = {}
    artifacts = {item.sha256 for item in bundle.artifacts}
    for item in bundle.code_files:
        key = item.artifact_ref, item.relative_path
        if key in files or item.artifact_ref not in artifacts:
            raise ValueError("Retained code file descriptor identity differs")
        files[key] = item
    return files


def _local_code_map(text: str, encoding: str) -> tuple[int, _LocalCodeMap]:
    """Project one retained slice without inventing a new per-slice BOM."""
    try:
        encoder = codecs.getincrementalencoder(encoding)(errors="strict")
        preamble = len(encoder.encode("", final=False))
        byte_offsets = [0]
        offset = 0
        for character in text:
            offset += len(encoder.encode(character, final=False))
            byte_offsets.append(offset)
        if encoder.encode("", final=True):
            raise ValueError(
                "Retained code encoding requires unavailable cross-slice state"
            )
    except (LookupError, UnicodeError) as exc:
        raise ValueError("Retained code text cannot be encoded exactly") from exc

    line_offsets: list[int] = []
    line = 0
    for index, character in enumerate(text):
        line_offsets.append(line)
        if character == "\r":
            if index + 1 >= len(text) or text[index + 1] != "\n":
                line += 1
        elif character == "\n":
            line += 1
    return preamble, _LocalCodeMap(tuple(byte_offsets), tuple(line_offsets))


def _retained_code_files(
    bundle: RepresentationRows, units: Mapping[str, EvidenceUnit]
) -> dict[tuple[str, str], _RetainedCodeFile]:
    """Bind post-hydration code units to their persisted file containers."""
    artifacts = {item.sha256 for item in bundle.artifacts}
    source_binding_rows = [
        (row.source_ref, row.artifact_sha)
        for row in bundle.revision_artifacts
        if row.role == "source_file" and row.revision_id == bundle.revision.revision_id
    ]
    source_bindings = set(source_binding_rows)
    if any(
        row.revision_id != bundle.revision.revision_id
        for row in bundle.revision_artifacts
    ):
        raise ValueError("Revision artifact belongs outside retained revision")

    if len(source_bindings) != len(source_binding_rows):
        raise ValueError("Retained source-file artifact binding is duplicated")
    files: dict[tuple[str, str], _RetainedCodeFile] = {}
    paths: set[str] = set()
    for unit in units.values():
        if unit.unit_type != "code_file":
            continue
        locators = [
            locator for locator in unit.locators if isinstance(locator, CodeLocator)
        ]
        values = tuple(
            unit.attributes.get(name)
            for name in (
                "artifact_ref",
                "relative_path",
                "language",
                "encoding",
                "coverage",
                "adapter_fingerprint",
            )
        )
        if len(locators) != 1 or any(not isinstance(value, str) for value in values):
            raise ValueError("Retained code file container is incomplete")
        artifact_ref, path, language, encoding, coverage, adapter = cast(
            tuple[str, str, str, str, str, str], values
        )
        locator = locators[0]
        preamble, _ = _local_code_map("", encoding)
        key = artifact_ref, path
        if (
            key in files
            or path in paths
            or artifact_ref not in artifacts
            or (path, artifact_ref) not in source_bindings
            or coverage not in {"parsed", "partial", "text_fallback", "empty"}
            or unit.parent_id is not None
            or unit.text
            or unit.citable
            or unit.text_origin != "source"
            or unit.native_ref != f"{path}#file"
            or unit.boundary_key != boundary_key("file", path)
            or locator.artifact_ref != artifact_ref
            or locator.native_ref != path
            or locator.relative_path != path
            or locator.char_start != 0
            or locator.char_end != 0
            or locator.byte_start != preamble
            or locator.byte_end != preamble
            or locator.line_start != 1
            or locator.line_end != 1
        ):
            raise ValueError("Retained code file container binding differs")
        paths.add(path)
        files[key] = _RetainedCodeFile(
            unit_id=unit.unit_id,
            artifact_ref=artifact_ref,
            relative_path=path,
            language=language,
            encoding=encoding,
            coverage=coverage,
            adapter_fingerprint=adapter,
        )
    if {(path, artifact) for artifact, path in files} != source_bindings:
        raise ValueError("Retained code file containers do not cover source bindings")
    return files


def _validate_locator(
    bundle: RepresentationRows,
    unit: EvidenceUnit,
    locator: TextLocator | CodeLocator,
    code_files: Mapping[tuple[str, str], CodeFileDescriptor],
    retained_code_files: Mapping[tuple[str, str], _RetainedCodeFile],
) -> _LocalCodeMap | None:
    if locator.char_end - locator.char_start != len(unit.text):
        raise ValueError("Source locator range differs from exact unit text")
    if isinstance(locator, TextLocator):
        if (
            locator.artifact_ref != bundle.revision.original_sha
            or locator.native_ref != bundle.source.source_key
        ):
            raise ValueError("Native source locator binding differs")
        return None
    key = locator.artifact_ref, locator.relative_path
    if code_files:
        descriptor = code_files.get(key)
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
        if (
            unit.unit_type != "code_file"
            and unit.attributes.get("range_dependency_ids") is None
        ):
            raise ValueError("Code representation requires declared ownership")
        return None

    retained = retained_code_files.get(key)
    if retained is None:
        raise ValueError("Code locator has no retained file container")
    preamble, local_map = _local_code_map(unit.text, retained.encoding)
    line_end = (
        locator.line_start + local_map.line_offsets[-1]
        if local_map.line_offsets
        else locator.line_start
    )
    if (
        locator.native_ref != locator.relative_path
        or unit.text_origin != "source"
        or unit.boundary_key != boundary_key("file", retained.relative_path)
        or unit.attributes.get("relative_path") != retained.relative_path
        or unit.attributes.get("language") != retained.language
        or locator.byte_end - locator.byte_start != local_map.byte_offsets[-1]
        or locator.line_end != line_end
        or unit.unit_type == "code_file"
        and (locator.byte_start != preamble or unit.unit_id != retained.unit_id)
    ):
        raise ValueError("Code locator codepoint/byte/line mapping differs")
    if (
        unit.unit_type != "code_file"
        and unit.attributes.get("range_dependency_ids") is None
    ):
        raise ValueError("Code representation requires declared ownership")
    return local_map


def _one_code_locator(unit: EvidenceUnit) -> CodeLocator | None:
    locators = [
        locator for locator in unit.locators if isinstance(locator, CodeLocator)
    ]
    if not locators:
        return None
    if len(locators) != 1:
        raise ValueError("Code unit requires one retained file locator")
    return locators[0]


def _validate_retained_code_structure(
    units: Mapping[str, EvidenceUnit],
    files: Mapping[tuple[str, str], _RetainedCodeFile],
    code_maps: Mapping[str, _LocalCodeMap],
) -> None:
    for uid in code_maps:
        unit = units[uid]
        locator = _one_code_locator(unit)
        if locator is None:
            raise ValueError("Code unit has no retained file locator")
        file = files[locator.artifact_ref, locator.relative_path]
        visited = {unit.unit_id}
        current = unit
        while current.unit_id != file.unit_id:
            if current.parent_id is None or current.parent_id not in units:
                raise ValueError("Code unit has no retained file-container parent")
            current = units[current.parent_id]
            if current.unit_id in visited:
                raise ValueError("Code unit parent chain is cyclic")
            visited.add(current.unit_id)
            parent_locator = _one_code_locator(current)
            if (
                parent_locator is None
                or parent_locator.artifact_ref != file.artifact_ref
                or parent_locator.relative_path != file.relative_path
            ):
                raise ValueError("Code unit parent crosses retained file container")


def _retained_span_matches(
    span: Mapping[str, Any], locator: CodeLocator, local_map: _LocalCodeMap
) -> bool:
    values = tuple(
        span.get(name)
        for name in (
            "char_start",
            "char_end",
            "byte_start",
            "byte_end",
            "line_start",
            "line_end",
        )
    )
    if any(not isinstance(value, int) for value in values):
        return False
    char_start, char_end, byte_start, byte_end, line_start, line_end = cast(
        tuple[int, int, int, int, int, int], values
    )
    if (
        span.get("coordinate_domain") != "original_file"
        or not locator.char_start <= char_start < char_end <= locator.char_end
    ):
        return False
    local_start = char_start - locator.char_start
    local_end = char_end - locator.char_start
    return (
        byte_start == locator.byte_start + local_map.byte_offsets[local_start]
        and byte_end == locator.byte_start + local_map.byte_offsets[local_end]
        and line_start == locator.line_start + local_map.line_offsets[local_start]
        and line_end == locator.line_start + local_map.line_offsets[local_end - 1]
    )


def _validate_retained_code_records(
    bundle: RepresentationRows,
    units: Mapping[str, EvidenceUnit],
    files: Mapping[tuple[str, str], _RetainedCodeFile],
    code_maps: Mapping[str, _LocalCodeMap],
) -> None:
    locators = {
        uid: locator
        for uid in code_maps
        if (locator := _one_code_locator(units[uid])) is not None
    }
    by_file: dict[tuple[str, str], list[tuple[str, CodeLocator]]] = defaultdict(list)
    for uid, locator in locators.items():
        by_file[(locator.artifact_ref, locator.relative_path)].append((uid, locator))
    records: dict[str, StructuredRecordRow] = {}
    for record in bundle.records:
        if record.record_type != "code_symbol":
            continue
        if record.unit_id in records:
            raise ValueError("Code unit has multiple symbol records")
        records[record.unit_id] = record
    symbol_ids = {
        unit.unit_id
        for unit in units.values()
        if unit.unit_type == "symbol" and unit.unit_id in code_maps
    }
    if set(records) != symbol_ids:
        raise ValueError("Retained code symbol records are incomplete")

    definition_ids = [
        row.unit_id
        for row in bundle.identifiers
        if row.namespace == "code.definition_key"
    ]
    if set(definition_ids) != symbol_ids or len(definition_ids) != len(symbol_ids):
        raise ValueError("Retained code definition identifiers are incomplete")

    for uid, record in records.items():
        unit = units[uid]
        locator = locators[uid]
        file = files[locator.artifact_ref, locator.relative_path]
        local_map = code_maps[uid]
        data = record.data_json
        declaration = data.get("declaration_span")
        definition_key = data.get("definition_key")
        definition_identifiers = [
            row
            for row in bundle.identifiers
            if row.unit_id == uid and row.namespace == "code.definition_key"
        ]
        if (
            data.get("parent_unit_id") != unit.parent_id
            or data.get("relative_path") != file.relative_path
            or data.get("language") != file.language
            or data.get("adapter_fingerprint") != file.adapter_fingerprint
            or not isinstance(definition_key, str)
            or len(definition_identifiers) != 1
            or definition_identifiers[0].normalized_value != definition_key
            or definition_identifiers[0].original_value != definition_key
            or not isinstance(declaration, Mapping)
            or not _retained_span_matches(declaration, locator, local_map)
        ):
            raise ValueError("Retained code declaration binding differs")

        contexts = data.get("context_spans")
        if not isinstance(contexts, tuple):
            raise TypeError("Retained code context binding differs")
        for context in contexts:
            if not isinstance(context, Mapping):
                raise TypeError("Retained code context binding differs")
            source_id = context.get("source_unit_id")
            source = units.get(cast(str, source_id))
            if source is None or source.unit_id not in code_maps or not source.text:
                raise ValueError("Retained code context binding differs")
            source_locator = locators[source.unit_id]
            source_key = source_locator.artifact_ref, source_locator.relative_path
            start, end = context.get("char_start"), context.get("char_end")
            original = context.get("original_file_locator")
            dependencies = context.get("dependency_unit_ids")
            if (
                source_key != (file.artifact_ref, file.relative_path)
                or not isinstance(start, int)
                or not isinstance(end, int)
                or not 0 <= start < end <= len(source.text)
                or not isinstance(original, Mapping)
                or original.get("char_start") != source_locator.char_start + start
                or original.get("char_end") != source_locator.char_start + end
                or not _retained_span_matches(
                    original, source_locator, code_maps[source.unit_id]
                )
                or not isinstance(dependencies, tuple)
                or len(dependencies) != len(set(dependencies))
                or context.get("reason")
                not in {"parent_declaration", "referenced_import"}
                or context.get("reason") == "parent_declaration"
                and source.unit_id != unit.parent_id
            ):
                raise ValueError("Retained code context binding differs")
            original_start = source_locator.char_start + start
            original_end = source_locator.char_start + end
            expected_dependencies = {
                candidate_id
                for candidate_id, candidate_locator in by_file[source_key]
                if candidate_locator.char_start < original_end
                and original_start < candidate_locator.char_end
            }
            expected_dependencies.add(source.unit_id)
            if set(dependencies) != expected_dependencies:
                raise ValueError("Retained code context dependencies differ")


_BUNDLE_INDEXES: dict[int, tuple[weakref.ref[Any], _RetainedIndex]] = {}


def _bundle_index(bundle: RepresentationRows) -> _RetainedIndex:
    """Validate and index one immutable row bundle once for the bundle object's lifetime.

    Code excerpt projection runs once per citation; rebuilding the index there costs a
    full revalidation of every unit in the extraction, which for a repository source
    is the whole repository. Bundles are frozen, so the index of one object never
    changes; it is keyed by object identity (not by value, which would hash the whole
    bundle) and dropped when the bundle is collected.
    """
    key = id(bundle)
    cached = _BUNDLE_INDEXES.get(key)
    if cached is not None and cached[0]() is bundle:
        return cached[1]
    index = _build_bundle_index(bundle)
    _BUNDLE_INDEXES[key] = (weakref.ref(bundle), index)
    weakref.finalize(bundle, _BUNDLE_INDEXES.pop, key, None)
    return index


def _build_bundle_index(bundle: RepresentationRows) -> _RetainedIndex:
    units = _validated_units(bundle)
    _validate_locator_rows(bundle, units)
    code_files = _code_files(bundle)
    retained_code_files = {} if code_files else _retained_code_files(bundle, units)
    code_maps = _validated_unit_locators(bundle, code_files, retained_code_files)
    if retained_code_files:
        _validate_retained_code_structure(units, retained_code_files, code_maps)
        _validate_retained_code_records(bundle, units, retained_code_files, code_maps)
    if any(record.unit_id not in units for record in bundle.records):
        raise ValueError("Canonical record belongs outside extraction")
    assets = {asset.asset_id: asset for asset in bundle.assets}
    if len(assets) != len(bundle.assets) or any(
        asset.extraction_id != bundle.extraction.extraction_id
        for asset in bundle.assets
    ):
        raise ValueError("Duplicate canonical asset identity")
    proxies = _validated_proxies(bundle, units, assets)
    asset_owners = _validated_asset_owners(bundle, units, assets)
    document_parts = _validated_documents(bundle)
    return _RetainedIndex(
        units=units,
        assets=assets,
        proxies=proxies,
        asset_owners={
            asset_id: frozenset(owner_ids)
            for asset_id, owner_ids in asset_owners.items()
        },
        code_maps=code_maps,
        document_parts=document_parts,
        locator_overlaps=_locator_overlaps(units),
        part_overlaps=_part_overlaps(document_parts),
    )


def _validated_units(bundle: RepresentationRows) -> dict[str, EvidenceUnit]:
    """Index the units by id, requiring one extraction, one revision and true text hashes."""
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
    return units


def _validate_locator_rows(
    bundle: RepresentationRows, units: Mapping[str, EvidenceUnit]
) -> None:
    """Require the stored locator rows to reproduce each unit's ordered locators."""
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


def _validated_unit_locators(
    bundle: RepresentationRows,
    code_files: Mapping[tuple[str, str], CodeFileDescriptor],
    retained_code_files: Mapping[tuple[str, str], _RetainedCodeFile],
) -> dict[str, _LocalCodeMap]:
    """Validate every native and document locator; return the code map of each code unit."""
    code_maps: dict[str, _LocalCodeMap] = {}
    for unit in bundle.units:
        for locator in unit.locators:
            if not isinstance(locator, (TextLocator, CodeLocator)):
                continue
            local_map = _validate_locator(
                bundle, unit, locator, code_files, retained_code_files
            )
            if local_map is None:
                continue
            if unit.unit_id in code_maps:
                raise ValueError("Code unit requires one retained file locator")
            code_maps[unit.unit_id] = local_map
        _validate_document_locators(bundle, unit)
    return code_maps


def _validate_document_locators(bundle: RepresentationRows, unit: EvidenceUnit) -> None:
    """Require document locators to bind the retained original and stay inside the unit."""
    for locator in unit.locators:
        if isinstance(locator, DocumentLocator) and (
            locator.artifact_ref != bundle.revision.original_sha
            or locator.native_ref != bundle.source.source_key
            or locator.char_end > len(unit.text)
        ):
            raise ValueError("Document locator retained binding differs")


def _validated_proxies(
    bundle: RepresentationRows,
    units: Mapping[str, EvidenceUnit],
    assets: Mapping[str, AssetRow],
) -> dict[str, AssetProxyRow]:
    """Index the asset proxies, requiring original source ownership and one row per proxy unit."""
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
    return proxies


def _validated_asset_owners(
    bundle: RepresentationRows,
    units: Mapping[str, EvidenceUnit],
    assets: Mapping[str, AssetRow],
) -> dict[str, set[str]]:
    """Collect the units that own each asset as source or caption, rejecting repeat links."""
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
    return asset_owners


def _validated_documents(
    bundle: RepresentationRows,
) -> dict[str, tuple[DocumentSourcePart, ...]]:
    """Read the document source parts of each unit, requiring one provenance per domain."""
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
    return documents


def _source_domain(locator: object) -> tuple[str, str, str] | None:
    """The source slice a text or code locator addresses; only equal domains overlap."""
    if isinstance(locator, TextLocator):
        return "text", locator.artifact_ref, locator.native_ref
    if isinstance(locator, CodeLocator):
        return "code", locator.artifact_ref, locator.relative_path
    return None


def _swept[K, V](
    domains: Iterable[Sequence[tuple[K, int, int, V]]],
) -> dict[K, tuple[V, ...]]:
    """For each key, the values of every same-domain range overlapping one of its ranges.

    Each domain holds ``(key, start, end, value)`` entries. A key's own nonempty
    ranges count as overlapping it. One sort-and-sweep per domain finds the pairs;
    each key's values keep their domain order, the order a pairwise scan yields.
    """
    tables = [tuple(entries) for entries in domains]
    hits: dict[K, list[tuple[int, int]]] = defaultdict(list)
    for number, entries in enumerate(tables):
        for position, (key, start, end, _) in enumerate(entries):
            if start < end:
                hits[key].append((number, position))
        ranges = [(start, end) for _, start, end, _ in entries]
        for left, right in overlapping_pairs(ranges):
            hits[entries[left][0]].append((number, right))
            hits[entries[right][0]].append((number, left))
    return {
        key: tuple(tables[number][position][3] for number, position in sorted(found))
        for key, found in hits.items()
    }


def _locator_overlaps(
    units: Mapping[str, EvidenceUnit],
) -> dict[str, tuple[_LocatorPair, ...]]:
    """For each unit, every same-domain source locator overlapping one of its own."""
    domains: dict[tuple[str, str, str], list[tuple[str, int, int, _LocatorPair]]] = (
        defaultdict(list)
    )
    for owner in units.values():
        for candidate in owner.locators:
            domain = _source_domain(candidate)
            if domain is not None:
                assert isinstance(candidate, (TextLocator, CodeLocator))
                domains[domain].append(
                    (
                        owner.unit_id,
                        candidate.char_start,
                        candidate.char_end,
                        (owner, candidate),
                    )
                )
    return _swept(domains.values())


def _part_overlaps(
    document_parts: Mapping[str, tuple[DocumentSourcePart, ...]],
) -> dict[tuple[str, int], tuple[_PartPair, ...]]:
    """For each unit part, every same-domain part overlapping it, itself included."""
    domains: dict[str, list[tuple[tuple[str, int], int, int, _PartPair]]] = defaultdict(
        list
    )
    for owner_id, parts in document_parts.items():
        for position, part in enumerate(parts):
            domains[part.source_domain].append(
                (
                    (owner_id, position),
                    part.source_start,
                    part.source_end,
                    (owner_id, part),
                )
            )
    return _swept(domains.values())


def project_code_excerpt_locator(
    bundle: RepresentationRows, unit_id: str, start: int, end: int
) -> CodeLocator:
    """Project a unit-local excerpt through its validated original-file code map."""
    index = _bundle_index(bundle)
    unit = index.units.get(unit_id)
    if unit is None or not 0 <= start < end <= len(unit.text):
        raise ValueError("Code excerpt is outside its retained unit")
    locator = _one_code_locator(unit)
    if locator is None:
        raise ValueError("Code excerpt has no retained file locator")
    char_start = locator.char_start + start
    char_end = locator.char_start + end
    descriptor = next(
        (
            file
            for file in bundle.code_files
            if (file.artifact_ref, file.relative_path)
            == (locator.artifact_ref, locator.relative_path)
        ),
        None,
    )
    if descriptor is not None:
        byte_start = descriptor.codepoint_to_byte[char_start]
        byte_end = descriptor.codepoint_to_byte[char_end]
        line_start = descriptor.byte_to_line[byte_start]
        line_end = descriptor.byte_to_line[max(byte_start, byte_end - 1)]
    else:
        local_map = index.code_maps.get(unit_id)
        if local_map is None:
            raise ValueError("Code excerpt has no validated retained map")
        byte_start = locator.byte_start + local_map.byte_offsets[start]
        byte_end = locator.byte_start + local_map.byte_offsets[end]
        line_start = locator.line_start + local_map.line_offsets[start]
        line_end = locator.line_start + local_map.line_offsets[end - 1]
    return locator.model_copy(
        update={
            "char_start": char_start,
            "char_end": char_end,
            "byte_start": byte_start,
            "byte_end": byte_end,
            "line_start": line_start,
            "line_end": line_end,
        }
    )


def _locator_intervals(
    target: EvidenceUnit, index: _RetainedIndex
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
    for owner, candidate in index.locator_overlaps.get(target.unit_id, ()):
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
            if (
                isinstance(locator, CodeLocator)
                and isinstance(candidate, CodeLocator)
                and target.unit_id in index.code_maps
            ):
                target_map = index.code_maps[target.unit_id]
                owner_map = index.code_maps[owner.unit_id]
                target_start = left - locator.char_start
                target_end = right - locator.char_start
                owner_start = left - candidate.char_start
                owner_end = right - candidate.char_start
                if (
                    locator.byte_start + target_map.byte_offsets[target_start]
                    != candidate.byte_start + owner_map.byte_offsets[owner_start]
                    or locator.byte_start + target_map.byte_offsets[target_end]
                    != candidate.byte_start + owner_map.byte_offsets[owner_end]
                    or locator.line_start + target_map.line_offsets[target_start]
                    != candidate.line_start + owner_map.line_offsets[owner_start]
                    or locator.line_start + target_map.line_offsets[target_end - 1]
                    != candidate.line_start + owner_map.line_offsets[owner_end - 1]
                ):
                    raise ValueError("Retained code overlap coordinate mapping differs")
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
    for position, part in enumerate(target_parts):
        for owner_id, candidate in index.part_overlaps.get(
            (target.unit_id, position), ()
        ):
            owner = index.units[owner_id]
            left = max(part.source_start, candidate.source_start)
            right = min(part.source_end, candidate.source_end)
            target_start = part.unit_start + left - part.source_start
            owner_start = candidate.unit_start + left - candidate.source_start
            length = right - left
            if (
                target.text[target_start : target_start + length]
                != owner.text[owner_start : owner_start + length]
            ):
                raise ValueError("Retained document source slices differ across owners")
            result.append((target_start, target_start + length, owner.unit_id))
    return result


def _partition(
    unit: EvidenceUnit,
    revision_id: str,
    overlaps: list[tuple[int, int, str]],
) -> tuple[OwnedInterval, ...]:
    """Cut the unit text at every owner boundary; each piece depends on the owners open over it.

    One sweep over the sorted boundaries opens and closes owners, so the cost is
    the boundaries' sort plus the size of the dependency sets produced.
    """
    opening: dict[int, list[str]] = defaultdict(list)
    closing: dict[int, list[str]] = defaultdict(list)
    boundaries = {0, len(unit.text)}
    for start, end, owner in overlaps:
        if not 0 <= start < end <= len(unit.text):
            raise ValueError("Canonical owner interval exceeds unit text")
        boundaries.update((start, end))
        opening[start].append(owner)
        closing[end].append(owner)
    open_owners: dict[str, int] = defaultdict(int)
    intervals = []
    for start, end in pairwise(sorted(boundaries)):
        for owner in closing.get(start, ()):
            open_owners[owner] -= 1
            if not open_owners[owner]:
                del open_owners[owner]
        for owner in opening.get(start, ()):
            open_owners[owner] += 1
        intervals.append(
            OwnedInterval(
                char_start=start,
                char_end=end,
                dependency_ids=frozenset({revision_id, unit.unit_id, *open_owners}),
            )
        )
    return tuple(intervals)


def _representation(
    bundle: RepresentationRows,
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
    bundle: RepresentationRows,
    index: _RetainedIndex,
    unit: EvidenceUnit,
) -> RepresentationUnit:
    if not unit.text:
        ownership: tuple[OwnedInterval, ...] = ()
    else:
        overlaps = _document_intervals(unit, index)
        if overlaps is None:
            overlaps = _locator_intervals(unit, index)
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
    bundle: RepresentationRows,
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


def prepare_extraction(bundle: RepresentationRows) -> PreparedExtraction:
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


class ChunkSpan(NamedTuple):
    """The range one chunk covers in its unit."""

    chunk_id: str
    unit_id: str
    char_start: int
    char_end: int


def _source_ranges(
    units: Iterable[EvidenceUnit],
) -> Iterator[tuple[tuple[str, str], int, int, str]]:
    """Each text or code locator as its source key, its range and its unit."""
    for unit in units:
        for locator in unit.locators:
            if isinstance(locator, (TextLocator, CodeLocator)):
                key = (locator.artifact_ref, locator.native_ref)
                yield key, locator.char_start, locator.char_end, unit.unit_id


def _point_owners(units: Iterable[EvidenceUnit]) -> dict[str, set[str]]:
    """Units whose source range strictly contains the position of an empty locator.

    Only empty units read these owners, and an empty unit's locators are empty.
    """
    spans: dict[tuple[str, str], list[tuple[int, int, str]]] = defaultdict(list)
    points: list[tuple[tuple[str, str], int, str]] = []
    for key, start, end, owner_id in _source_ranges(units):
        if start < end:
            spans[key].append((start, end, owner_id))
        else:
            points.append((key, start, owner_id))
    ordered = {key: sorted(items) for key, items in spans.items()}
    starts = {key: [start for start, _, _ in items] for key, items in ordered.items()}
    owners: dict[str, set[str]] = defaultdict(set)
    for key, point, point_id in points:
        before = bisect_left(starts.get(key, []), point)
        owners[point_id].update(
            owner for _, end, owner in ordered.get(key, [])[:before] if end > point
        )
    return owners


def _unit_dependencies(
    unit: EvidenceUnit,
    ownership: tuple[OwnedInterval, ...],
    revision_id: str,
    proxy: AssetProxyRow | None,
    point_owners: Mapping[str, set[str]],
) -> frozenset[str]:
    """A unit depends on its intervals' owners, else on its declared or containing owners."""
    covered = frozenset(
        dep for interval in ownership for dep in interval.dependency_ids
    )
    if covered:
        return covered
    declared = unit.attributes.get(
        "overlapping_owner_ids", unit.attributes.get("range_dependency_ids")
    )
    owners = (
        set(cast(tuple[str, ...], declared))
        if declared is not None
        else set(point_owners.get(unit.unit_id) or {unit.unit_id})
    )
    owners.add(unit.unit_id)
    if proxy is not None:
        owners.add(proxy.asset_id)
        owners.update(proxy.required_unit_ids)
    return frozenset({revision_id, *owners})


def _covered_dependencies(
    ownership: tuple[OwnedInterval, ...], ends: Sequence[int], start: int, end: int
) -> frozenset[str]:
    """The dependencies of the intervals a range touches; ownership partitions in order.

    ``ends`` holds each interval's end, so the first touched interval is a bisection.
    """
    first = bisect_right(ends, start)
    covered: set[str] = set()
    for interval in ownership[first:]:
        if interval.char_start >= end:
            break
        covered.update(interval.dependency_ids)
    return frozenset(covered)


def _interval_rows(
    ownership: Mapping[str, tuple[OwnedInterval, ...]],
    owners: dict[str, frozenset[str]],
) -> list[IntervalRow]:
    """One row per owned interval; its dependency set is added as an owner keyed by digest."""
    rows = []
    for owner_id, owned in ownership.items():
        for interval in owned:
            key = dependency_digest(interval.dependency_ids)
            owners.setdefault(key, interval.dependency_ids)
            rows.append(
                IntervalRow(
                    unit_id=owner_id,
                    char_start=interval.char_start,
                    char_end=interval.char_end,
                    dependency_digest=key,
                )
            )
    return rows


def representation_facts(
    rows: RepresentationRows,
    prepared: PreparedExtraction,
    chunks: Iterable[ChunkSpan],
) -> RepresentationFacts:
    """Every dependency set the units, chunks, assets and intervals of one extraction need.

    A chunk depends on the owners of the intervals it covers; an asset on itself
    and every unit linked to it; an interval's set is stored under its digest.
    """
    revision = rows.revision.revision_id
    ownership = {item.unit.unit_id: item.ownership for item in prepared.units}
    proxies = {proxy.proxy_unit_id: proxy for proxy in rows.proxies}
    points = _point_owners(rows.units)
    units = {
        unit.unit_id: _unit_dependencies(
            unit, ownership[unit.unit_id], revision, proxies.get(unit.unit_id), points
        )
        for unit in rows.units
    }
    owners: dict[str, frozenset[str]] = dict(units)
    ends = {
        owner_id: [interval.char_end for interval in owned]
        for owner_id, owned in ownership.items()
    }
    for chunk in chunks:
        owners[chunk.chunk_id] = (
            _covered_dependencies(
                ownership[chunk.unit_id],
                ends[chunk.unit_id],
                chunk.char_start,
                chunk.char_end,
            )
            or units[chunk.unit_id]
        )
    linked: dict[str, set[str]] = defaultdict(set)
    for link in rows.unit_assets:
        linked[link.asset_id].add(link.unit_id)
    for asset in rows.assets:
        owners[asset.asset_id] = frozenset(
            {revision, asset.asset_id}.union(
                *(units[unit_id] for unit_id in linked[asset.asset_id])
            )
        )
    intervals = _interval_rows(ownership, owners)
    return RepresentationFacts(
        extraction_id=rows.extraction.extraction_id,
        dependencies=tuple(
            OwnerDependencies(owner_id=owner, dependency_ids=deps)
            for owner, deps in sorted(owners.items())
        ),
        intervals=tuple(intervals),
    )
