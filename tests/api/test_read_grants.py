"""Shared Python entry points enforce the independent read operation grant."""

import pytest

from tychora import AsyncTychora, Tychora
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


class Counter:
    model = "synthetic-output-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text):
        return len(text.split())


def source(text="Historical alpha evidence."):
    return {
        "type": "text",
        "source_key": "manual",
        "collection_id": "default",
        "kind": "manual@1",
        "format": "text",
        "text": text,
        "metadata": {
            "title": "Verification",
            "publisher": "Example Team",
            "edition": "1",
        },
    }


@pytest.mark.parametrize(
    "subject", ["evidence", "assets", "revisions", "diagnostics", "runs", "jobs"]
)
def test_sync_inspection_requires_read_before_identity_lookup(tmp_path, subject):
    path = tmp_path / "kb"
    with Tychora.open(path, token_counter=Counter()) as kb:
        denied = kb.scope.model_copy(update={"capabilities": frozenset()})
    with Tychora.open(path, scope=denied, token_counter=Counter()) as kb:
        with pytest.raises(TychoraError) as error:
            kb.inspect({"subject": subject, "id": "unregistered"})
        assert error.value.code == "FORBIDDEN_SCOPE"
        with pytest.raises(TychoraError) as error:
            kb.relations.traverse({"unit_ids": ["unregistered"]})
        assert error.value.code == "FORBIDDEN_SCOPE"


def test_sync_search_requires_read_even_for_warm_cache(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "kb", token_counter=Counter()) as kb:
        assert kb.jobs.wait(kb.sources.add(source()).id).state == "succeeded"
        request = {"question": "alpha", "modalities": ["lexical"]}
        result = kb.search(request)
        assert result.segments

        def forbidden_preflight():
            raise AssertionError("Denied read reached tokenizer")

        monkeypatch.setattr(
            kb._workspace.services.query.counter,
            "ensure_available",
            forbidden_preflight,
            raising=False,
        )
        denied = kb.scope.model_copy(update={"capabilities": frozenset()})
        with pytest.raises(TychoraError) as error:
            kb.search(request, scope=denied)
        assert error.value.code == "FORBIDDEN_SCOPE"
        with pytest.raises(TychoraError) as replay_error:
            kb._workspace.bridge.call(
                kb._workspace.services.query.replay(
                    result.run_id, denied, ExecutionContext()
                )
            )
        assert replay_error.value.code == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio
async def test_async_search_and_inspection_require_read(tmp_path):
    path = tmp_path / "kb"
    async with AsyncTychora.open(path, token_counter=Counter()) as kb:
        assert (
            await kb.jobs.wait((await kb.sources.add(source())).id)
        ).state == "succeeded"
        denied = kb.scope.model_copy(update={"capabilities": frozenset()})
        with pytest.raises(TychoraError) as error:
            await kb.search(
                {"question": "alpha", "modalities": ["lexical"]}, scope=denied
            )
        assert error.value.code == "FORBIDDEN_SCOPE"
    async with AsyncTychora.open(path, scope=denied, token_counter=Counter()) as kb:
        for subject in (
            "evidence",
            "assets",
            "revisions",
            "diagnostics",
            "runs",
            "jobs",
        ):
            with pytest.raises(TychoraError) as error:
                await kb.inspect({"subject": subject, "id": "unregistered"})
            assert error.value.code == "FORBIDDEN_SCOPE"
        with pytest.raises(TychoraError) as error:
            await kb.relations.traverse({"unit_ids": ["unregistered"]})
        assert error.value.code == "FORBIDDEN_SCOPE"


