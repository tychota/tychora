"""Cross-record validation before a SQLite write transaction is acquired."""

import json
from collections.abc import Mapping
from datetime import date
from hashlib import sha256
from types import SimpleNamespace
from typing import Any, NoReturn, cast

from tychora.adapters.files.assertions import iter_verified_assertions
from tychora.adapters.files.references import MAX_MANIFEST_BYTES
from tychora.configuration.registry import Registry, compile_filter
from tychora.contracts.code import CODE_RESOLVER_FINGERPRINT
from tychora.contracts.corpus import ExtractionBundle
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import CodeLocator, TextLocator
from tychora.contracts.identity import digest, normalize_source_key
from tychora.contracts.requests import EqFilter
from tychora.ports.artifacts import ArtifactReader

from .code_provenance import validate_code_record


def fail(message: str) -> NoReturn:
    raise TychoraError("INTEGRITY_ERROR", message)


def _code_text(descriptor, payload: bytes) -> str:
    from tychora.adapters.code.syntax import source_maps

    try:
        text = payload.decode(descriptor.encoding)
        maps = source_maps(payload, text, descriptor.encoding)
    except (LookupError, UnicodeError, ValueError):
        fail("Code descriptor encoding cannot reproduce original coordinates")
    if (
        descriptor.byte_length != len(payload)
        or descriptor.char_length != len(text)
        or descriptor.codepoint_to_byte != maps[0]
        or descriptor.byte_to_codepoint != maps[1]
        or descriptor.line_starts != maps[2]
        or descriptor.byte_to_line != maps[3]
    ):
        fail("Code descriptor differs from recomputed original coordinates")
    return text


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
    if bundle.code_files:
        needed.add(bundle.extraction.document_artifact_sha)
        needed.update(item.artifact_ref for item in bundle.code_files)
        needed.update(
            ref.sha256
            for ref in bundle.artifacts
            if ref.media_type == "application/json"
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
    if (
        bundle.extraction.assertions_artifact_sha is not None
        and refs[bundle.extraction.assertions_artifact_sha].media_type
        == "application/json"
    ):
        root = refs[bundle.extraction.assertions_artifact_sha]
        locations = tuple(
            iter_verified_assertions(
                root,
                reader,
                expected_extraction_id=(
                    bundle.extraction.extraction_id if bundle.code_files else None
                ),
            )
        )
        if any(item.leaf_ref.sha256 not in refs for item in locations):
            fail("Bundle omits an assertion partition artifact")
    return payloads


def validate_source_locator(
    loc: TextLocator | CodeLocator, payloads: dict[str, bytes], descriptor=None
) -> None:
    """Validate shared unit/asset source coordinates against verified original bytes."""
    if loc.type == "code" and descriptor is not None:
        text = _code_text(descriptor, payloads[loc.artifact_ref])
    else:
        try:
            text = payloads[loc.artifact_ref].decode("utf-8")
        except UnicodeDecodeError:
            fail("Text locator requires valid UTF-8 bytes")
    if loc.char_end > len(text):
        fail("Locator character range exceeds original text")
    if loc.type == "code" and (
        loc.relative_path != loc.native_ref
        or normalize_source_key(loc.relative_path) != loc.relative_path
        or loc.byte_start
        != (
            descriptor.codepoint_to_byte[loc.char_start]
            if descriptor is not None
            else len(text[: loc.char_start].encode("utf-8"))
        )
        or loc.byte_end
        != (
            descriptor.codepoint_to_byte[loc.char_end]
            if descriptor is not None
            else len(text[: loc.char_end].encode("utf-8"))
        )
        or loc.line_start
        != (
            descriptor.byte_to_line[loc.byte_start]
            if descriptor is not None
            else text[: loc.char_start].count("\n") + 1
        )
        or loc.line_end
        != (
            descriptor.byte_to_line[max(loc.byte_start, loc.byte_end - 1)]
            if descriptor is not None
            else text[: max(loc.char_start, loc.char_end - 1)].count("\n") + 1
        )
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
    code_kind = revision.kind_id.split("@")[0] == "code_file"
    metadata_has_path = "relative_path" in revision.metadata_json
    metadata_has_language = "language" in revision.metadata_json
    if (
        code_kind
        and bundle.source.source_type != "repository"
        and (not metadata_has_path or not metadata_has_language)
    ):
        fail("Standalone code metadata requires relative_path and language")
    if (
        code_kind
        and bundle.source.source_type == "repository"
        and not bundle.code_files
    ):
        fail("Repository code requires verified per-file descriptors")
    code_files = {item.relative_path: item for item in bundle.code_files}
    if len(code_files) != len(bundle.code_files):
        fail("Duplicate code file descriptor path")
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
    if bundle.code_files:
        try:
            retained = json.loads(payloads[extraction.document_artifact_sha])
            retained_data = retained["data"]
            retained_values = retained_data["files"]
            links = retained.get("required_manifests", [])
            partition_links = [
                link for link in links if link.get("type") == "extraction"
            ]
            assertion_links = [
                link
                for link in links
                if link.get("type") in ("assertion", "assertion_index")
            ]
            if len(partition_links) + len(assertion_links) != len(links):
                fail("Retained code extraction has an unknown manifest link")
            assertion_ref = retained_data.get("assertions_ref")
            if (
                bool(assertion_ref) != bool(assertion_links)
                or len(assertion_links) > 1
                or assertion_ref is not None
                and assertion_links[0]["artifact"] != assertion_ref
            ):
                fail("Retained code assertion closure differs")
            if retained_data.get("files_partitioned"):
                if (
                    retained_values
                    or not partition_links
                    or len(partition_links) > 10_000
                ):
                    fail("Retained code partition root is malformed")
                retained_values = []
                for link in partition_links:
                    if (
                        link.get("type") != "extraction"
                        or link.get("schema_version") != "1"
                    ):
                        fail("Retained code partition link is unregistered")
                    child_payload = payloads[link["artifact"]["sha256"]]
                    if len(child_payload) > MAX_MANIFEST_BYTES:
                        fail("Retained code extraction partition exceeds byte capacity")
                    child = json.loads(child_payload)
                    values = child.get("data", {}).get("files")
                    if (
                        child.get("type") != "extraction"
                        or child.get("schema_version") != "1"
                        or child.get("required_artifacts") != []
                        or child.get("required_manifests") != []
                        or not isinstance(values, list)
                        or not 0 < len(values) <= 4000
                    ):
                        fail("Retained code extraction partition is malformed")
                    retained_values.extend(values)
            elif partition_links:
                fail("Inline retained code extraction has partition links")
            retained_files = {
                item["captured"]["relative_path"]: item for item in retained_values
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            fail("Retained code extraction manifest is malformed")
        if (
            retained.get("type") != "extraction"
            or retained.get("schema_version") != "1"
            or retained_data.get("registry_digest") != registry.registry_digest
            or retained_data.get("resolver_fingerprint")
            != CODE_RESOLVER_FINGERPRINT
            or retained_data.get("parser_fingerprint") != extraction.parser_fingerprint
            or retained_data.get("normalizer_fingerprint")
            != extraction.normalizer_fingerprint
            or (
                retained_data["assertions_ref"]["sha256"]
                if retained_data.get("assertions_ref") is not None
                else None
            )
            != extraction.assertions_artifact_sha
            or (
                retained_data["diagnostics_ref"]["sha256"]
                if retained_data.get("diagnostics_ref") is not None
                else None
            )
            != extraction.diagnostics_artifact_sha
            or set(retained_files) != set(code_files)
        ):
            fail("Retained code extraction differs from canonical preparation")
        parser_fingerprint = digest(
            "code-parser-v1",
            [
                [
                    item["captured"]["relative_path"],
                    item["syntax"]["adapter_fingerprint"],
                    item["syntax"]["options"],
                    item["syntax"]["coverage"],
                ]
                for item in retained_values
            ],
        )
        kind = cast(Mapping[str, Any], registry.kinds[revision.kind_id])
        normalizer_fingerprint = digest(
            "code-normalizer-v1",
            kind,
            registry.schema_digests,
            CODE_RESOLVER_FINGERPRINT,
        )
        if (
            extraction.parser_fingerprint != parser_fingerprint
            or extraction.normalizer_fingerprint != normalizer_fingerprint
        ):
            fail("Code extraction fingerprints differ from verified inputs")
        for path, descriptor in code_files.items():
            stored = retained_files[path]
            syntax = stored["syntax"]
            if (
                stored["captured"]["artifact_ref"]["sha256"] != descriptor.artifact_ref
                or digest("captured-file-v1", stored["captured"])
                != descriptor.capture_entry_digest
                or syntax["language"] != descriptor.language
                or syntax["encoding"] != descriptor.encoding
                or syntax["coverage"] != descriptor.coverage
                or syntax["adapter_fingerprint"] != descriptor.adapter_fingerprint
                or (path, descriptor.artifact_ref) not in source_bindings
            ):
                fail("Code descriptor differs from retained file provenance")
            _code_text(descriptor, payloads[descriptor.artifact_ref])

    def descriptor_for(loc: CodeLocator):
        descriptor = code_files.get(loc.relative_path)
        if descriptor is not None:
            if descriptor.artifact_ref != loc.artifact_ref:
                fail("Code locator artifact differs from file descriptor")
            return descriptor
        if bundle.source.source_type == "repository":
            fail("Repository code locator has no verified file descriptor")
        from tychora.adapters.code.syntax import source_maps

        payload = payloads[loc.artifact_ref]
        text = payload.decode("utf-8")
        maps = source_maps(payload, text, "utf-8")
        return SimpleNamespace(
            relative_path=revision.metadata_json["relative_path"],
            language=revision.metadata_json["language"],
            encoding="utf-8",
            byte_length=len(payload),
            char_length=len(text),
            codepoint_to_byte=maps[0],
            byte_to_codepoint=maps[1],
            line_starts=maps[2],
            byte_to_line=maps[3],
        )

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
                validate_source_locator(
                    cast(TextLocator | CodeLocator, loc),
                    payloads,
                    descriptor_for(loc) if loc.type == "code" else None,
                )
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
            original_parts = []
            for loc in cast(tuple[TextLocator | CodeLocator, ...], unit.locators):
                text = (
                    _code_text(descriptor_for(loc), payloads[loc.artifact_ref])
                    if loc.type == "code"
                    else payloads[loc.artifact_ref].decode("utf-8")
                )
                original_parts.append(text[loc.char_start : loc.char_end])
            original = "".join(original_parts)
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
    if bundle.code_files:
        facet_values = {
            (facet.unit_id, facet.facet_key): facet.value
            for facet in bundle.facets
            if facet.unit_id is not None
        }
        containers = {}
        for unit in units.values():
            code_locators = [loc for loc in unit.locators if loc.type == "code"]
            if not code_locators:
                continue
            if len(code_locators) != 1:
                fail("Code unit requires one original-file locator")
            descriptor = descriptor_for(code_locators[0])
            if (
                unit.attributes.get("relative_path") != descriptor.relative_path
                or unit.attributes.get("language") != descriptor.language
                or facet_values.get((unit.unit_id, "code.path"))
                != descriptor.relative_path
                or facet_values.get((unit.unit_id, "code.language"))
                != descriptor.language
            ):
                fail("Code unit attributes or all-unit facets differ from descriptor")
            if unit.unit_type == "code_file":
                if (
                    unit.citable
                    or unit.text
                    or unit.attributes.get("encoding") != descriptor.encoding
                    or unit.attributes.get("coverage") != descriptor.coverage
                    or unit.attributes.get("artifact_ref") != descriptor.artifact_ref
                    or unit.attributes.get("capture_entry_digest")
                    != descriptor.capture_entry_digest
                    or unit.attributes.get("adapter_fingerprint")
                    != descriptor.adapter_fingerprint
                    or descriptor.relative_path in containers
                ):
                    fail("Code file container differs from verified descriptor")
                containers[descriptor.relative_path] = unit.unit_id
        if set(containers) != set(code_files):
            fail("Code file descriptors require exact container coverage")
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
            code_locator = next(
                (loc for loc in units[record.unit_id].locators if loc.type == "code"),
                None,
            )
            if code_locator is None:
                fail("Code symbol record has no code locator")
            validate_code_record(
                record,
                units[record.unit_id],
                original_units,
                descriptor_for(code_locator),
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
