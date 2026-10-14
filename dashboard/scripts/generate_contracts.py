"""Regenerate dashboard examples from real HTTP and a temporary synthetic corpus.

Run from the repository root: PYTHONPATH=src python dashboard/scripts/generate_contracts.py
No external providers, credentials, or persistent workspace are used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dashboard/contracts"))
    asyncio.run(capture(parser.parse_args().output))
