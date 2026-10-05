"""Framework-neutral evidence adapter using only Tychora's public API."""

from __future__ import annotations

from tychora import QueryRequest, Tychora


def retrieve_segments(kb: Tychora, question: str, *, limit: int = 8) -> list[dict]:
    """Return one record per authorized segment; no reembedding or answer synthesis."""
    request = QueryRequest.from_mapping({"question": question, "limit": limit})
    result = kb.search(request)
    records: list[dict] = []
    for segment in result.segments[:limit]:
        asset_ids = sorted(
            {citation.asset_id for citation in segment.citations if citation.asset_id}
        )
        derived = sorted(
            proxy.proxy_id
            for visual in result.visuals
            if visual.asset_id in asset_ids
            for proxy in visual.proxies
            if proxy.origin == "derived"
        )
        records.append(
            {
                "id": segment.segment_id,
                "text": segment.text,
                "metadata": {
                    "id": segment.segment_id,
                    "unit_ids": list(segment.matched_unit_ids),
                    "context_unit_ids": list(segment.context_only_unit_ids),
                    "source_id": segment.source_id,
                    "revision_id": segment.revision_id,
                    "extraction_id": segment.extraction_id,
                    "locators": [
                        locator.model_dump(mode="json")
                        for citation in segment.citations
                        for locator in citation.locators
                    ],
                    "role": segment.role,
                    "derived": derived,
                    "asset_ids": asset_ids,
                    "run_id": result.run_id,
                    "snapshot_id": result.snapshot_id,
                },
            }
        )
    return records
