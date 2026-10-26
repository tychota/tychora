"""Synthetic parser-shaped split table retains exact page and row provenance."""

import io
from hashlib import sha256
from uuid import UUID

import pytest

from tests.evaluation.conftest import SyntheticCounter
from tychora.adapters.docling.parser import DoclingParser
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.application.inspection import InspectionService
from tychora.application.owner import StagedOutput, WorkspaceOwner
from tychora.application.query import QueryService
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.composition import DocumentCompositionPlan
from tychora.contracts.corpus import CollectionRow, RevisionRow, SourceRow, WorkspaceRow
from tychora.contracts.documents import DocumentRegistration
from tychora.contracts.identity import canonical_bytes, digest, revision_id, source_id
from tychora.contracts.operations import (
    ConfigurationVariant,
    EvaluationRequest,
    InspectRequest,
)
from tychora.contracts.requests import QueryRequest, RequestScope, SourceInput
from tychora.evaluation.provenance import verify_result_locators
from tychora.evaluation.runner import EvaluationService
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ingestion.acquire import Acquirer
from tychora.ingestion.composition import compose_document
from tychora.ports.execution import ExecutionContext

COLLECTION = "55825936-6a6a-4d29-945a-327d771a76b7"


def _two_page_pdf() -> bytes:
    """Small public PDF whose text agrees with the declared parser facts."""

    def stream(text):
        commands = f"BT /F1 10 Tf 10 50 Td ({text}) Tj ET".encode()
        return (
            f"<< /Length {len(commands)} >>\nstream\n".encode()
            + commands
            + b"\nendstream"
        )

    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        stream("Sample Value Earlier 0"),
        stream("Table 1 continued Sample Value Needle 1"),
    )
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(result)


def _table(index, page, value):
    cells = []
    for row, pair in enumerate((("Sample", "Value"), (value, str(index)))):
        for column, text in enumerate(pair):
            cells.append(
                {
                    "text": text,
                    "start_row_offset_idx": row,
                    "end_row_offset_idx": row + 1,
                    "start_col_offset_idx": column,
                    "end_col_offset_idx": column + 1,
                    "column_header": row == 0,
                }
            )
    return {
        "self_ref": f"#/tables/{index}",
        "parent": {"$ref": "#/body"},
        "children": [],
        "label": "table",
        "captions": ([{"$ref": "#/texts/0"}] if index == 1 else []),
        "prov": [
            {
                "page_no": page,
                "charspan": [0, 1],
                "bbox": {
                    "l": 10.0,
                    "t": 80.0 if page == 1 else 1.0,
                    "r": 90.0,
                    "b": 99.0 if page == 1 else 20.0,
                    "coord_origin": "TOPLEFT",
                },
            }
        ],
        "data": {"num_rows": 2, "num_cols": 2, "table_cells": cells},
    }


