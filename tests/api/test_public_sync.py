"""Public Python API integration tests."""

import asyncio
from threading import Event, Thread

import pytest

from examples.knowledge_adapter import retrieve_segments
from tychora import (
    DiscoverRequest,
    OutputProfile,
    QueryRequest,
    SourceInput,
    Tychora,
    format_result,
)


class CharacterCounter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def test_public_open_discovers_builtin_kinds_and_closes(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=CharacterCounter()) as kb:
        result = kb.discover(DiscoverRequest(subject="kinds"))
        assert any(item.id == "manual@1" for item in result.definitions)
    try:
        kb.discover(DiscoverRequest(subject="kinds"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("A closed public client must reject operations")


def test_public_api_ingests_text_without_cli_or_temporary_input(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=CharacterCounter()) as kb:
        job = kb.sources.add(
            SourceInput(
                type="text",
                source_key="manual",
                collection_id=kb.default_collection_id,
                kind="manual@1",
                format="text",
                metadata={
                    "title": "Verification",
                    "publisher": "Example Team",
                    "edition": "1",
                },
                text="Record verification outcomes.",
            )
        )
        status = kb.jobs.wait(job.id)
        assert status.state == "succeeded", (
            status.error_code,
            tuple(kb.jobs.events(job.id)),
        )
        result = kb.search(
            QueryRequest.from_mapping(
                {"question": "verification", "modalities": ["lexical"]}
            )
        )
        assert result.segments
        assert "Record verification outcomes." in format_result(
            result,
            OutputProfile(renderer="canonical-json@1"),
            counter=CharacterCounter(),
        )
        unit = kb.evidence.get(result.segments[0].matched_unit_ids[0])
        assert unit.evidence is not None
        assert unit.evidence.text == "Record verification outcomes."
        adapted = retrieve_segments(kb, "verification", limit=1)
        assert len(adapted) == 1
        assert adapted[0]["id"] == result.segments[0].segment_id
        assert adapted[0]["metadata"]["id"] == adapted[0]["id"]
        assert adapted[0]["metadata"]["unit_ids"] == list(
            result.segments[0].matched_unit_ids
        )


def test_public_add_returns_before_extraction_and_deduplicates_pending_job(tmp_path):
    started, release = Event(), Event()
    with Tychora.open(tmp_path / "knowledge", token_counter=CharacterCounter()) as kb:
        original = kb._workspace.services.sources._extract

        async def delayed(*args):
            started.set()
            await asyncio.to_thread(release.wait)
            return await original(*args)

        kb._workspace.services.sources._extract = delayed
        source = {
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
        first = kb.sources.add(source, idempotency_key="one")
        try:
            assert started.wait(3)
            assert kb.jobs.get(first.id).state == "running"
            second = kb.sources.add(source, idempotency_key="one")
            assert second.id == first.id
            assert kb.jobs.cancel(first.id).id == first.id
        finally:
            release.set()
        assert kb.jobs.wait(first.id).state == "cancelled"


def test_close_waits_for_accepted_extraction_to_exit(tmp_path):
    started, release, finished = Event(), Event(), Event()
    kb = Tychora.open(tmp_path / "knowledge", token_counter=CharacterCounter())
    original = kb._workspace.services.sources._extract

    async def delayed(*args):
        started.set()
        await asyncio.to_thread(release.wait)
        try:
            return await original(*args)
        finally:
            finished.set()

    kb._workspace.services.sources._extract = delayed
    job = kb.sources.add(
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
    assert started.wait(3)
    closer = Thread(target=kb.close)
    closer.start()
    try:
        assert not finished.wait(0.05)
        assert closer.is_alive()
    finally:
        release.set()
    closer.join(timeout=3)
    assert not closer.is_alive()
    assert finished.is_set()
    with Tychora.open(
        tmp_path / "knowledge", token_counter=CharacterCounter()
    ) as reopened:
        assert reopened.jobs.get(job.id).state == "cancelled"


@pytest.mark.asyncio
async def test_sync_public_calls_inside_active_event_loop_block_and_complete(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=CharacterCounter()) as kb:
        job = kb.sources.add(
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
        assert kb.jobs.wait(job.id).state == "succeeded"
        assert kb.search(
            {"question": "verification", "modalities": ["lexical"]}
        ).segments
