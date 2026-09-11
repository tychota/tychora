"""Cross-record validation before a SQLite write transaction is acquired."""

from collections.abc import Mapping
from datetime import date
from hashlib import sha256
from typing import NoReturn, cast

from tychora.configuration.registry import Registry, compile_filter
from tychora.contracts.corpus import ExtractionBundle
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import CodeLocator, TextLocator
from tychora.contracts.identity import digest, normalize_source_key
from tychora.contracts.requests import EqFilter
from tychora.ports.artifacts import ArtifactReader

from .code_provenance import validate_code_record


def fail(message: str) -> NoReturn:
    raise TychoraError("INTEGRITY_ERROR", message)


def verify_artifacts(
    bundle: ExtractionBundle, reader: ArtifactReader
) -> dict[str, bytes]:
    needed = {
        loc.locator.artifact_ref
        for loc in bundle.locators
        if loc.locator.type in ("text", "code")
    }
    needed.update(
        asset.locator.artifact_ref
        for asset in bundle.assets
        if asset.locator.type in ("text", "code")
    )
    refs = {ref.sha256: ref for ref in bundle.artifacts}
    if len(refs) != len(bundle.artifacts):
        fail("Duplicate artifact identity")
    payloads = {}
    for ref in refs.values():
        hasher = sha256()
        size = 0
        parts = []
        try:
            with reader.open_verified(ref) as stream:
                while data := stream.read(1024 * 1024):
                    size += len(data)
                    hasher.update(data)
                    if ref.sha256 in needed:
                        parts.append(data)
        except OSError:
            fail("Artifact payload is unavailable")
        if size != ref.size_bytes or hasher.hexdigest() != ref.sha256:
            fail("Artifact bytes differ from their immutable reference")
        if parts:
            payloads[ref.sha256] = b"".join(parts)
        elif ref.sha256 in needed:
            payloads[ref.sha256] = b""
    required = {bundle.revision.original_sha, bundle.extraction.document_artifact_sha}
    required.update(row.artifact_sha for row in bundle.revision_artifacts)
    required.update(row.artifact_sha for row in bundle.assets)
    required.update(row.derivation_sha for row in bundle.proxies)
    required.update(loc.locator.artifact_ref for loc in bundle.locators)
    required.update(row.locator.artifact_ref for row in bundle.assets)
    required.update(
        value
        for value in (
            bundle.extraction.assertions_artifact_sha,
            bundle.extraction.diagnostics_artifact_sha,
        )
        if value
    )
    if required - refs.keys():
        fail("Bundle omits required artifact references")
    return payloads


def validate_source_locator(
    loc: TextLocator | CodeLocator, payloads: dict[str, bytes]
) -> None:
    """Validate shared unit/asset source coordinates against verified original bytes."""
    try:
        text = payloads[loc.artifact_ref].decode("utf-8")
    except UnicodeDecodeError:
        fail("Text locator requires valid UTF-8 bytes")
    if loc.char_end > len(text):
        fail("Locator character range exceeds original text")
    if loc.type == "code" and (
        loc.relative_path != loc.native_ref
        or normalize_source_key(loc.relative_path) != loc.relative_path
        or loc.byte_start != len(text[: loc.char_start].encode("utf-8"))
        or loc.byte_end != len(text[: loc.char_end].encode("utf-8"))
        or loc.line_start != text[: loc.char_start].count("\n") + 1
        or loc.line_end != text[: max(loc.char_start, loc.char_end - 1)].count("\n") + 1
    ):
        fail("Code byte, character, line or path coordinates differ")