@pytest.mark.asyncio
async def test_parser_shaped_split_table_composes_without_model_download(tmp_path):
    import pypdfium2

    document = tmp_path / "split.pdf"
    document.write_bytes(_two_page_pdf())
    with pypdfium2.PdfDocument(document) as pdf:
        assert len(pdf) == 2
        assert all(
            word in pdf[index].get_textpage().get_text_range()
            for index, word in enumerate(("Earlier", "Needle"))
        )
    settings = tmp_path / "settings.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        f'[configuration.acquisition]\nroots=["{tmp_path}"]\n'
        # SyntheticCounter charges one token per serialized character; this
        # scenario needs room to deliver both the table and its source row.
        "[configuration.limits]\nmax_tokens=12000\n"
    )
    config = resolve_config(settings, "test", {})
    registry = Registry.from_config(config)
    scope = RequestScope(
        principal_id="synthetic-reader",
        collection_ids=frozenset({COLLECTION}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup", "compose"}),
    )
    with WorkspaceOwner.open(tmp_path / "workspace", config, scope) as owner:
        context = ExecutionContext()
        await owner.run_write(
            lambda: owner.db.execute(
                "INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) "
                'VALUES (?,?,?,?,0,\'{"schema_version":1,"upload_providers":[]}\')',
                (COLLECTION, owner.workspace_id, "manuals", "Manuals"),
            ),
            context,
        )
        metadata = FrozenJsonMap(
            {"title": "Synthetic table", "publisher": "Example", "edition": "1"}
        )
        captured = await owner.run_write(
            lambda: Acquirer(owner.artifacts, config).capture(
                SourceInput.from_mapping(
                    {
                        "type": "file",
                        "path": str(document),
                        "source_key": "split.pdf",
                        "collection_id": COLLECTION,
                        "kind": "manual@1",
                        "metadata": metadata,
                    }
                ),
                context,
            ),
            context,
        )
        sid = source_id(UUID(COLLECTION), "split.pdf")
        metadata_sha = digest("metadata-v1", metadata)
        created_at = await owner.run_write(
            lambda: owner.db.execute("SELECT created_at FROM workspaces").fetchone()[0],
            context,
        )
        registration = DocumentRegistration(
            capture=captured,
            workspace=WorkspaceRow(
                workspace_id=owner.workspace_id,
                created_at=created_at,
            ),
            collection=CollectionRow(
                collection_id=COLLECTION,
                workspace_id=owner.workspace_id,
                slug="manuals",
                title="Manuals",
            ),
            source=SourceRow(
                source_id=sid,
                collection_id=COLLECTION,
                source_key="split.pdf",
                created_at=captured.captured_at.isoformat(),
            ),
            revision=RevisionRow(
                revision_id=revision_id(sid, captured.original_sha, "1", metadata_sha),
                source_id=sid,
                kind_id="manual@1",
                original_sha=captured.original_sha,
                edition_key="1",
                metadata_sha=metadata_sha,
                title="Synthetic table",
                metadata_json=metadata,
                captured_at=captured.captured_at.isoformat(),
                allowed_uses_json=("lookup", "compose"),
            ),
            format="pdf",
        )
        export = {
            "schema_name": "DoclingDocument",
            "version": "1.10.0",
            "name": "Synthetic split table",
            "body": {
                "self_ref": "#/body",
                "children": [
                    {"$ref": "#/tables/0"},
                    {"$ref": "#/texts/0"},
                    {"$ref": "#/tables/1"},
                ],
            },
            "furniture": {"self_ref": "#/furniture", "children": []},
            "texts": [
                {
                    "self_ref": "#/texts/0",
                    "parent": {"$ref": "#/body"},
                    "children": [],
                    "label": "caption",
                    "text": "Table 1 continued",
                    "prov": [
                        {
                            "page_no": 2,
                            "charspan": [0, 17],
                            "bbox": {
                                "l": 10.0,
                                "t": 0.0,
                                "r": 90.0,
                                "b": 1.0,
                                "coord_origin": "TOPLEFT",
                            },
                        }
                    ],
                }
            ],
            "groups": [],
            "pictures": [],
            "tables": [_table(0, 1, "Earlier"), _table(1, 2, "Needle")],
            "pages": {
                "1": {"page_no": 1, "size": {"width": 100.0, "height": 100.0}},
                "2": {"page_no": 2, "size": {"width": 100.0, "height": 100.0}},
            },
        }
        kind = registry.kinds["manual@1"]
        assert isinstance(kind, FrozenJsonMap)
        parser = DoclingParser(owner, registration, tmp_path)
        encoded = canonical_bytes(export)
        parsed = await owner.run_write(
            lambda: parser._retain(
                (
                    (
                        StagedOutput("document.json", "application/json", len(encoded)),
                        io.BytesIO(encoded),
                    ),
                ),
                captured.original_ref,
                config,
                registry,
                kind,
                FrozenJsonMap(),
            ),
            context,
        )
        result = await owner.run_write(
            lambda: compose_document(
                parsed,
                registry,
                DocumentCompositionPlan(
                    base_normalizer_fingerprint=parsed.normalizer_fingerprint
                ),
                owner.artifacts,
                context,
            ),
            context,
        )
        assert len(result.fragments) == 2
        assert any(
            "Needle" in unit.text and unit.unit_type == "table_row"
            for unit in result.bundle.units
        )
        assert (
            result.assertions and result.assertions[0].predicate == "continues_table@1"
        )
        config_ref = await owner.run_write(
            lambda: owner.artifacts.put_stream(
                io.BytesIO(
                    canonical_bytes(
                        {
                            "type": "registry",
                            "schema_version": "1",
                            "data": config.model_dump(mode="json"),
                        }
                    )
                ),
                "application/json",
            ),
            context,
        )

        def publish():
            jobs = JobRepository(
                owner.db, artifacts=owner.artifacts, workspace_id=owner.workspace_id
            )
            coordinator = ProjectionCoordinator(
                store=owner.store,
                artifacts=owner.artifacts,
                config=config,
                counter=SyntheticCounter(),
            )
            job = jobs.accept("add", scope, None, {"source": "split.pdf"}, config_ref)
            jobs.start(job.id)
            plan = coordinator.build(
                result.bundle, owner.store.active_snapshot_id(), job, context
            )
            return owner.store.publish_snapshot(plan)

        snapshot_id = await owner.run_write(publish, context)
        query = QueryService(owner, output_counter=SyntheticCounter())
        delivered = await query.search(
            QueryRequest.from_mapping(
                {
                    "question": "Needle",
                    "modalities": ["lexical"],
                    "mode": "lookup",
                    "budgets": {"max_tokens": 12000},
                }
            ),
            scope,
            context,
        )
        assert delivered.result.snapshot_id == snapshot_id
        assert any("Needle" in segment.text for segment in delivered.result.segments)
        row = next(
            unit
            for unit in result.bundle.units
            if unit.unit_type == "table_row" and "Needle" in unit.text
        )
        table = next(
            unit
            for unit in result.bundle.units
            if unit.unit_type == "table" and "Needle" in unit.text
        )
        assert any(
            locator.type == "document" and locator.physical_page == 2
            for locator in row.locators
        )
        assert {table.unit_id, row.unit_id} <= {
            unit_id
            for segment in delivered.result.segments
            for unit_id in segment.matched_unit_ids
        }
        lexical_hits = await owner.run_write(
            lambda: {
                unit_id
                for (unit_id,) in owner.db.execute(
                    "SELECT c.unit_id FROM lexical_fts "
                    "JOIN lexical_records r ON r.rowid=lexical_fts.rowid "
                    "JOIN chunks c ON c.chunk_id=r.chunk_id "
                    "JOIN snapshot_batches b ON b.batch_id=r.batch_id "
                    "WHERE lexical_fts MATCH 'Needle' AND b.snapshot_id=? "
                    "AND b.slot='lexical' AND c.unit_id IN (?,?)",
                    (snapshot_id, table.unit_id, row.unit_id),
                )
            },
            context,
        )
        assert lexical_hits == {table.unit_id, row.unit_id}
        inspected_table = await InspectionService(owner).inspect(
            InspectRequest(subject="evidence", id=table.unit_id, view="full"),
            scope,
            context,
        )
        assert inspected_table.evidence is not None
        assert inspected_table.evidence.text == table.text
        assert any(
            locator.type == "document" and locator.physical_page == 2
            for locator in inspected_table.evidence.locators
        )
        assert row.attributes["source_refs"] == ("#/tables/1",)
        assert any(
            locator.physical_page == 2 and locator.native_ref == "split.pdf"
            for segment in delivered.result.segments
            for citation in segment.citations
            for locator in citation.locators
            if locator.type == "document"
        )
        verified = await owner.run_write(
            lambda: verify_result_locators(owner, delivered.result), context
        )
        assert verified.valid == verified.total and verified.total > 0

        start = row.text.index("Needle")
        case = {
            "case_id": "table-split",
            "family_key": "table-split",
            "split": "test",
            "benchmark": "integrity",
            "label_provenance": "declared two-page PDF, parser-shaped table cells, canonical retained table and row identities",
            "query": {
                "question": "Needle",
                "modalities": ["lexical"],
                "mode": "lookup",
                "budgets": {"max_tokens": 12000},
            },
            "expected_source_ids": [sid],
            "expected_revision_ids": [registration.revision.revision_id],
            "expected_unit_ids": [row.unit_id],
            "expected_spans": [
                {
                    "unit_id": row.unit_id,
                    "char_start": start,
                    "char_end": start + len("Needle"),
                }
            ],
        }
        cases = canonical_bytes(case) + b"\n"
        manifest = canonical_bytes(
            {
                "format": "tychora-gold@1",
                "dataset_id": "synthetic-table-v1",
                "name": "Synthetic split table",
                "version_sha": sha256(cases).hexdigest(),
                "case_count": 1,
            }
        )
        jobs = await owner.run_write(
            lambda: JobRepository(
                owner.db, artifacts=owner.artifacts, workspace_id=owner.workspace_id
            ),
            context,
        )
        evaluation = EvaluationService(owner, query, jobs)
        await evaluation.register_dataset_bytes(manifest, cases, scope, context)
        handle = await evaluation.run(
            EvaluationRequest(
                dataset_id="synthetic-table-v1",
                split="test",
                snapshot_id=snapshot_id,
                variants=(
                    ConfigurationVariant.from_mapping(
                        {
                            "name": "lexical",
                            "profile": "test",
                            "overrides": {"channels": ["lexical"]},
                        }
                    ),
                ),
            ),
            scope,
            context,
        )
        assert handle.state == "succeeded"
        report = await evaluation.get(handle.id, scope, context)
        metrics = {
            metric["name"]: metric["value"]
            for metric in report["cases"]["table-split"]["runs"][0]["metrics"][
                "metrics"
            ]
        }
        assert metrics["unit_recall"] == 1.0
        assert metrics["span_completeness"] == 1.0
        assert metrics["locator_validity"] == 1.0
