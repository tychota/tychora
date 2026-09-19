import json
import os
import shutil
from contextlib import ExitStack
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from tests.fixtures.documents.create_record_fixtures import (
    create_record_document,
    record_content,
)
from tychora.adapters.docling.parser import DoclingParser
from tychora.application.owner import WorkspaceOwner
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts import SourceInput, identity
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import CollectionRow, RevisionRow, SourceRow, WorkspaceRow
from tychora.contracts.extraction import DocumentRegistration
from tychora.contracts.requests import RequestScope
from tychora.ingestion.acquire import Acquirer
from tychora.ingestion.normalize import normalize
from tychora.ports.execution import ExecutionContext


@pytest.fixture(scope="module")
def document_record_case(tmp_path_factory):
    root = tmp_path_factory.mktemp("document-records")
    cache = {}
    with ExitStack() as stack:

        def make(
            kind,
            format,
            *,
            custom=False,
            page_break_at=None,
            source_builder=None,
            commit=True,
            online=False,
            scope=None,
        ):
            key = (
                kind,
                format,
                custom,
                page_break_at,
                source_builder,
                commit,
                online,
                None if scope is None else scope.model_dump_json(),
            )
            if key in cache:
                return cache[key]
            folder = root / str(len(list(root.iterdir())))
            folder.mkdir()
            pack = folder / "pack"
            shutil.copytree(
                Path(__file__).parents[2] / "src/tychora/profiles/documents", pack
            )
            content = record_content(kind)
            registered_kind = "feedback@1" if kind.startswith("feedback") else kind
            if custom:
                target = pack / "kinds/standard.toml"
                target.write_text(
                    target.read_text()
                    .replace(
                        'identifier_prefixes = ["STD-"]',
                        'identifier_prefixes = ["LOCAL-"]',
                    )
                    .replace(
                        'requirement_labels = ["Requirement"]',
                        'requirement_labels = ["Exigence"]',
                    )
                    .replace(
                        'proof_labels = ["Expected evidence"]',
                        'proof_labels = ["Preuve attendue"]',
                    )
                    + 'foundation_labels = ["Foundation"]\n'
                )
                content = [
                    (
                        level,
                        text.replace("STD-", "LOCAL-")
                        .replace("Requirement:", "Exigence:")
                        .replace("Expected evidence:", "Preuve attendue:"),
                    )
                    for level, text in content
                ]
            if format != "markdown":
                with (pack / "profile.toml").open("a") as out:
                    out.write('\n[extraction]\nparser="docling"\n')
            source = folder / ("source." + format)
            if source_builder is None:
                create_record_document(source, content, page_break_at=page_break_at)
            else:
                source_builder(source, content)
            config_path = folder / "workspace.toml"
            config_path.write_text(
                'schema_version=1\n[profiles.test]\npath="pack"\n[configuration.acquisition]\nroots='
                + json.dumps([str(folder)])
                + "\n"
            )
            config = resolve_config(config_path, "test", {})
            if online:
                config = config.model_copy(
                    update={
                        "values": config.values.model_copy(update={"offline": False})
                    }
                )
            owner = stack.enter_context(
                WorkspaceOwner.open(
                    folder / "data",
                    config,
                    scope or RequestScope(principal_id="fixture"),
                )
            )
            ctx = ExecutionContext()
            metadata = json.loads(
                (
                    Path(__file__).parents[1]
                    / "configuration/fixtures/generic-records.json"
                ).read_text()
            )[registered_kind]["metadata"]
            if registered_kind == "feedback@1":
                metadata.update(author=None, target=None, decision=None)
            collection = CollectionRow(
                collection_id="00000000-0000-4000-8000-000000000010",
                workspace_id=owner.workspace_id,
                slug="synthetic",
                title="Synthetic documents",
            )
            captured = owner.run_blocking_sync(
                lambda: Acquirer(owner.artifacts, config).capture(
                    SourceInput.model_validate(
                        {
                            **(
                                {
                                    "type": "text",
                                    "text": source.read_text(),
                                    "format": "markdown",
                                    "encoding": "utf-8",
                                }
                                if format == "markdown"
                                else {"type": "file", "path": str(source)}
                            ),
                            "kind": registered_kind,
                            "metadata": metadata,
                            "collection_id": UUID(collection.collection_id),
                            "source_key": source.name,
                        }
                    ),
                    ctx,
                ),
                ctx,
            )
            sid = identity.source_id(UUID(collection.collection_id), source.name)
            msha = identity.digest("metadata-v1", metadata)
            revision = RevisionRow(
                revision_id=identity.revision_id(sid, captured.original_sha, "1", msha),
                source_id=sid,
                kind_id=registered_kind,
                original_sha=captured.original_sha,
                edition_key="1",
                metadata_sha=msha,
                title=metadata["title"],
                metadata_json=FrozenJsonMap(metadata),
                captured_at=captured.captured_at.isoformat(),
                allowed_uses_json=("lookup",),
            )
            fields = {
                "capture": captured,
                "workspace": WorkspaceRow(
                    workspace_id=owner.workspace_id,
                    created_at=owner.run_blocking_sync(
                        lambda: owner.db.execute(
                            "SELECT created_at FROM workspaces"
                        ).fetchone()[0],
                        ctx,
                    ),
                ),
                "collection": collection,
                "source": SourceRow(
                    source_id=sid,
                    collection_id=collection.collection_id,
                    source_key=source.name,
                    created_at=captured.captured_at.isoformat(),
                ),
                "revision": revision,
                "format": format,
            }
            if format == "markdown":
                from tychora.contracts.extraction import NativeRegistration
                from tychora.ingestion.native import NativeParser

                native_registration = NativeRegistration.model_validate(fields)
                parsed = owner.run_blocking_sync(
                    lambda: NativeParser(owner.artifacts, native_registration).extract(
                        captured.original_ref, config, ctx
                    ),
                    ctx,
                )
            else:
                registration = DocumentRegistration.model_validate(fields)
                model_path = os.environ.get("TYCHORA_TEST_DOCLING_MODELS")
                if not model_path:
                    pytest.fail(
                        "Set TYCHORA_TEST_DOCLING_MODELS to an explicitly provisioned checksum-verified Docling model bundle; ingestion never downloads models."
                    )
                parsed = DoclingParser(owner, registration, Path(model_path)).extract(
                    captured.original_ref, config, ctx
                )
            registry = Registry.from_config(config)
            bundle = (
                normalize(parsed, revision.revision_id, registry) if commit else None
            )
            if bundle is not None:
                owner.run_blocking_sync(
                    lambda: owner.store.commit_extraction(bundle), ctx
                )
            result = SimpleNamespace(
                parsed=parsed,
                bundle=bundle,
                store=owner.store,
                registry=registry,
                owner=owner,
                ctx=ctx,
                source=source,
                content=content,
            )
            cache[key] = result
            return result

        yield make


