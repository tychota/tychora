from hashlib import sha256
from itertools import pairwise

import pytest
from pydantic import ValidationError

from tests.helpers.corpus import artifact, seed_bundle
from tychora.adapters.code.syntax import source_maps
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.code_storage import CodeFileDescriptor
from tychora.contracts.corpus import (
    AssetRow,
    ExtractionBundle,
    LocatorRow,
    UnitAssetRow,
)
from tychora.contracts.evidence import (
    CodeLocator,
    DocumentLocator,
    EvidenceUnit,
    ImageLocator,
    TextLocator,
)
from tychora.contracts.identity import digest, unit_id
from tychora.indexing.representations import prepare_extraction
from tychora.indexing.windows import window_unit
from tychora.ingestion.assets import ProxyFactory


def _replace_units(
    bundle: ExtractionBundle, units: tuple[EvidenceUnit, ...]
) -> ExtractionBundle:
    locators = tuple(
        LocatorRow(
            locator_id=digest("locator-v1", unit.unit_id, index),
            unit_id=unit.unit_id,
            fragment_ordinal=index,
            locator=locator,
        )
        for unit in units
        for index, locator in enumerate(unit.locators)
    )
    return bundle.model_copy(
        update={"units": units, "locators": locators, "chunks": ()}
    )


def test_windows_cover_end_match_and_preserve_exact_ranges(long_unit, voyage_counter):
    from tychora.contracts.indexing import OwnedInterval, RepresentationUnit

    prepared = RepresentationUnit(
        unit=long_unit,
        revision_id="revision-long",
        title="Long source",
        kind_id="manual@1",
        symbol_path=(),
        ownership=(
            OwnedInterval(
                char_start=0,
                char_end=len(long_unit.text),
                dependency_ids=frozenset({"revision-long", long_unit.unit_id}),
            ),
        ),
    )
    windows = window_unit(prepared, voyage_counter)
    assert any(
        "END_MARKER" in long_unit.text[w.char_start : w.char_end] for w in windows
    )
    assert windows[0].char_start == 0
    assert windows[-1].char_end == len(long_unit.text)
    assert all(a.char_end == b.char_start for a, b in pairwise(windows))
    assert all(w.token_count <= 800 for w in windows)


def test_windows_preserve_unicode_special_text_and_crlf(voyage_counter) -> None:
    text = "Cafe\u0301 🧬 <|endoftext|>\r\nNext sentence.\n\nFinal."
    bundle = seed_bundle(text)
    prepared = prepare_extraction(bundle).representation(bundle.units[0].unit_id)

    windows = window_unit(prepared, voyage_counter)

    assert "".join(window.text for window in windows) == text
    assert all(
        window.text == text[window.char_start : window.char_end] for window in windows
    )
    assert all(
        window.token_count == voyage_counter.count(window.text) for window in windows
    )


def test_long_atomic_source_splits_without_overlap(counter) -> None:
    text = "x" * 1_801
    bundle = seed_bundle(text)
    prepared = prepare_extraction(bundle).representation(bundle.units[0].unit_id)

    windows = window_unit(prepared, counter)

    assert len(windows) == 3
    assert [window.token_count for window in windows] == [512, 512, 777]
    assert "".join(window.text for window in windows) == text


def test_windows_prefer_exact_paragraph_boundaries(counter) -> None:
    text = "a" * 400 + "\n\n" + "b" * 400 + "\n\n" + "c" * 400
    bundle = seed_bundle(text)

    windows = window_unit(
        prepare_extraction(bundle).representation(bundle.units[0].unit_id), counter
    )

    assert [window.char_end for window in windows] == [402, 804, 1204]
    assert "".join(window.text for window in windows) == text