def validate_bundle(
    bundle: ExtractionBundle, registry: Registry, payloads: dict[str, bytes]
) -> None:
    revision, extraction = bundle.revision, bundle.extraction
    if (
        bundle.collection.workspace_id != bundle.workspace.workspace_id
        or bundle.source.collection_id != bundle.collection.collection_id
        or revision.source_id != bundle.source.source_id
        or extraction.revision_id != revision.revision_id
    ):
        fail("Bundle ownership differs")
    if normalize_source_key(bundle.source.source_key) != bundle.source.source_key:
        fail("Source key is not canonical")
    registry.validate_metadata(revision.kind_id, revision.metadata_json)
    if revision.metadata_sha != digest("metadata-v1", revision.metadata_json):
        fail("Metadata digest differs")
    for value in (
        revision.published_on,
        revision.effective_from,
        revision.effective_until,
    ):
        if value is not None:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    fail("Date is not strict ISO format")
            except ValueError:
                fail("Invalid ISO date")
    if (
        revision.effective_from
        and revision.effective_until
        and revision.effective_from >= revision.effective_until
    ):
        fail("Effective interval is empty or reversed")
    units = {unit.unit_id: unit for unit in bundle.units}
    proxy_ids = {proxy.proxy_unit_id for proxy in bundle.proxies}
    original_units = {
        uid: unit
        for uid, unit in units.items()
        if unit.text_origin == "source" and uid not in proxy_ids
    }
    if len(units) != len(bundle.units):
        fail("Duplicate evidence unit")
    for unit in units.values():
        if unit.extraction_id != extraction.extraction_id:
            fail("Unit belongs to a different extraction")
        if sha256(unit.text.encode("utf-8")).hexdigest() != unit.text_sha:
            fail("Evidence text hash differs")
        visited = {unit.unit_id}
        parent = unit.parent_id
        while parent is not None:
            if parent not in units or parent in visited:
                fail("Parent is missing, cross-extraction, or cyclic")
            visited.add(parent)
            parent = units[parent].parent_id
    source_bindings = {
        (row.source_ref, row.artifact_sha) for row in bundle.revision_artifacts
    }
    if any(
        row.revision_id != revision.revision_id for row in bundle.revision_artifacts
    ):
        fail("Revision artifact belongs elsewhere")
    for unit in units.values():
        locators = sorted(
            (row for row in bundle.locators if row.unit_id == unit.unit_id),
            key=lambda row: row.fragment_ordinal,
        )
        if (
            tuple(row.fragment_ordinal for row in locators)
            != tuple(range(len(locators)))
            or tuple(row.locator for row in locators) != unit.locators
        ):
            fail("Unit locator rows are incomplete or inconsistent")
        for row in locators:
            loc = row.locator
            if (loc.native_ref, loc.artifact_ref) not in source_bindings:
                fail("Locator has no original revision artifact binding")
            if loc.type in ("text", "code"):
                validate_source_locator(cast(TextLocator | CodeLocator, loc), payloads)
            elif loc.type == "document" and loc.char_end > len(unit.text):
                fail("Document character span exceeds unit text")
        if (
            unit.unit_id in original_units
            and unit.locators
            and all(loc.type in ("text", "code") for loc in unit.locators)
            and not (
                unit.unit_type == "code_file" and not unit.citable and not unit.text
            )
        ):
            original = "".join(
                payloads[loc.artifact_ref].decode("utf-8")[
                    loc.char_start : loc.char_end
                ]
                for loc in cast(tuple[TextLocator | CodeLocator, ...], unit.locators)
            )
            if original != unit.text:
                fail("Original unit text differs from its verified locator spans")
    if any(row.unit_id not in units for row in bundle.locators):
        fail("Locator belongs to an unknown unit")
    for chunk in bundle.chunks:
        unit = units.get(chunk.unit_id)
        if (
            unit is None
            or chunk.extraction_id != extraction.extraction_id
            or not 0 <= chunk.char_start < chunk.char_end <= len(unit.text)
            or chunk.text != unit.text[chunk.char_start : chunk.char_end]
        ):
            fail("Chunk must be an exact nonempty unit substring")
    for facet in bundle.facets:
        if (
            facet.unit_id is not None
            and facet.unit_id not in units
            or facet.revision_id is not None
            and facet.revision_id != revision.revision_id
        ):
            fail("Facet belongs outside this extraction")
        definition = registry.facet(facet.facet_key)
        if definition["key"] != facet.facet_key:
            fail("Stored facets require canonical keys")
        compile_filter(EqFilter(field=facet.facet_key, value=facet.value), registry)
    for record in bundle.records:
        if record.unit_id not in units:
            fail("Structured record belongs outside extraction")
        registry.validate_record(revision.kind_id, record.data_json)
        # The exact bound record schema has validated this optional string array.
        for uid in cast(tuple[str, ...], record.data_json.get("foundation_ids", ())):
            if uid not in original_units:
                fail(
                    "Record foundation is not an original source unit in this extraction"
                )
        bindings = cast(
            Mapping[str, Mapping[str, Mapping[str, str]]],
            registry.artifact["definition_schema_bindings"],
        )
        binding = bindings["kinds"][revision.kind_id]["record_schema"]
        if record.schema_hash != registry.schema_digests[binding]:
            fail("Structured record schema hash differs from original kind binding")
        if revision.kind_id.split("@")[0] == "code_file":
            validate_code_record(
                record,
                units[record.unit_id],
                original_units,
                revision.metadata_json,
                payloads,
            )
    if any(row.unit_id not in units for row in bundle.identifiers):
        fail("Identifier belongs outside extraction")
    assets = {asset.asset_id: asset for asset in bundle.assets}
    if len(assets) != len(bundle.assets) or len(proxy_ids) != len(bundle.proxies):
        fail("Duplicate asset or proxy")
    for asset in assets.values():
        if asset.extraction_id != extraction.extraction_id:
            fail("Asset belongs to a different extraction")
        loc = asset.locator
        if loc.type == "document":
            fail("Asset document locator has no owning unit for its character domain")
        own_image = (
            loc.type == "image"
            and loc.artifact_ref == asset.artifact_sha
            and loc.native_ref == asset.native_ref
        )
        if not own_image and (loc.native_ref, loc.artifact_ref) not in source_bindings:
            fail("Asset locator has no original revision or own-image binding")
        if loc.type in ("text", "code"):
            validate_source_locator(cast(TextLocator | CodeLocator, loc), payloads)
    for link in bundle.unit_assets:
        if link.unit_id not in units or link.asset_id not in assets:
            fail("Asset link crosses extraction boundary")
    for proxy in bundle.proxies:
        unit = units.get(proxy.proxy_unit_id)
        asset = assets.get(proxy.asset_id)
        if unit is None or asset is None or unit.citable or not proxy.required_unit_ids:
            fail("Proxy requires a noncitable unit, asset and original dependencies")
        if proxy.derivation_sha in {
            revision.original_sha,
            asset.artifact_sha,
            extraction.document_artifact_sha,
        }:
            fail("Proxy requires a separate derivation artifact")
        for uid in proxy.required_unit_ids:
            if uid not in original_units:
                fail(
                    "Proxy dependencies must be original source units from its extraction"
                )