@pytest.mark.qualification
@pytest.mark.parametrize("format", ["pdf", "docx"])
def test_standard_document_records_use_source_fields(document_record_case, format):
    result = document_record_case("standard@1", format)
    record = next(row for row in result.bundle.records if row.record_type == "rule@1")
    assert (
        "Record the sample before changing the instrument."
        in record.data_json["requirement"]
    )
    assert "Keep the original label." in record.data_json["requirement"]
    assert record.data_json["proof"] == "The sample identifier is quotable."
    assert record.data_json["control"] == "Inspect the source."
    assert record.data_json["severity"] == "warning"
    assert record.data_json["foundation_ids"] == ()
    assert record.data_json["foundation_refs"] == ("REF-MISSING",)


def assert_source_parts(result):
    from tychora.contracts.documents import DocumentSourcePart

    raw = {u.native_ref: u for u in result.parsed.units}
    for unit in result.bundle.units:
        assert unit.attributes["source_refs"]
        ranges = []
        for entry in unit.attributes["source_parts"]:
            part = DocumentSourcePart.model_validate_json(
                identity.canonical_bytes(entry)
            )
            item = raw[part.native_ref]
            source = (
                item.raw_item[part.field]
                if part.field != "table_cell"
                else item.raw_item["data"]["table_cells"][part.cell_index]["text"]
            )
            assert (
                unit.text[part.unit_start : part.unit_end]
                == source[part.source_start : part.source_end]
            )
            assert part.provenance == item.provenance
            ranges.append((part.unit_start, part.unit_end))
        for sep in unit.attributes["structural_separators"]:
            assert unit.text[sep["unit_start"] : sep["unit_end"]] == sep["text"]
            ranges.append((sep["unit_start"], sep["unit_end"]))
        ranges.sort()
        assert ranges[0][0] == 0 and ranges[-1][1] == len(unit.text)
        assert all(a[1] == b[0] for a, b in pairwise(ranges))
    assert (
        result.owner.run_blocking_sync(
            lambda: result.store.hydrate(tuple(u.unit_id for u in result.bundle.units)),
            result.ctx,
        )
        == result.bundle.units
    )


@pytest.mark.qualification
@pytest.mark.parametrize("format", ["pdf", "docx"])
@pytest.mark.parametrize("kind", ["template@1", "feedback@1", "feedback-explicit@1"])
def test_literal_template_and_feedback(document_record_case, format, kind):
    result = document_record_case(kind, format)
    values = [row.data_json for row in result.bundle.records]
    if kind == "template@1":
        assert [(v["slot"], v["instruction"]) for v in values] == [
            ("Summary", "State the observed result.\n\nKeep the uncertainty explicit."),
            ("Limits", "State the supported range."),
        ]
        assert {r.record_type for r in result.bundle.records} == {"template-slot@1"}
    else:
        assert len(values) == 1
        assert result.bundle.records[0].record_type == "feedback@1"
        value = values[0]
        assert value["comment"] == "The baseline is missing."
        assert value["target"] == "paragraph-3"
        assert [value[k] for k in ("author", "decision", "observation", "status")] == (
            [
                "Source reviewer",
                "Request source comparison",
                "Source supplied observation",
                "proposed",
            ]
            if "explicit" in kind
            else [None] * 4
        )
    assert all(u.review_state == "unreviewed" for u in result.bundle.units)
    assert_source_parts(result)