def test_native_overlap_splits_parent_at_exact_child_range(counter) -> None:
    bundle = seed_bundle("prefix CHILD suffix")
    parent = bundle.units[0]
    parent_locator = parent.locators[0]
    child_text = "CHILD"
    child_sha = sha256(child_text.encode()).hexdigest()
    child_id = unit_id(bundle.extraction.extraction_id, "child", 1, child_sha)
    child = EvidenceUnit(
        unit_id=child_id,
        extraction_id=parent.extraction_id,
        parent_id=parent.unit_id,
        ordinal=1,
        unit_type="paragraph",
        native_ref="child",
        boundary_key=parent.boundary_key,
        text=child_text,
        text_sha=child_sha,
        locators=(
            TextLocator(
                artifact_ref=parent_locator.artifact_ref,
                native_ref=parent_locator.native_ref,
                char_start=7,
                char_end=12,
            ),
        ),
        attributes=FrozenJsonMap(),
    )
    bundle = _replace_units(bundle, (parent, child))

    windows = window_unit(
        prepare_extraction(bundle).representation(parent.unit_id), counter
    )

    assert [window.text for window in windows] == ["prefix ", "CHILD", " suffix"]
    assert child_id not in windows[0].dependency_ids
    assert child_id in windows[1].dependency_ids
    assert child_id not in windows[2].dependency_ids
    assert all(
        bundle.revision.revision_id in window.dependency_ids for window in windows
    )

    wrong_text = "WRONG"
    wrong_sha = sha256(wrong_text.encode()).hexdigest()
    wrong_child = child.model_copy(
        update={
            "unit_id": unit_id(
                bundle.extraction.extraction_id,
                child.native_ref,
                child.ordinal,
                wrong_sha,
            ),
            "text": wrong_text,
            "text_sha": wrong_sha,
        }
    )
    with pytest.raises(ValueError, match="source slices differ"):
        prepare_extraction(_replace_units(bundle, (parent, wrong_child)))

    wrong_source = child.model_copy(
        update={
            "locators": (
                child.locators[0].model_copy(update={"native_ref": "other.md"}),
            )
        }
    )
    with pytest.raises(ValueError, match="source locator binding"):
        prepare_extraction(_replace_units(bundle, (parent, wrong_source)))


def test_empty_source_container_has_no_text_window(counter) -> None:
    bundle = seed_bundle("")

    assert (
        window_unit(
            prepare_extraction(bundle).representation(bundle.units[0].unit_id), counter
        )
        == ()
    )


def test_representation_contract_rejects_inexact_text_range(counter) -> None:
    bundle = seed_bundle("exact")
    (window,) = window_unit(
        prepare_extraction(bundle).representation(bundle.units[0].unit_id), counter
    )

    with pytest.raises(ValidationError, match="range length"):
        type(window).model_validate({**window.model_dump(), "char_end": 4})


def test_prepared_extraction_reuses_complete_code_ownership_view(counter) -> None:
    bundle = seed_bundle("header\nchild body\nfooter", kind="code_file@1")
    parent = bundle.units[0]
    artifact = parent.locators[0].artifact_ref
    path = "src/example.py"

    def code_unit(
        uid: str, text: str, start: int, end: int, ordinal: int, parent_id: str | None
    ) -> EvidenceUnit:
        return EvidenceUnit(
            unit_id=uid,
            extraction_id=bundle.extraction.extraction_id,
            parent_id=parent_id,
            ordinal=ordinal,
            unit_type="symbol",
            native_ref=f"symbol:{ordinal}",
            boundary_key=path,
            text=text,
            text_sha=sha256(text.encode()).hexdigest(),
            locators=(
                CodeLocator(
                    artifact_ref=artifact,
                    native_ref=path,
                    relative_path=path,
                    char_start=start,
                    char_end=end,
                    byte_start=start,
                    byte_end=end,
                    line_start=1 if ordinal == 0 else 2,
                    line_end=3 if ordinal == 0 else 2,
                ),
            ),
            attributes=FrozenJsonMap(
                {
                    "relative_path": path,
                    "language": "python",
                    "range_dependency_ids": [uid, child_id]
                    if ordinal == 0
                    else [uid, parent_id],
                }
            ),
        )

    parent_id = unit_id(bundle.extraction.extraction_id, "parent", 0, parent.text_sha)
    child_text = "child body"
    child_id = unit_id(
        bundle.extraction.extraction_id,
        "child",
        1,
        sha256(child_text.encode()).hexdigest(),
    )
    code_parent = code_unit(parent_id, parent.text, 0, len(parent.text), 0, None)
    code_child = code_unit(child_id, child_text, 7, 17, 1, parent_id)
    encoded = parent.text.encode()
    maps = source_maps(encoded, parent.text, "utf-8")
    descriptor = CodeFileDescriptor(
        relative_path=path,
        artifact_ref=artifact,
        capture_entry_digest="1" * 64,
        language="python",
        encoding="utf-8",
        coverage="parsed",
        adapter_fingerprint="2" * 64,
        byte_length=len(encoded),
        char_length=len(parent.text),
        codepoint_to_byte=maps[0],
        byte_to_codepoint=maps[1],
        line_starts=maps[2],
        byte_to_line=maps[3],
    )
    bundle = _replace_units(bundle, (code_parent, code_child)).model_copy(
        update={"code_files": (descriptor,)}
    )

    prepared = prepare_extraction(bundle)
    parent_windows = window_unit(prepared.representation(parent_id), counter)
    child_windows = window_unit(prepared.representation(child_id), counter)

    assert [window.text for window in parent_windows] == [
        "header\n",
        "child body",
        "\nfooter",
    ]
    assert child_id in parent_windows[1].dependency_ids
    assert parent_id in child_windows[0].dependency_ids

    bad_locator = code_child.locators[0].model_copy(update={"byte_start": 8})
    bad_child = code_child.model_copy(update={"locators": (bad_locator,)})
    with pytest.raises(ValueError, match="codepoint/byte/line"):
        prepare_extraction(_replace_units(bundle, (code_parent, bad_child)))


