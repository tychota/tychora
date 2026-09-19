"""Retained repository code normalization and pure identity/range kernels."""

import base64
import json
from collections import defaultdict
from collections.abc import Mapping
from hashlib import sha256
from io import BytesIO
from itertools import pairwise
from typing import Any, cast

from tychora.adapters.files.assertions import verify_assertion_closure
from tychora.adapters.files.references import MAX_MANIFEST_BYTES, required_refs
from tychora.configuration.registry import Registry
from tychora.contracts.artifacts import ArtifactRef, ManifestRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.code import (
    CODE_RESOLVER_FINGERPRINT,
    CapturedSyntax,
    CatalogDefinition,
    CodeExtractionArtifact,
    CodeRegistration,
    ImportBinding,
    OwnedRange,
    Resolution,
    SymbolCatalog,
    code_extraction_document,
    retained_syntax_data,
)
from tychora.contracts.corpus import (
    ExtractionBundle,
    ExtractionRow,
    FacetRow,
    IdentifierRow,
    LocatorRow,
    RevisionArtifactRow,
    StructuredRecordRow,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import CodeLocator, EvidenceUnit
from tychora.contracts.identity import (
    canonical_bytes,
    digest,
    extraction_id,
    normalize_source_key,
    unit_id,
)
from tychora.contracts.relationships import (
    AssertionObservation,
    RelationshipAssertion,
    RelationshipEndpoint,
    assertion_index_document,
    partition_assertions,
)
from tychora.ports.artifacts import ArtifactReader, Artifacts
from tychora.ports.language import Definition, Reference, SourceSpan, SyntaxResult

from .resolve import resolve


def _partition_document(values: list[dict]) -> bytes:
    return canonical_bytes(
        {
            "type": "extraction",
            "schema_version": "1",
            "required_artifacts": [],
            "required_manifests": [],
            "data": {"files": values},
        }
    )


def _partition_files(files: tuple[CapturedSyntax, ...]) -> tuple[bytes, ...]:
    parts: list[bytes] = []
    current: list[dict] = []
    encoded_items = 0
    empty_size = len(_partition_document([]))
    for item in files:
        value = retained_syntax_data(item)
        item_size = len(canonical_bytes(value))
        candidate_size = empty_size + encoded_items + item_size + len(current)
        if len(current) + 1 > 4000 or candidate_size > MAX_MANIFEST_BYTES:
            if not current:
                raise ValueError("One retained syntax fact exceeds manifest capacity")
            parts.append(_partition_document(current))
            current = [value]
            encoded_items = item_size
            if empty_size + item_size > MAX_MANIFEST_BYTES:
                raise ValueError("One retained syntax fact exceeds manifest capacity")
        else:
            current.append(value)
            encoded_items += item_size
    if current:
        parts.append(_partition_document(current))
    if len(parts) > 10_000:
        raise ValueError("Retained syntax facts exceed manifest-link capacity")
    return tuple(parts)


def definition_key(
    revision_id: str,
    path: str,
    language: str,
    qualified_name: str,
    symbol_kind: str,
    discriminator: str | int,
) -> str:
    path = normalize_source_key(path)
    return digest(
        "code-definition-v1",
        revision_id,
        path,
        language,
        qualified_name,
        symbol_kind,
        discriminator,
    )


def range_dependencies(
    spans: tuple[OwnedRange, ...],
) -> Mapping[str, frozenset[str]]:
    """Return complete half-open overlap ownership within each original file."""
    dependencies: dict[str, set[str]] = defaultdict(set)
    grouped: dict[tuple[str, str], list[OwnedRange]] = defaultdict(list)
    for span in spans:
        grouped[(span.artifact_ref, span.relative_path)].append(span)
        dependencies[span.owner_unit_id].add(span.owner_unit_id)
    for ranges in grouped.values():
        active: list[OwnedRange] = []
        for current in sorted(
            ranges,
            key=lambda item: (item.char_start, item.char_end, item.owner_unit_id),
        ):
            active = [item for item in active if item.char_end > current.char_start]
            for item in active:
                dependencies[current.owner_unit_id].add(item.owner_unit_id)
                dependencies[item.owner_unit_id].add(current.owner_unit_id)
            active.append(current)
    return {owner: frozenset(values) for owner, values in dependencies.items()}


def _fingerprints(
    registration: CodeRegistration,
    files: tuple[CapturedSyntax, ...],
    registry: Registry,
) -> tuple[str, str]:
    parser = digest(
        "code-parser-v1",
        [
            [
                item.captured.relative_path,
                item.syntax.adapter_fingerprint,
                item.syntax.options,
                item.syntax.coverage,
            ]
            for item in files
        ],
    )
    kind = cast(Mapping[str, Any], registry.kinds[registration.revision.kind_id])
    normalizer = digest(
        "code-normalizer-v1",
        kind,
        registry.schema_digests,
        CODE_RESOLVER_FINGERPRINT,
    )
    return parser, normalizer


def _artifact_ref(payload: bytes, media_type: str) -> ArtifactRef:
    identity = sha256(payload).hexdigest()
    return ArtifactRef(
        sha256=identity,
        size_bytes=len(payload),
        media_type=media_type,
        storage_key=identity,
    )


def _locator(item: CapturedSyntax, start: int, end: int) -> CodeLocator:
    syntax = item.syntax
    return CodeLocator(
        artifact_ref=item.captured.artifact_ref.sha256,
        native_ref=item.captured.relative_path,
        relative_path=item.captured.relative_path,
        char_start=start,
        char_end=end,
        byte_start=syntax.byte_offset(start),
        byte_end=syntax.byte_offset(end),
        line_start=syntax.line_for_byte(syntax.byte_offset(start)),
        line_end=syntax.line_for_byte(
            max(syntax.byte_offset(start), syntax.byte_offset(end) - 1)
        ),
    )


def _span_data(span: SourceSpan) -> dict[str, object]:
    return {
        "char_start": span.char_start,
        "char_end": span.char_end,
        "byte_start": span.byte_start,
        "byte_end": span.byte_end,
        "line_start": span.line_start,
        "line_end": span.line_end,
        "coordinate_domain": "original_file",
    }


def _native_ref(path: str, native_ref: str) -> str:
    return f"{path}#{native_ref}"


def _build(
    parsed: CodeExtractionArtifact, registry: Registry
) -> tuple[ExtractionBundle, tuple[RelationshipAssertion, ...], tuple[Resolution, ...]]:
    registration = parsed.registration
    rid = registration.revision.revision_id
    eid = extraction_id(rid, parsed.parser_fingerprint, parsed.normalizer_fingerprint)
    units: list[EvidenceUnit] = []
    locators: list[LocatorRow] = []
    records: list[StructuredRecordRow] = []
    facets: list[FacetRow] = []
    identifiers: list[IdentifierRow] = []
    descriptors = tuple(item.descriptor() for item in parsed.files)
    container_ids: dict[str, str] = {}
    definition_ids: dict[tuple[str, str], str] = {}
    definition_units: dict[str, tuple[CapturedSyntax, Definition]] = {}
    source_ranges: list[OwnedRange] = []

    def append_unit(unit: EvidenceUnit) -> None:
        units.append(unit)
        locators.extend(
            LocatorRow(
                locator_id=digest("locator-v1", unit.unit_id, ordinal),
                unit_id=unit.unit_id,
                fragment_ordinal=ordinal,
                locator=locator,
            )
            for ordinal, locator in enumerate(unit.locators)
        )

    for file_ordinal, item in enumerate(parsed.files):
        path, syntax = item.captured.relative_path, item.syntax
        container_native = _native_ref(path, "file")
        container_id = unit_id(eid, container_native, 0, sha256(b"").hexdigest())
        container_ids[path] = container_id
        append_unit(
            EvidenceUnit(
                unit_id=container_id,
                extraction_id=eid,
                parent_id=None,
                ordinal=len(units),
                unit_type="code_file",
                native_ref=container_native,
                boundary_key=path,
                text="",
                text_sha=sha256(b"").hexdigest(),
                citable=False,
                locators=(_locator(item, 0, 0),),
                attributes=FrozenJsonMap(
                    {
                        "relative_path": path,
                        "language": syntax.language,
                        "encoding": syntax.encoding,
                        "coverage": syntax.coverage,
                        "artifact_ref": item.captured.artifact_ref.sha256,
                        "capture_entry_digest": digest(
                            "captured-file-v1",
                            item.captured.model_dump(mode="json"),
                        ),
                        "adapter_fingerprint": syntax.adapter_fingerprint,
                    }
                ),
            )
        )
        for key, value in (("code.path", path), ("code.language", syntax.language)):
            facets.append(FacetRow(unit_id=container_id, facet_key=key, value=value))

        for ordinal, definition in enumerate(syntax.definitions_found, start=1):
            text = syntax.text[definition.char_start : definition.char_end]
            native = _native_ref(path, definition.native_ref)
            uid = unit_id(eid, native, ordinal, sha256(text.encode()).hexdigest())
            definition_ids[(path, definition.native_ref)] = uid
            definition_units[uid] = (item, definition)

        for ordinal, definition in enumerate(syntax.definitions_found, start=1):
            path = item.captured.relative_path
            uid = definition_ids[(path, definition.native_ref)]
            parent_id = (
                definition_ids.get((path, definition.parent_native_ref))
                if definition.parent_native_ref
                else container_ids[path]
            )
            text = syntax.text[definition.char_start : definition.char_end]
            locator = _locator(item, definition.char_start, definition.char_end)
            append_unit(
                EvidenceUnit(
                    unit_id=uid,
                    extraction_id=eid,
                    parent_id=parent_id,
                    ordinal=len(units),
                    unit_type="symbol",
                    native_ref=_native_ref(path, definition.native_ref),
                    boundary_key=path,
                    text=text,
                    text_sha=sha256(text.encode()).hexdigest(),
                    locators=(locator,),
                    attributes=FrozenJsonMap(
                        {"relative_path": path, "language": syntax.language}
                    ),
                )
            )
            source_ranges.append(
                OwnedRange(
                    owner_unit_id=uid,
                    artifact_ref=item.captured.artifact_ref.sha256,
                    relative_path=path,
                    char_start=definition.char_start,
                    char_end=definition.char_end,
                    byte_start=definition.byte_start,
                    byte_end=definition.byte_end,
                )
            )
            for key, value in (
                ("code.path", path),
                ("code.language", syntax.language),
                ("code.symbol_kind", definition.kind),
                ("code.qualified_name", definition.qualified_name),
            ):
                facets.append(FacetRow(unit_id=uid, facet_key=key, value=value))

        start = len(syntax.definitions_found) + 1
        for offset, span in enumerate(syntax.uncovered_spans, start=start):
            text = syntax.text[span.char_start : span.char_end]
            native = _native_ref(path, span.native_ref)
            uid = unit_id(eid, native, offset, sha256(text.encode()).hexdigest())
            locator = _locator(item, span.char_start, span.char_end)
            append_unit(
                EvidenceUnit(
                    unit_id=uid,
                    extraction_id=eid,
                    parent_id=container_ids[path],
                    ordinal=len(units),
                    unit_type="code",
                    native_ref=native,
                    boundary_key=path,
                    text=text,
                    text_sha=sha256(text.encode()).hexdigest(),
                    citable=bool(text),
                    locators=(locator,),
                    attributes=FrozenJsonMap(
                        {"relative_path": path, "language": syntax.language}
                    ),
                )
            )
            source_ranges.append(
                OwnedRange(
                    owner_unit_id=uid,
                    artifact_ref=item.captured.artifact_ref.sha256,
                    relative_path=path,
                    char_start=span.char_start,
                    char_end=span.char_end,
                    byte_start=span.byte_start,
                    byte_end=span.byte_end,
                )
            )
            for key, value in (("code.path", path), ("code.language", syntax.language)):
                facets.append(FacetRow(unit_id=uid, facet_key=key, value=value))

    dependencies = range_dependencies(tuple(source_ranges))
    units = [
        unit.model_copy(
            update={
                "attributes": FrozenJsonMap(
                    {
                        **dict(unit.attributes),
                        "range_dependency_ids": sorted(dependencies[unit.unit_id]),
                    }
                )
            }
        )
        if unit.unit_id in dependencies
        else unit
        for unit in units
    ]
    schema_bindings = cast(
        Mapping[str, Mapping[str, Mapping[str, str]]],
        registry.artifact["definition_schema_bindings"],
    )
    schema_id = schema_bindings["kinds"][registration.revision.kind_id]["record_schema"]
    for uid, (item, definition) in definition_units.items():
        path, syntax = item.captured.relative_path, item.syntax
        contexts = []
        for context in definition.context:
            candidates = [
                unit_range
                for unit_range in source_ranges
                if unit_range.relative_path == path
                and unit_range.char_start <= context.char_start
                and context.char_end <= unit_range.char_end
            ]
            if not candidates:
                raise TychoraError("INTEGRITY_ERROR", "Code context has no source unit")
            source_range = min(
                candidates,
                key=lambda value: (
                    value.char_end - value.char_start,
                    value.owner_unit_id,
                ),
            )
            context_dependencies = frozenset(
                other.owner_unit_id
                for other in source_ranges
                if other.relative_path == path
                and other.artifact_ref == source_range.artifact_ref
                and other.char_start < context.char_end
                and context.char_start < other.char_end
            ) | {source_range.owner_unit_id}
            contexts.append(
                {
                    "source_unit_id": source_range.owner_unit_id,
                    "char_start": context.char_start - source_range.char_start,
                    "char_end": context.char_end - source_range.char_start,
                    "original_file_locator": {
                        "char_start": context.char_start,
                        "char_end": context.char_end,
                        "byte_start": context.byte_start,
                        "byte_end": context.byte_end,
                        "line_start": context.line_start,
                        "line_end": context.line_end,
                        "coordinate_domain": "original_file",
                    },
                    "reason": context.reason,
                    "dependency_unit_ids": sorted(context_dependencies),
                }
            )
        discriminator = (
            definition.overload_discriminator
            if definition.overload_discriminator is not None
            else definition.byte_start
        )
        key = definition_key(
            rid,
            path,
            syntax.language,
            definition.qualified_name,
            definition.kind,
            discriminator,
        )
        records.append(
            StructuredRecordRow(
                unit_id=uid,
                record_type="code_symbol",
                schema_hash=cast(str, registry.schema_digests[schema_id]),
                data_json=FrozenJsonMap(
                    {
                        "relative_path": path,
                        "language": syntax.language,
                        "symbol_kind": definition.kind,
                        "name": definition.name,
                        "qualified_name": definition.qualified_name,
                        "declaration_span": _span_data(definition.declaration_span),
                        "parent_unit_id": units[
                            [u.unit_id for u in units].index(uid)
                        ].parent_id,
                        "overload_discriminator": str(definition.overload_discriminator)
                        if definition.overload_discriminator is not None
                        else None,
                        "definition_key": key,
                        "context_spans": contexts,
                        "adapter_fingerprint": syntax.adapter_fingerprint,
                    }
                ),
            )
        )
        identifiers.extend(
            (
                IdentifierRow(
                    namespace="code.definition_key",
                    normalized_value=key,
                    original_value=key,
                    unit_id=uid,
                ),
                IdentifierRow(
                    namespace="code.qualified_name",
                    normalized_value=definition.qualified_name,
                    original_value=definition.qualified_name,
                    unit_id=uid,
                ),
                IdentifierRow(
                    namespace="code.short_name",
                    normalized_value=definition.name,
                    original_value=definition.name,
                    unit_id=uid,
                ),
            )
        )

    assertions: list[RelationshipAssertion] = []
    by_parent: dict[str, list[EvidenceUnit]] = defaultdict(list)
    unit_by_id = {unit.unit_id: unit for unit in units}
    for unit in units:
        if unit.unit_type == "symbol" and unit.parent_id is not None:
            by_parent[unit.parent_id].append(unit)
            parent = unit_by_id[unit.parent_id]
            assertions.append(_structural_assertion(parsed, parent, unit, "contains@1"))
    for siblings in by_parent.values():
        siblings.sort(
            key=lambda unit: (
                cast(CodeLocator, unit.locators[0]).byte_start,
                unit.unit_id,
            )
        )
        assertions.extend(
            _structural_assertion(parsed, before, after, "precedes@1")
            for before, after in pairwise(siblings)
        )

    definitions = tuple(
        CatalogDefinition(
            target_id=uid,
            revision_id=rid,
            relative_path=item.captured.relative_path,
            language=item.syntax.language,
            name=definition.name,
            qualified_name=definition.qualified_name,
            symbol_kind=definition.kind,
            native_ref=definition.native_ref,
            parent_native_ref=definition.parent_native_ref,
        )
        for uid, (item, definition) in definition_units.items()
    )
    resolutions = []
    resolved_sources: list[tuple[CapturedSyntax, Reference, Resolution]] = []
    for item in parsed.files:
        imports = []
        for reference in item.syntax.references_found:
            if reference.form != "import" or not reference.local_name:
                continue
            names = {reference.import_name, *reference.candidate_names}
            targets = tuple(
                definition.target_id
                for definition in definitions
                if definition.name in names or definition.qualified_name in names
            )
            if targets:
                imports.append(
                    ImportBinding(
                        relative_path=item.captured.relative_path,
                        source_scope_ref=reference.source_scope_ref,
                        local_name=reference.local_name,
                        target_revision_id=rid,
                        target_ids=targets,
                    )
                )
        catalog = SymbolCatalog(
            source_id=registration.source.source_id,
            revision_id=rid,
            source_relative_path=item.captured.relative_path,
            source_language=item.syntax.language,
            source_adapter_fingerprint=item.syntax.adapter_fingerprint,
            source_container_id=container_ids[item.captured.relative_path],
            source_ranges=tuple(
                owned
                for owned in source_ranges
                if owned.relative_path == item.captured.relative_path
                and owned.artifact_ref == item.captured.artifact_ref.sha256
            ),
            definitions=definitions,
            imports=tuple(imports),
        )
        file_resolutions = resolve(item.syntax.references_found, catalog)
        resolutions.extend(file_resolutions)
        resolved_sources.extend(
            (item, reference, resolution)
            for reference, resolution in zip(
                item.syntax.references_found, file_resolutions, strict=True
            )
        )

    semantic_predicates = {
        "call": "calls@1",
        "jsx": "calls@1",
        "import": "imports@1",
        "export": "imports@1",
        "inheritance": "inherits@1",
    }
    for item, reference, resolution in resolved_sources:
        predicate = semantic_predicates.get(reference.form)
        if (
            predicate is None
            or not resolution.supported
            or resolution.state != "resolved"
            or resolution.selected_target is None
        ):
            continue
        subject = unit_by_id[resolution.source_unit_id]
        target = unit_by_id[resolution.selected_target]
        assertions.append(
            _semantic_assertion(
                parsed,
                item,
                reference,
                resolution,
                subject,
                target,
                predicate,
            )
        )

    refs = {
        ref.sha256: ref
        for ref in (
            registration.capture.original_ref,
            parsed.raw_document_ref,
            *(entry.artifact_ref for entry in registration.capture.entries),
            parsed.assertions_ref,
            parsed.diagnostics_ref,
            *(link.artifact for link in parsed.partition_refs),
            *(link.artifact for link in parsed.assertion_partition_refs),
        )
        if ref is not None
    }
    revision_artifacts = [
        RevisionArtifactRow(
            revision_id=rid,
            role="original",
            source_ref=registration.source.source_key,
            artifact_sha=registration.capture.original_ref.sha256,
        )
    ]
    revision_artifacts.extend(
        RevisionArtifactRow(
            revision_id=rid,
            role="source_file",
            source_ref=entry.relative_path,
            artifact_sha=entry.artifact_ref.sha256,
        )
        for entry in registration.capture.entries
    )
    bundle = ExtractionBundle(
        workspace=registration.workspace,
        collection=registration.collection,
        source=registration.source,
        artifacts=tuple(refs.values()),
        revision=registration.revision,
        revision_artifacts=tuple(revision_artifacts),
        extraction=ExtractionRow(
            extraction_id=eid,
            revision_id=rid,
            parser_fingerprint=parsed.parser_fingerprint,
            normalizer_fingerprint=parsed.normalizer_fingerprint,
            document_artifact_sha=parsed.raw_document_ref.sha256,
            assertions_artifact_sha=parsed.assertions_ref.sha256
            if parsed.assertions_ref
            else None,
            diagnostics_artifact_sha=parsed.diagnostics_ref.sha256
            if parsed.diagnostics_ref
            else None,
            created_at=registration.revision.captured_at,
        ),
        units=tuple(units),
        locators=tuple(locators),
        records=tuple(records),
        facets=tuple(facets),
        identifiers=tuple(identifiers),
        code_files=descriptors,
    )
    return bundle, tuple(assertions), tuple(resolutions)


def _structural_assertion(
    parsed: CodeExtractionArtifact,
    subject: EvidenceUnit,
    object_: EvidenceUnit,
    predicate: str,
) -> RelationshipAssertion:
    assert subject.locators and object_.locators
    observation = AssertionObservation(
        observation_key=f"{predicate}:{subject.unit_id}:{object_.unit_id}",
        revision_id=parsed.registration.revision.revision_id,
        extraction_id=subject.extraction_id,
        predicate=predicate,
        subject=RelationshipEndpoint(
            entity_id=subject.unit_id, unit_id=subject.unit_id, kind=subject.unit_type
        ),
        object=RelationshipEndpoint(
            entity_id=object_.unit_id, unit_id=object_.unit_id, kind=object_.unit_type
        ),
        origin_unit_id=subject.unit_id,
        origin_locator=subject.locators[0],
        required_support_ids=frozenset({subject.unit_id, object_.unit_id}),
        method="native",
        status="recorded",
        confidence=1.0,
        derivation_fingerprint=digest(
            "code-structure-v1", predicate, subject.unit_id, object_.unit_id
        ),
    )
    return RelationshipAssertion.from_observation(observation)


def _semantic_assertion(
    parsed: CodeExtractionArtifact,
    item: CapturedSyntax,
    reference: Reference,
    resolution: Resolution,
    subject: EvidenceUnit,
    object_: EvidenceUnit,
    predicate: str,
) -> RelationshipAssertion:
    observation = AssertionObservation(
        observation_key=f"{predicate}:{resolution.reference_id}",
        revision_id=parsed.registration.revision.revision_id,
        extraction_id=subject.extraction_id,
        predicate=predicate,
        subject=RelationshipEndpoint(
            entity_id=subject.unit_id, unit_id=subject.unit_id, kind=subject.unit_type
        ),
        object=RelationshipEndpoint(
            entity_id=object_.unit_id, unit_id=object_.unit_id, kind=object_.unit_type
        ),
        origin_unit_id=subject.unit_id,
        origin_locator=_locator(item, reference.char_start, reference.char_end),
        required_support_ids=frozenset({subject.unit_id, object_.unit_id}),
        method="parser_inferred",
        status="recorded",
        confidence=1.0,
        derivation_fingerprint=digest(
            "code-resolution-assertion-v1",
            resolution.reference_id,
            predicate,
            subject.unit_id,
            object_.unit_id,
            resolution.rule,
            parsed.resolver_fingerprint,
        ),
    )
    return RelationshipAssertion.from_observation(observation)


def retain_code_extraction(
    registration: CodeRegistration,
    files: tuple[CapturedSyntax, ...],
    registry: Registry,
    artifacts: Artifacts,
) -> CodeExtractionArtifact:
    registration = CodeRegistration.model_validate(registration.model_dump())
    files = tuple(CapturedSyntax.model_validate(item.model_dump()) for item in files)
    if tuple(item.captured for item in files) != registration.capture.entries:
        raise ValueError("Code syntax files differ from repository capture entries")
    parser, normalizer = _fingerprints(registration, files, registry)
    placeholder = CodeExtractionArtifact.model_construct(
        registration=registration,
        files=files,
        raw_document_ref=ArtifactRef(
            sha256="0" * 64,
            size_bytes=0,
            media_type="application/json",
            storage_key="0" * 64,
        ),
        parser_fingerprint=parser,
        normalizer_fingerprint=normalizer,
        resolver_fingerprint=CODE_RESOLVER_FINGERPRINT,
        registry_digest=registry.registry_digest,
        assertions_ref=None,
        diagnostics_ref=None,
        partition_refs=(),
        assertion_partition_refs=(),
    )
    preliminary = code_extraction_document(placeholder)
    prepared = placeholder.model_copy(
        update={"raw_document_ref": _artifact_ref(preliminary, "application/json")}
    )
    _, assertions, resolutions = _build(prepared, registry)
    assertion_ref = None
    assertion_partition_refs: tuple[ManifestRef, ...] = ()
    assertion_parts = partition_assertions(assertions)
    if len(assertion_parts) == 1:
        assertion_ref = artifacts.put_stream(
            BytesIO(assertion_parts[0]), "application/x-ndjson"
        )
    elif assertion_parts:
        leaves = tuple(
            ManifestRef(
                artifact=artifacts.put_stream(
                    BytesIO(part), "application/x-ndjson"
                ),
                type="assertion",
                schema_version="1",
            )
            for part in assertion_parts
        )
        index = assertion_index_document(
            extraction_id(
                registration.revision.revision_id, parser, normalizer
            ),
            leaves,
            assertions,
        )
        required_refs(json.loads(index), capture_published=True)
        assertion_ref = artifacts.put_stream(BytesIO(index), "application/json")
        assertion_partition_refs = leaves
    diagnostic_payload = json.dumps(
        {
            "type": "extraction",
            "schema_version": "1",
            "required_artifacts": [],
            "required_manifests": [],
            "data": {
                "resolutions": [item.model_dump(mode="json") for item in resolutions],
                "syntax": [
                    {
                        "relative_path": item.captured.relative_path,
                        "coverage": item.syntax.coverage,
                        "diagnostics": [
                            value.model_dump(mode="json")
                            for value in item.syntax.diagnostics
                        ],
                    }
                    for item in files
                ],
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    diagnostics_ref = artifacts.put_stream(
        BytesIO(diagnostic_payload), "application/json"
    )
    partition_refs = []
    inline = placeholder.model_copy(
        update={
            "assertions_ref": assertion_ref,
            "diagnostics_ref": diagnostics_ref,
            "assertion_partition_refs": assertion_partition_refs,
        }
    )
    inline_payload = code_extraction_document(inline)
    if len(files) > 4000 or len(inline_payload) > MAX_MANIFEST_BYTES:
        for child in _partition_files(files):
            required_refs(json.loads(child), capture_published=True)
            child_ref = artifacts.put_stream(BytesIO(child), "application/json")
            partition_refs.append(
                ManifestRef(artifact=child_ref, type="extraction", schema_version="1")
            )
    final = placeholder.model_copy(
        update={
            "assertions_ref": assertion_ref,
            "diagnostics_ref": diagnostics_ref,
            "partition_refs": tuple(partition_refs),
            "assertion_partition_refs": assertion_partition_refs,
        }
    )
    payload = code_extraction_document(final)
    required_refs(json.loads(payload), capture_published=True)
    raw_ref = artifacts.put_stream(BytesIO(payload), "application/json")
    return CodeExtractionArtifact.model_validate(
        final.model_copy(update={"raw_document_ref": raw_ref}).model_dump()
    )


def replay_code_extraction(
    manifest_ref: ArtifactRef,
    registration: CodeRegistration,
    artifacts: ArtifactReader,
) -> CodeExtractionArtifact:
    with artifacts.open_verified(manifest_ref) as stream:
        payload = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ValueError("Code extraction manifest exceeds byte capacity")
    if sha256(payload).hexdigest() != manifest_ref.sha256:
        raise ValueError("Code extraction manifest differs from retained reference")
    root = json.loads(payload)
    if root.get("type") != "extraction" or root.get("schema_version") != "1":
        raise ValueError("Unknown code extraction manifest")
    data = root["data"]
    if data["registration_fingerprint"] != digest(
        "code-registration-v1", registration.model_dump(mode="json")
    ):
        raise ValueError("Code replay registration differs")
    required_manifests = tuple(
        ManifestRef.model_validate_json(canonical_bytes(value))
        for value in root.get("required_manifests", [])
    )
    partition_refs = tuple(
        link for link in required_manifests if link.type == "extraction"
    )
    assertion_roots = tuple(
        link
        for link in required_manifests
        if link.type in ("assertion", "assertion_index")
    )
    if len(assertion_roots) > 1:
        raise ValueError("Code extraction has multiple assertion roots")
    stored_files = data["files"]
    if data.get("files_partitioned"):
        if stored_files or not partition_refs or len(partition_refs) > 10_000:
            raise ValueError("Malformed code extraction partition root")
        stored_files = []
        for link in partition_refs:
            with artifacts.open_verified(link.artifact) as stream:
                child_payload = stream.read(MAX_MANIFEST_BYTES + 1)
            if (
                len(child_payload) > MAX_MANIFEST_BYTES
                or sha256(child_payload).hexdigest() != link.artifact.sha256
                or len(child_payload) != link.artifact.size_bytes
            ):
                raise ValueError("Code extraction partition differs from its link")
            child = json.loads(child_payload)
            child_files = child.get("data", {}).get("files")
            if (
                child.get("type") != "extraction"
                or child.get("schema_version") != "1"
                or child.get("required_artifacts") != []
                or child.get("required_manifests") != []
                or not isinstance(child_files, list)
                or not 0 < len(child_files) <= 4000
            ):
                raise ValueError("Malformed code extraction partition")
            stored_files.extend(child_files)
    elif partition_refs or data.get("files_partitioned") not in (False, None):
        raise ValueError("Inline code extraction has unexpected partitions")
    if len(stored_files) > 90000:
        raise ValueError("Code extraction exceeds repository file capacity")
    entries = {entry.relative_path: entry for entry in registration.capture.entries}
    files = []
    for value in stored_files:
        captured = entries.get(value["captured"]["relative_path"])
        if captured is None or captured.model_dump(mode="json") != value["captured"]:
            raise ValueError("Code replay file binding differs")
        with artifacts.open_verified(captured.artifact_ref) as stream:
            file_bytes = stream.read()
        from .syntax import source_maps

        encoding = value["syntax"]["encoding"]
        try:
            text = file_bytes.decode(encoding)
            maps = source_maps(file_bytes, text, encoding)
        except (LookupError, UnicodeError) as error:
            raise ValueError("Code replay cannot reproduce decoded source") from error
        syntax = SyntaxResult.model_validate_json(
            canonical_bytes(
                {
                    **value["syntax"],
                    "file_bytes": base64.b64encode(file_bytes).decode("ascii"),
                    "text": text,
                    "codepoint_to_byte": maps[0],
                    "byte_to_codepoint": maps[1],
                    "line_starts": maps[2],
                    "byte_to_line": maps[3],
                }
            )
        )
        files.append(CapturedSyntax(captured=captured, syntax=syntax))
    assertions_ref = (
        ArtifactRef.model_validate(data["assertions_ref"])
        if data["assertions_ref"]
        else None
    )
    diagnostics_ref = (
        ArtifactRef.model_validate(data["diagnostics_ref"])
        if data["diagnostics_ref"]
        else None
    )
    assertion_partition_refs: tuple[ManifestRef, ...] = ()
    if assertions_ref is not None:
        if not assertion_roots or assertion_roots[0].artifact != assertions_ref:
            raise ValueError("Code assertion root differs from retained closure")
        closure = verify_assertion_closure(
            assertions_ref,
            artifacts,
            expected_extraction_id=extraction_id(
                registration.revision.revision_id,
                data["parser_fingerprint"],
                data["normalizer_fingerprint"],
            ),
        )
        if assertion_roots[0].type == "assertion_index":
            assertion_partition_refs = tuple(
                ManifestRef(
                    artifact=leaf.artifact,
                    type="assertion",
                    schema_version="1",
                )
                for leaf in closure.leaves
            )
    elif assertion_roots:
        raise ValueError("Code extraction closure has an unexpected assertion root")
    if diagnostics_ref is not None:
        with artifacts.open_verified(diagnostics_ref):
            pass
    return CodeExtractionArtifact(
        registration=registration,
        files=tuple(files),
        raw_document_ref=manifest_ref,
        parser_fingerprint=data["parser_fingerprint"],
        normalizer_fingerprint=data["normalizer_fingerprint"],
        resolver_fingerprint=data["resolver_fingerprint"],
        registry_digest=data["registry_digest"],
        assertions_ref=assertions_ref,
        diagnostics_ref=diagnostics_ref,
        partition_refs=partition_refs,
        assertion_partition_refs=assertion_partition_refs,
    )


def normalize_code(
    parsed: CodeExtractionArtifact, registry: Registry
) -> ExtractionBundle:
    parsed = CodeExtractionArtifact.model_validate(parsed.model_dump())
    parser, normalizer = _fingerprints(parsed.registration, parsed.files, registry)
    if (
        parsed.registry_digest != registry.registry_digest
        or parsed.parser_fingerprint != parser
        or parsed.normalizer_fingerprint != normalizer
        or parsed.resolver_fingerprint != CODE_RESOLVER_FINGERPRINT
        or parsed.diagnostics_ref is None
    ):
        raise TychoraError("INTEGRITY_ERROR", "Code extraction binding differs")
    registry.validate_metadata(
        parsed.registration.revision.kind_id,
        parsed.registration.revision.metadata_json,
    )
    bundle, _, _ = _build(parsed, registry)
    return bundle
