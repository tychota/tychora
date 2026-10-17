"""Async public client stays responsive while local ingestion runs."""

import asyncio
from contextlib import contextmanager
from threading import Event

import pytest
from PIL import Image

from tests.helpers.assets import asset_path
from tests.helpers.timing import scaled
from tychora import AsyncTychora
from tychora.adapters.docling.parser import DoclingParser
from tychora.configuration.resolve import resolve_config
from tychora.contracts.identity import source_id
from tychora.ports.execution import ExecutionContext

# Parser and OCR jobs are slow on loaded CI runners; the API caps one wait at 300 s.
JOB_WAIT = min(scaled(60), 300)


class CharacterCounter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def _simple_pdf() -> bytes:
    commands = b"BT /F1 14 Tf 72 720 Td (Record verification outcomes) Tj ET"
    stream = b"<< /Length %d >>\nstream\n" % len(commands) + commands + b"\nendstream"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        stream,
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

        status, _ = await asyncio.gather(
            kb.jobs.wait(job.id, timeout=JOB_WAIT), heartbeat()
        )
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
        assert (await kb.jobs.wait(job.id, timeout=JOB_WAIT)).state == "succeeded"
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


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
@pytest.mark.asyncio
async def test_two_public_queries_complete_while_parser_job_is_held(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "TYCHORA_MODEL_CACHE", str(asset_path("TYCHORA_TEST_MODEL_CACHE"))
    )
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(_simple_pdf())
    picture = tmp_path / "held.png"
    Image.new("RGB", (8, 6), "blue").save(picture)
    settings = tmp_path / "settings.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[configuration.extraction]\nparser="docling"\n'
        f'[configuration.acquisition]\nroots=["{tmp_path}"]\n'
    )
    config = resolve_config(settings, "test", {})
    entered, release = Event(), Event()
    original = DoclingParser.aextract

    async def held_parser(self, *args):
        entered.set()
        await asyncio.to_thread(release.wait)
        return await original(self, *args)

    async with AsyncTychora.open(
        tmp_path / "knowledge", config=config, token_counter=CharacterCounter()
    ) as kb:
        first = await kb.sources.add(
            {
                "type": "file",
                "path": str(pdf),
                "source_key": "manual",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "pdf",
                "metadata": {
                    "title": "Verification",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
        first_status = await kb.jobs.wait(first.id, timeout=min(scaled(120), 300))
        assert first_status.state == "succeeded", first_status.error_code
        monkeypatch.setattr(DoclingParser, "aextract", held_parser)
        parser_job = await kb.sources.add(
            {
                "type": "file",
                "path": str(picture),
                "format": "png",
                "source_key": "held-image",
                "collection_id": "default",
                "kind": "manual@1",
                "metadata": {
                    "title": "Held image",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            narrowed = kb.scope.model_copy(update={"collection_ids": frozenset()})
            async with asyncio.timeout(5):
                visible, denied, heartbeat = await asyncio.gather(
                    kb.search({"question": "verification", "modalities": ["lexical"]}),
                    kb.search(
                        {"question": "record", "modalities": ["lexical"]},
                        scope=narrowed,
                    ),
                    kb.discover({"subject": "kinds"}),
                )
            assert entered.is_set() and not release.is_set()
            assert visible.segments, (visible.warnings, visible.coverage)
            assert not denied.segments
            assert heartbeat.subject == "kinds"
        finally:
            release.set()
        assert (
            await kb.jobs.wait(parser_job.id, timeout=min(scaled(20), 300))
        ).state == "succeeded"


@pytest.mark.asyncio
async def test_cancelled_public_original_inspection_keeps_payload_pinned(
    tmp_path, monkeypatch
):
    from tychora.application.removal import RemovalService

    async with AsyncTychora.open(
        tmp_path / "knowledge", token_counter=CharacterCounter()
    ) as kb:
        job = await kb.sources.add(
            {
                "type": "text",
                "text": "Private synthetic original.",
                "format": "text",
                "kind": "manual@1",
                "source_key": "leased-original",
                "collection_id": "default",
                "metadata": {
                    "title": "Lease",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
        assert (await kb.jobs.wait(job.id, timeout=JOB_WAIT)).state == "succeeded"
        sid = source_id(kb.default_collection_id, "leased-original")
        revision = (await kb.sources.revisions(sid)).revisions[0].revision_id
        owner = kb._workspace.owner
        sha = await owner.run_blocking(
            lambda: owner.db.execute(
                "SELECT original_sha FROM revisions WHERE revision_id=?", (revision,)
            ).fetchone()[0],
            ExecutionContext(),
        )
        entered, release = Event(), Event()
        original_open = owner.artifacts.open_verified

        @contextmanager
        def held_open(ref):
            with original_open(ref) as stream:
                if ref.sha256 == sha:
                    entered.set()
                    assert release.wait(20)
                yield stream

        monkeypatch.setattr(owner.artifacts, "open_verified", held_open)
        pending = asyncio.create_task(
            kb.inspect({"subject": "revisions", "id": revision, "original": True})
        )
        removal = None
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            pending.cancel()
            await asyncio.sleep(0.02)
            assert not pending.done()
            assert owner.artifacts.payload_is_pinned(sha)
            removal = asyncio.create_task(
                kb.management.run({"operation": "remove", "source_ids": [sid]})
            )
            await asyncio.sleep(0.05)
            assert not removal.done()
            assert owner.artifacts.payload_is_pinned(sha)
            assert (owner.path / "artifacts" / sha).read_bytes() == (
                b"Private synthetic original."
            )
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert removal is not None
        removed = await removal
        assert removed.job is not None and removed.job.state == "succeeded"
        assert not owner.artifacts.payload_is_pinned(sha)
        purged = await kb.management.run(
            {
                "operation": "purge-originals",
                "source_id": sid,
                "revision_ids": [revision],
                "confirm_original_removal": True,
            }
        )
        assert purged.job is not None and purged.job.state == "succeeded"
        counts = await owner.run_blocking(
            lambda: RemovalService(
                owner, kb._workspace.services.jobs.repository
            ).reconcile_pending_deletions(),
            ExecutionContext(),
        )
        assert purged.deleted_payloads + counts["deleted_payloads"] == 1
        assert not (owner.path / "artifacts" / sha).exists()