def test_docling_source_parts_bind_narrow_overlapping_owner(counter) -> None:
    bundle = seed_bundle("AAchildZZ")
    parent = bundle.units[0]
    child_text = "child"
    child_id = unit_id(
        bundle.extraction.extraction_id,
        "doc-child",
        1,
        sha256(child_text.encode()).hexdigest(),
    )
    common = {
        "document_representation": "docling-source-parts@1",
        "source_refs": ["#/texts/0"],
        "structural_separators": [],
    }
    source_domain = digest(
        "document-source-domain-v1",
        bundle.revision.revision_id,
        bundle.extraction.extraction_id,
        "#/texts/0",
        "text",
        None,
    )
    parent = parent.model_copy(
        update={
            "locators": (
                DocumentLocator(
                    artifact_ref=parent.locators[0].artifact_ref,
                    native_ref=bundle.source.source_key,
                    char_start=0,
                    char_end=len(parent.text),
                ),
            ),
            "attributes": FrozenJsonMap(
                {
                    **common,
                    "source_parts": [
                        {
                            "native_ref": "#/texts/0",
                            "field": "text",
                            "cell_index": None,
                            "unit_start": 0,
                            "unit_end": 9,
                            "source_start": 0,
                            "source_end": 9,
                            "source_domain": source_domain,
                            "provenance": [],
                        }
                    ],
                    "overlapping_owner_ids": [parent.unit_id, child_id],
                }
            ),
        }
    )
    child = EvidenceUnit(
        unit_id=child_id,
        extraction_id=parent.extraction_id,
        parent_id=parent.unit_id,
        ordinal=1,
        unit_type="paragraph",
        native_ref="doc-child",
        boundary_key=parent.boundary_key,
        text=child_text,
        text_sha=sha256(child_text.encode()).hexdigest(),
        locators=(
            DocumentLocator(
                artifact_ref=parent.locators[0].artifact_ref,
                native_ref=bundle.source.source_key,
                char_start=0,
                char_end=5,
            ),
        ),
        attributes=FrozenJsonMap(
            {
                **common,
                "source_parts": [
                    {
                        "native_ref": "#/texts/0",
                        "field": "text",
                        "cell_index": None,
                        "unit_start": 0,
                        "unit_end": 5,
                        "source_start": 2,
                        "source_end": 7,
                        "source_domain": source_domain,
                        "provenance": [],
                    }
                ],
                "overlapping_owner_ids": [parent.unit_id, child_id],
            }
        ),
    )
    bundle = _replace_units(bundle, (parent, child))

    windows = window_unit(
        prepare_extraction(bundle).representation(parent.unit_id), counter
    )

    assert [window.text for window in windows] == ["AA", "child", "ZZ"]
    assert child_id in windows[1].dependency_ids
    assert child_id not in windows[0].dependency_ids | windows[2].dependency_ids

    broken_parts = list(parent.attributes["source_parts"])
    broken_parts[0] = {**dict(broken_parts[0]), "unit_end": 8, "source_end": 8}
    broken_parent = parent.model_copy(
        update={
            "attributes": FrozenJsonMap(
                {**dict(parent.attributes), "source_parts": broken_parts}
            )
        }
    )
    with pytest.raises(ValueError, match="source parts.*cover"):
        prepare_extraction(_replace_units(bundle, (broken_parent, child)))

    fabricated_parent = parent.model_copy(
        update={
            "attributes": FrozenJsonMap(
                {
                    **dict(parent.attributes),
                    "source_parts": [
                        {
                            **dict(parent.attributes["source_parts"][0]),
                            "source_domain": "fabricated-domain",
                        }
                    ],
                }
            )
        }
    )
    fabricated_child = child.model_copy(
        update={
            "attributes": FrozenJsonMap(
                {
                    **dict(child.attributes),
                    "source_parts": [
                        {
                            **dict(child.attributes["source_parts"][0]),
                            "source_domain": "fabricated-domain",
                        }
                    ],
                }
            )
        }
    )
    with pytest.raises(ValueError, match="source domain identity"):
        prepare_extraction(
            _replace_units(bundle, (fabricated_parent, fabricated_child))
        )

    wrong_text = "WRONG"
    wrong_sha = sha256(wrong_text.encode()).hexdigest()
    wrong_id = unit_id(
        bundle.extraction.extraction_id,
        child.native_ref,
        child.ordinal,
        wrong_sha,
    )
    wrong_parent = parent.model_copy(
        update={
            "attributes": FrozenJsonMap(
                {
                    **dict(parent.attributes),
                    "overlapping_owner_ids": [parent.unit_id, wrong_id],
                }
            )
        }
    )
    wrong_child = child.model_copy(
        update={
            "unit_id": wrong_id,
            "text": wrong_text,
            "text_sha": wrong_sha,
            "attributes": FrozenJsonMap(
                {
                    **dict(child.attributes),
                    "overlapping_owner_ids": [parent.unit_id, wrong_id],
                }
            ),
        }
    )
    with pytest.raises(ValueError, match="document source slices differ"):
        prepare_extraction(_replace_units(bundle, (wrong_parent, wrong_child)))

    undeclared = parent.model_copy(
        update={
            "attributes": FrozenJsonMap(
                {
                    key: value
                    for key, value in parent.attributes.items()
                    if key != "overlapping_owner_ids"
                }
            )
        }
    )
    with pytest.raises(ValueError, match="declared ownership"):
        prepare_extraction(_replace_units(bundle, (undeclared, child)))


