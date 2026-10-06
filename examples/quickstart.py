"""Offline lexical evidence using the public synchronous Python API.

Set TYCHORA_MODEL_BUNDLE to a verified local output-tokenizer bundle first.
Usage: python examples/quickstart.py ./knowledge
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from tychora import AsyncTychora, QueryRequest, SourceInput, Tychora


def main(workspace: Path) -> None:
    bundle = os.environ.get("TYCHORA_MODEL_BUNDLE")
    if not bundle:
        raise SystemExit("Set TYCHORA_MODEL_BUNDLE to a verified local bundle")
    with Tychora.open(workspace) as kb:
        setup = kb.management.run(
            {
                "operation": "setup",
                "assets": ["output-tokenizer"],
                "local_bundle": bundle,
                "offline": True,
            }
        )
        assert setup.job is not None and setup.job.state == "succeeded"
        kinds = kb.discover({"subject": "kinds"})
        if not any(item.id == "manual@1" for item in kinds.definitions):
            raise SystemExit("This workspace does not register manual@1")
        job = kb.sources.add(
            SourceInput(
                type="text",
                source_key="verification-manual",
                collection_id=kb.default_collection_id,
                kind="manual@1",
                format="text",
                metadata={
                    "title": "Verification",
                    "publisher": "Example Team",
                    "edition": "1",
                },
                text="Record verification outcomes.",
            ),
            idempotency_key="verification-manual-v1",
        )
        status = kb.jobs.wait(job.id)
        if status.state != "succeeded":
            raise SystemExit(f"Source job ended in {status.state}: {status.error_code}")
        result = kb.search(
            QueryRequest.from_mapping(
                {"question": "verification", "modalities": ["lexical"]}
            )
        )
        for segment in result.segments:
            for unit_id in segment.matched_unit_ids:
                inspection = kb.evidence.get(unit_id)
                if inspection.evidence is not None:
                    print(f"{unit_id} {inspection.evidence.text}")


async def async_main(workspace: Path) -> None:
    """Equivalent public asynchronous path for an existing event loop."""
    bundle = os.environ.get("TYCHORA_MODEL_BUNDLE")
    if not bundle:
        raise SystemExit("Set TYCHORA_MODEL_BUNDLE to a verified local bundle")
    async with AsyncTychora.open(workspace) as kb:
        setup = await kb.management.run(
            {
                "operation": "setup",
                "assets": ["output-tokenizer"],
                "local_bundle": bundle,
                "offline": True,
            }
        )
        assert setup.job is not None and setup.job.state == "succeeded"
        kinds = await kb.discover({"subject": "kinds"})
        if not any(item.id == "manual@1" for item in kinds.definitions):
            raise SystemExit("This workspace does not register manual@1")
        job = await kb.sources.add(
            {
                "type": "text",
                "source_key": "verification-manual",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "metadata": {
                    "title": "Verification",
                    "publisher": "Example Team",
                    "edition": "1",
                },
                "text": "Record verification outcomes.",
            },
            idempotency_key="verification-manual-v1",
        )
        status = await kb.jobs.wait(job.id)
        if status.state != "succeeded":
            raise SystemExit(f"Source job ended in {status.state}: {status.error_code}")
        result = await kb.search(
            QueryRequest.from_mapping(
                {"question": "verification", "modalities": ["lexical"]}
            )
        )
        for segment in result.segments:
            for unit_id in segment.matched_unit_ids:
                inspection = await kb.evidence.get(unit_id)
                if inspection.evidence is not None:
                    print(f"{unit_id} {inspection.evidence.text}")


if __name__ == "__main__":
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "knowledge")
    if "--async" in sys.argv[2:]:
        asyncio.run(async_main(path))
    else:
        main(path)