@pytest.mark.qualification
@pytest.mark.parametrize("format", ["pdf", "docx"])
def test_numbered_descendants_persist_all_overlap_owners(document_record_case, format):
    result = document_record_case("law@1", format)
    assert [row.original_value for row in result.bundle.identifiers] == [
        "Article 4",
        "1.",
        "2.",
        "4.1",
        "1.",
        "Article 5",
        "1.",
    ]
    by_id = {u.unit_id: u for u in result.bundle.units}
    article, first, second, sub, child, sibling, last = [
        by_id[r.unit_id] for r in result.bundle.identifiers
    ]
    assert first.parent_id == second.parent_id == sub.parent_id == article.unit_id
    assert child.parent_id == sub.unit_id
    assert last.parent_id == sibling.unit_id
    assert (
        article.text
        == "Article 4\n\n1. Record the sample.\n\n2. Preserve the label.\n\n4.1 Observations\n\n1. Retain the observation."
    )
    assert "Article 5" not in article.text
    assert set(child.attributes["overlapping_owner_ids"]) == {
        article.unit_id,
        sub.unit_id,
        child.unit_id,
    }
    assert child.unit_id in article.attributes["overlapping_owner_ids"]
    assert_source_parts(result)
    rows = result.owner.run_blocking_sync(
        lambda: result.owner.db.execute(
            "SELECT u.attributes_json FROM identifiers i JOIN evidence_units u ON u.unit_id=i.unit_id WHERE i.namespace='provision' AND i.normalized_value='4.1'"
        ).fetchall(),
        result.ctx,
    )
    assert len(rows) == 1
    stored = json.loads(rows[0][0])
    assert child.unit_id in stored["overlapping_owner_ids"]
    assert stored["source_parts"]
    from tychora.ingestion.documents import replay_document

    def replay():
        with result.owner.artifacts.open_verified(
            result.parsed.raw_document_ref
        ) as stream:
            manifest = stream.read()
        with result.owner.artifacts.open_verified(
            result.parsed.docling_json_ref
        ) as stream:
            sdk = stream.read()
        return replay_document(manifest, sdk, result.parsed.raw_document_ref)

    replayed = result.owner.run_blocking_sync(replay, result.ctx)
    assert (
        normalize(replayed, result.bundle.revision.revision_id, result.registry)
        == result.bundle
    )


@pytest.mark.qualification
@pytest.mark.parametrize("format", ["pdf", "docx"])
def test_grouped_source_replay_and_retry(document_record_case, format):
    from tychora.ingestion.documents import replay_document

    result = document_record_case("standard@1", format, page_break_at=3)
    assert_source_parts(result)
    unit = next(
        u for u in result.bundle.units if u.unit_id == result.bundle.records[0].unit_id
    )
    assert {loc.physical_page for loc in unit.locators} == (
        {1, 2} if format == "pdf" else {None}
    )

    def replay():
        with result.owner.artifacts.open_verified(
            result.parsed.raw_document_ref
        ) as stream:
            manifest = stream.read()
        with result.owner.artifacts.open_verified(
            result.parsed.docling_json_ref
        ) as stream:
            sdk = stream.read()
        return replay_document(manifest, sdk, result.parsed.raw_document_ref)

    result.source.unlink()
    replayed = result.owner.run_blocking_sync(replay, result.ctx)
    bundle = normalize(replayed, result.bundle.revision.revision_id, result.registry)
    assert bundle == result.bundle
    result.owner.run_blocking_sync(
        lambda: result.store.commit_extraction(bundle), result.ctx
    )
    assert bundle.revision == result.bundle.revision


@pytest.mark.qualification
@pytest.mark.parametrize("format", ["docx", "pdf"])
def test_custom_pack_matches_native_semantic_values(document_record_case, format):
    native = document_record_case("standard@1", "markdown", custom=True)
    rich = document_record_case("standard@1", format, custom=True)
    for result in (native, rich):
        assert result.bundle.revision.kind_id == "standard@1"
        assert result.registry.kinds["standard@1"]["default_lane"] == "standard"
        assert result.bundle.records[0].record_type == "rule@1"
        unit = next(
            u
            for u in result.bundle.units
            if u.unit_id == result.bundle.records[0].unit_id
        )
        assert (
            "Exigence: Record the sample before changing the instrument." in unit.text
        )
        assert "Preuve attendue: The sample identifier is quotable." in unit.text
        assert result.bundle.identifiers[0].original_value == "LOCAL-01"
        assert result.bundle.records[0].data_json["foundation_refs"] == ("REF-MISSING",)
    semantic = lambda r: {
        k: v for k, v in r.bundle.records[0].data_json.items() if k != "provenance"
    }
    assert semantic(native) == semantic(rich)