def test_proxy_windows_bind_asset_revision_and_every_original_owner(counter) -> None:
    bundle = seed_bundle("original context")
    original = bundle.units[0]
    context_text = "context"
    context_sha = sha256(context_text.encode()).hexdigest()
    context = EvidenceUnit(
        unit_id=unit_id(bundle.extraction.extraction_id, "context", 1, context_sha),
        extraction_id=bundle.extraction.extraction_id,
        parent_id=original.unit_id,
        ordinal=1,
        unit_type="paragraph",
        native_ref="context",
        boundary_key=original.boundary_key,
        text=context_text,
        text_sha=context_sha,
        locators=(
            TextLocator(
                artifact_ref=original.locators[0].artifact_ref,
                native_ref=original.locators[0].native_ref,
                char_start=9,
                char_end=16,
            ),
        ),
    )
    asset_id = "asset-a"
    image_locator = ImageLocator(artifact_ref="image-ref", native_ref="image.png")
    bundle = _replace_units(bundle, (original, context)).model_copy(
        update={
            "assets": (
                AssetRow(
                    asset_id=asset_id,
                    extraction_id=bundle.extraction.extraction_id,
                    artifact_sha="1" * 64,
                    asset_type="image",
                    native_ref="image.png",
                    locator=image_locator,
                ),
            ),
            "unit_assets": (
                UnitAssetRow(
                    unit_id=original.unit_id,
                    asset_id=asset_id,
                    role="source",
                ),
            ),
        }
    )
    derivation = artifact(b"synthetic proxy derivation", "application/json")
    created = ProxyFactory(bundle).make_proxy(
        asset_id,
        "description",
        "derived description",
        (original.unit_id, context.unit_id),
        derivation,
    )
    bundle = _replace_units(bundle, (*bundle.units, created.unit)).model_copy(
        update={
            "artifacts": (*bundle.artifacts, derivation),
            "proxies": (created.binding,),
        }
    )
    proxy_id = created.unit.unit_id

    (window,) = window_unit(
        prepare_extraction(bundle).representation(proxy_id), counter
    )

    assert window.dependency_ids == frozenset(
        {
            bundle.revision.revision_id,
            proxy_id,
            asset_id,
            original.unit_id,
            context.unit_id,
        }
    )

    mismatched_unit = created.unit.model_copy(
        update={
            "attributes": FrozenJsonMap({"asset_id": asset_id, "proxy_kind": "ocr"})
        }
    )
    with pytest.raises(ValueError, match="proxy unit.*binding"):
        prepare_extraction(_replace_units(bundle, (original, context, mismatched_unit)))

    omitted_owner = bundle.model_copy(
        update={
            "proxies": (
                bundle.proxies[0].model_copy(
                    update={"required_unit_ids": frozenset({context.unit_id})}
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="complete original ownership"):
        prepare_extraction(omitted_owner)

    proxy_owner = bundle.model_copy(
        update={
            "proxies": (
                bundle.proxies[0].model_copy(
                    update={"required_unit_ids": frozenset({proxy_id})}
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="original source"):
        prepare_extraction(proxy_owner)


def test_preparation_fails_when_retained_owner_is_missing() -> None:
    bundle = seed_bundle("source")
    unit = bundle.units[0].model_copy(
        update={"attributes": FrozenJsonMap({"overlapping_owner_ids": ["missing"]})}
    )

    with pytest.raises(ValueError, match="owner set differs"):
        prepare_extraction(_replace_units(bundle, (unit,)))
