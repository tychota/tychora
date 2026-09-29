"""Async public client stays responsive while local ingestion runs."""

import asyncio
from threading import Event

import pytest

from tychora import AsyncTychora


class CharacterCounter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


@pytest.mark.asyncio
async def test_async_public_ingestion_and_heartbeat(tmp_path):
    async with AsyncTychora.open(
        tmp_path / "knowledge", token_counter=CharacterCounter()
    ) as kb:
        job = await kb.sources.add(
            {
                "type": "text",
                "source_key": "manual",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "metadata": {
                    "title": "Verification",
                    "publisher": "Example Team",
                    "edition": "1",
                },
                "text": "Record verification outcomes.",
            }
        )
        beats = 0

        async def heartbeat():
            nonlocal beats
            while beats < 3:
                await asyncio.sleep(0.01)
                beats += 1

        status, _ = await asyncio.gather(kb.jobs.wait(job.id), heartbeat())
        assert status.state == "succeeded"
        assert beats == 3
        result = await kb.search(
            {"question": "verification", "modalities": ["lexical"]}
        )
        assert result.segments
        narrowed = kb.scope.model_copy(update={"collection_ids": frozenset()})
        broad, limited = await asyncio.gather(
            kb.search({"question": "verification", "modalities": ["lexical"]}),
            kb.search(
                {"question": "verification", "modalities": ["lexical"]}, scope=narrowed
            ),
        )
        assert broad.segments
        assert not limited.segments


@pytest.mark.asyncio
async def test_cancelled_async_search_drains_before_returning(tmp_path):
    async with AsyncTychora.open(
        tmp_path / "knowledge", token_counter=CharacterCounter()
    ) as kb:
        job = await kb.sources.add(
            {
                "type": "text",
                "text": "Record verification outcomes.",
                "format": "text",
                "kind": "manual@1",
                "source_key": "manual",
                "collection_id": "default",
                "metadata": {
                    "title": "Verification",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
        assert (await kb.jobs.wait(job.id)).state == "succeeded"
        started, release = Event(), Event()
        original = kb._workspace.services.query._prepare

        async def delayed(*args):
            started.set()
            await asyncio.to_thread(release.wait)
            return await original(*args)

        kb._workspace.services.query._prepare = delayed
        search = asyncio.create_task(
            kb.search({"question": "verification", "modalities": ["lexical"]})
        )
        try:
            assert await asyncio.to_thread(started.wait, 3)
            search.cancel()
            await asyncio.sleep(0.02)
            assert not search.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await search