def test_retained_citation_after_same_edition_refresh(tmp_path):
    with Tychora.open(tmp_path / "kb", token_counter=Counter()) as kb:
        assert (
            kb.jobs.wait(kb.sources.add(source(), idempotency_key="first").id).state
            == "succeeded"
        )
        result = kb.search({"question": "alpha", "modalities": ["lexical"]})
        uid = result.segments[0].matched_unit_ids[0]
        before = kb.evidence.get(uid)
        assert before.evidence is not None
        xid = before.evidence.extraction_id
        rid = kb._workspace.owner.run_blocking_sync(
            lambda: kb._workspace.owner.db.execute(
                "SELECT revision_id FROM extractions WHERE extraction_id=?", (xid,)
            ).fetchone()[0],
            ExecutionContext(),
        )
        assert (
            kb.jobs.wait(
                kb.sources.add(
                    source("Current beta evidence."), idempotency_key="second"
                ).id
            ).state
            == "succeeded"
        )
        # Retention must not rely on old published membership rows.
        owner = kb._workspace.owner
        owner.run_blocking_sync(
            lambda: owner.db.execute(
                "DELETE FROM page_members WHERE extraction_id=?", (xid,)
            ),
            ExecutionContext(),
        )
        assert kb.evidence.get(uid).evidence == before.evidence
        assert (
            kb.inspect({"subject": "revisions", "id": rid, "original": True}).payload
            == b"Historical alpha evidence."
        )
        assert kb.inspect({"subject": "diagnostics", "id": xid}).records

        owner.run_blocking_sync(
            lambda: owner.db.execute(
                "INSERT INTO review_events(unit_id,state,actor_id,created_at) VALUES (?,'rejected','reviewer','2026-01-01T00:00:00Z')",
                (uid,),
            ),
            ExecutionContext(),
        )
        with pytest.raises(TychoraError) as rejected:
            kb.evidence.get(uid)
        assert rejected.value.code == "FORBIDDEN_SCOPE"
        with pytest.raises(TychoraError) as rejected_original:
            kb.inspect({"subject": "revisions", "id": rid, "original": True})
        assert rejected_original.value.code == "FORBIDDEN_SCOPE"
        owner.run_blocking_sync(
            lambda: owner.db.execute(
                "UPDATE sources SET withdrawn_at='2026-01-01T00:00:00Z'"
            ),
            ExecutionContext(),
        )
        with pytest.raises(TychoraError) as withdrawn:
            kb.inspect({"subject": "diagnostics", "id": xid})
        assert withdrawn.value.code == "FORBIDDEN_SCOPE"


def test_public_reparse_retains_old_exact_extraction(tmp_path):
    from tychora.configuration.resolve import resolve_config

    settings = tmp_path / "settings.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    first_config = resolve_config(settings, "test", {})
    path = tmp_path / "kb"
    with Tychora.open(path, config=first_config, token_counter=Counter()) as kb:
        assert (
            kb.jobs.wait(kb.sources.add(source(), idempotency_key="old").id).state
            == "succeeded"
        )
        old = kb.search({"question": "alpha", "modalities": ["lexical"]}).segments[0]
    settings.write_text(
        settings.read_text()
        + '[configuration.extraction]\nrepresentation_version="2"\n'
    )
    next_config = resolve_config(settings, "test", {})
    with Tychora.open(path, config=next_config, token_counter=Counter()) as kb:
        job = kb.sources.add(source(), idempotency_key="new-parser")
        status = kb.jobs.wait(job.id)
        assert status.state == "succeeded", (
            status.error_code,
            tuple(kb.jobs.events(job.id)),
        )
        new = kb.search({"question": "alpha", "modalities": ["lexical"]}).segments[0]
        assert old.revision_id == new.revision_id
        assert old.extraction_id != new.extraction_id
        assert (
            kb.evidence.get(old.matched_unit_ids[0]).evidence.extraction_id
            == old.extraction_id
        )
        assert kb.inspect({"subject": "diagnostics", "id": old.extraction_id}).records
        assert (
            kb.inspect(
                {"subject": "revisions", "id": old.revision_id, "original": True}
            ).payload
            == b"Historical alpha evidence."
        )


def test_public_native_parser_change_reparses_once_then_reuses(tmp_path, monkeypatch):
    from tychora.configuration.resolve import resolve_config
    from tychora.ingestion.native import NativeParser

    settings = tmp_path / "settings.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    workspace = tmp_path / "kb"
    original_extract = NativeParser.extract
    parsed = []

    def counted_extract(self, original, config, ctx):
        parsed.append(True)
        return original_extract(self, original, config, ctx)

    monkeypatch.setattr(NativeParser, "extract", counted_extract)
    with Tychora.open(
        workspace, config=resolve_config(settings, "test", {}), token_counter=Counter()
    ) as kb:
        assert kb.jobs.wait(kb.sources.add(source()).id).state == "succeeded"
        old = kb.search({"question": "alpha", "modalities": ["lexical"]}).segments[0]
        assert len(parsed) == 1
        assert kb.jobs.wait(kb.sources.add(source()).id).state == "succeeded"
        assert len(parsed) == 1

    settings.write_text(
        settings.read_text()
        + '[configuration.extraction]\nrepresentation_version="2"\n'
    )
    with Tychora.open(
        workspace, config=resolve_config(settings, "test", {}), token_counter=Counter()
    ) as kb:
        assert kb.jobs.wait(kb.sources.add(source()).id).state == "succeeded"
        updated = kb.search({"question": "alpha", "modalities": ["lexical"]}).segments[
            0
        ]
        assert updated.revision_id == old.revision_id
        assert updated.extraction_id != old.extraction_id
        assert len(parsed) == 2
        assert kb.evidence.get(old.matched_unit_ids[0]).evidence is not None
        assert kb.jobs.wait(kb.sources.add(source()).id).state == "succeeded"
        assert len(parsed) == 2
