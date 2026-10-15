"""Regenerate dashboard examples from real HTTP and a temporary synthetic corpus.

Run from the repository root: PYTHONPATH=src python dashboard/scripts/generate_contracts.py
No external providers, credentials, or persistent workspace are used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from hashlib import sha256
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from tychora import Tychora
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth


class SyntheticCounter:
    model = "synthetic-output"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def redact_paths(value):
    """Example files must not preserve local filesystem locations."""
    if isinstance(value, dict):
        return {key: redact_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_paths(item) for item in value]
    if isinstance(value, str) and value.startswith(("/", "file://")):
        return "<redacted-path>"
    return value


async def capture(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix="tychora-dashboard-contracts-") as directory,
        Tychora.open(
            Path(directory) / "workspace", token_counter=SyntheticCounter()
        ) as kb,
    ):
        # This throwaway credential is never written to fixtures or printed.
        credential = "synthetic-fixture-only"
        app = create_app(
            kb._workspace.owner,
            kb._workspace.services,
            LocalAuth({credential: kb.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": f"Bearer {credential}"},
        ) as client:

            async def request(name: str, method: str, url: str, **kwargs):
                response = await client.request(method, url, **kwargs)
                response.raise_for_status()
                value = redact_paths(response.json())
                (output / f"{name}.json").write_text(json.dumps(value, indent=2) + "\n")
                return value

            await request("handshake", "GET", "/v1/handshake")
            await request("discovery-kinds", "GET", "/v1/discovery/kinds?limit=4")
            for kind in ("document", "manual"):
                await request(
                    f"task4-{kind}-metadata-schema",
                    "GET",
                    f"/v1/discovery/kinds/{kind}@1/metadata-schema",
                )
            receipt = await request(
                "source-add",
                "POST",
                "/v1/sources",
                json={
                    "type": "text",
                    "source_key": "dashboard-synthetic-manual",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "text",
                    "text": "Record synthetic verification outcomes for the dashboard fixture.",
                    "metadata": {
                        "title": "Synthetic verification",
                        "publisher": "Fixture",
                        "edition": "1",
                    },
                },
            )
            for _ in range(200):
                response = await client.get(f"/v1/jobs/{receipt['id']}")
                response.raise_for_status()
                if response.json()["state"] in {"succeeded", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.05)
            if response.json()["state"] != "succeeded":
                raise RuntimeError("Synthetic source job did not succeed")
            await request("job-status", "GET", f"/v1/jobs/{receipt['id']}")
            await request("job-events", "GET", f"/v1/jobs/{receipt['id']}/events/page")
            sources = await request("sources", "GET", "/v1/sources")
            source = sources["definitions"][0]
            revisions = await request(
                "source-revisions", "GET", f"/v1/sources/{source['id']}/revisions"
            )
            revision_id = revisions["revisions"][0]["revision_id"]
            extraction_id = revisions["revisions"][0]["extractions"][0]["extraction_id"]
            content_path = (
                f"/v1/sources/{source['id']}/revisions/{revision_id}"
                f"/extractions/{extraction_id}"
            )
            await request("extraction-units", "GET", content_path + "/units")
            await request("extraction-windows", "GET", content_path + "/windows")

            query = await request(
                "query",
                "POST",
                "/v1/queries",
                json={
                    "question": "verification",
                    "modalities": ["lexical"],
                    "trace": True,
                },
            )
            unit_id = query["segments"][0]["matched_unit_ids"][0]
            await request("evidence", "GET", f"/v1/evidence/{unit_id}")
            await request(
                "original",
                "POST",
                "/v1/inspect",
                json={
                    "subject": "revisions",
                    "id": revision_id,
                    "view": "original",
                },
            )
            await request("query-history", "GET", "/v1/queries")
            await request("query-run", "GET", f"/v1/queries/{query['run_id']}")
            case = {
                "case_id": "synthetic-verification",
                "family_key": "synthetic-verification",
                "split": "test",
                "query": {"question": "verification", "modalities": ["lexical"]},
                "expected_source_ids": [source["id"]],
                "expected_revision_ids": [revision_id],
                "expected_unit_ids": [unit_id],
                "label_provenance": "synthetic fixture text and public HTTP evidence identity",
            }
            cases = json.dumps(case, separators=(",", ":")) + "\n"
            dataset = await request(
                "evaluation-dataset",
                "POST",
                "/v1/evaluations/datasets",
                json={
                    "manifest_json": json.dumps(
                        {
                            "format": "tychora-gold@1",
                            "dataset_id": "dashboard-synthetic-v1",
                            "name": "Synthetic dashboard contract",
                            "version_sha": sha256(cases.encode()).hexdigest(),
                            "case_count": 1,
                        }
                    ),
                    "cases_jsonl": cases,
                },
            )
            evaluation = await request(
                "evaluation-receipt",
                "POST",
                "/v1/evaluations",
                json={
                    "dataset_id": dataset["dataset_id"],
                    "split": "test",
                    "snapshot_id": query["snapshot_id"],
                    "offline": False,
                    "variants": [
                        {
                            "name": "baseline",
                            "profile": kb._workspace.owner.config.profile,
                        }
                    ],
                },
            )
            await request("evaluation-list", "GET", "/v1/evaluations")
            await request(
                "evaluation-detail", "GET", f"/v1/evaluations/{evaluation['id']}"
            )
        await app.state.runtime.services.source_jobs.aclose()


async def capture_pdf(output: Path) -> None:
    """Capture actual status envelopes without invoking recognition or indexing."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from dashboard.e2e.pdf_fixture import generate_pdf

    output.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix="tychora-pdf-contracts-") as directory,
        Tychora.open(
            Path(directory) / "workspace", token_counter=SyntheticCounter()
        ) as kb,
    ):
        app = create_app(
            kb._workspace.owner,
            kb._workspace.services,
            LocalAuth({"synthetic-pdf-capture": kb.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer synthetic-pdf-capture"},
        ) as client:
            for mode in ("native", "scan", "mixed", "blank"):
                data = generate_pdf(mode)
                prepared = await client.post(
                    "/v1/pdf-intakes",
                    json={
                        "source_key": f"synthetic-{mode}",
                        "collection_id": str(kb.default_collection_id),
                        "kind": "document@1",
                        "metadata": {"title": f"Synthetic {mode}"},
                        "expected_size_bytes": len(data),
                        "format": "pdf",
                    },
                )
                prepared.raise_for_status()
                path = "/v1/pdf-intakes/" + prepared.json()["intake_id"]
                uploaded = await client.put(
                    path + "/content",
                    content=data,
                    headers={"Content-Type": "application/pdf"},
                )
                uploaded.raise_for_status()
                analysed = await client.post(path + "/analyse")
                analysed.raise_for_status()
                value = redact_pdf(analysed.json())
                (output / f"task6-pdf-{mode}-status.json").write_text(
                    json.dumps(value, indent=2) + "\n"
                )
                (await client.delete(path)).raise_for_status()
        await app.state.runtime.services.source_jobs.aclose()


def redact_pdf(value):
    """Opaque private intake receipts and volatile expiry never enter examples."""
    if isinstance(value, dict):
        return {
            key: "<synthetic-receipt>"
            if key == "intake_id"
            else "2030-01-01T00:00:00Z"
            if key == "expires_at"
            else redact_pdf(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_pdf(item) for item in value]
    return redact_paths(value)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dashboard/contracts"))
    parser.add_argument(
        "--pdf",
        action="store_true",
        help="Capture generated PDF analysis envelopes; verified local assets are explicit prerequisites",
    )
    parser.add_argument(
        "--pdf-evidence",
        type=Path,
        help="Export a synthetic Task6 real HTTP browser capture without repeating OCR",
    )
    args = parser.parse_args()
    if args.pdf_evidence:
        capture_value = json.loads(args.pdf_evidence.read_text())
        args.output.mkdir(parents=True, exist_ok=True)
        for key in (
            "analysed",
            "accepted",
            "events",
            "excluded",
            "included",
            "evidence",
            "windows",
            "units",
            "coverage",
            "diagnostics",
        ):
            (args.output / f"task6-pdf-{key}.json").write_text(
                json.dumps(redact_pdf(capture_value[key]), indent=2) + "\n"
            )
    else:
        asyncio.run(capture_pdf(args.output) if args.pdf else capture(args.output))
