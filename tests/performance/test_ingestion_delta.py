"""Measure real repository parse, embedding, and projection work by delta state."""

from typing import TypedDict

import pytest

from tests.code.conftest import git, make_repo
from tests.indexing.test_publication import RecordingCounter, RecordingEmbedder
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.application.owner import WorkspaceOwner
from tychora.application.sources import SourceService
from tychora.configuration.resolve import resolve_config
from tychora.contracts.requests import RequestScope, SourceInput
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ports.execution import ExecutionContext


class _Observed(TypedDict):
    parsed_files: int
    embedded_windows: int
    projected_units: int
    capture_dirty: bool | None


@pytest.mark.asyncio
async def test_repository_ingestion_delta_matrix_uses_actual_pipeline(
    tmp_path, monkeypatch
):
    repo = make_repo(tmp_path)
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        f'[configuration.acquisition]\nroots=["{tmp_path}"]\n'
        '[configuration.extraction]\nparser="code"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ndimensions=2\n'
        '[configuration.index.text]\nbackend="chroma"\n'
    )
    config = resolve_config(settings, "test", {})
    collection = "55825936-6a6a-4d29-945a-327d771a76b7"
    scope = RequestScope(
        principal_id="synthetic-writer",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup", "compose"}),
        upload_providers=frozenset({"voyage"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_blocking(
            lambda: owner.db.execute(
                "INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) "
                "VALUES (?,?,?,?,0,?)",
                (
                    collection,
                    owner.workspace_id,
                    "code",
                    "Synthetic code",
                    '{"schema_version":1,"upload_providers":["voyage"]}',
                ),
            ),
            ExecutionContext(),
        )
        counter = RecordingCounter()
        embedder = RecordingEmbedder(counter)
        coordinator = ProjectionCoordinator(
            store=owner.store,
            artifacts=owner.artifacts,
            config=config,
            counter=counter,
            text_embedder=embedder,
            text_index=ChromaVectorIndex(tmp_path / "vectors"),
        )
        service = SourceService(owner, coordinator)
        _, acquirer = await service._ports(ExecutionContext())
        source = SourceInput.from_mapping(
            {
                "type": "repository",
                "path": str(repo),
                "kind": "code_file@1",
                "metadata": {"title": "Synthetic repository"},
                "source_key": "repository",
                "collection_id": collection,
            }
        )
        observed: _Observed = {
            "parsed_files": 0,
            "embedded_windows": 0,
            "projected_units": 0,
            "capture_dirty": None,
        }
        original_parse = owner.run_parser
        original_embed = embedder.embed_groups
        original_prepare = coordinator.prepare
        original_capture = acquirer.capture

        def measured_capture(source, ctx):
            captured = original_capture(source, ctx)
            observed["capture_dirty"] = captured.dirty
            return captured

        async def measured_parse(parser, artifacts, ctx):
            observed["parsed_files"] += 1
            return await original_parse(parser, artifacts, ctx)

        def measured_embed(groups, ctx):
            observed["embedded_windows"] += sum(
                len(group.window_ids) for group in groups
            )
            return original_embed(groups, ctx)

        def measured_prepare(bundle, base, job, ctx):
            prepared = original_prepare(bundle, base, job, ctx)
            observed["projected_units"] += len(prepared.bundle.units)
            return prepared

        monkeypatch.setattr(acquirer, "capture", measured_capture)
        monkeypatch.setattr(owner, "run_parser", measured_parse)
        monkeypatch.setattr(embedder, "embed_groups", measured_embed)
        monkeypatch.setattr(coordinator, "prepare", measured_prepare)
        samples = []

        async def run(label):
            observed.update(
                parsed_files=0,
                embedded_windows=0,
                projected_units=0,
                capture_dirty=None,
            )
            handle = await service.add(source, scope)
            assert handle.state == "succeeded"
            samples.append((label, dict(observed)))

        await run("initial")
        await run("no_op")
        (repo / "module.py").write_text("def edited(): return 2\n")
        await run("edit")
        (repo / "notes.txt").unlink()
        await run("delete")
        git(repo, "mv", "module.py", "renamed.py")
        await run("rename")
        (repo / "renamed.py").write_text("def dirty(): return 3\n")
        await run("dirty")

        assert [sample["parsed_files"] for _, sample in samples] == [2, 0, 1, 0, 0, 1]
        assert samples[1][1] == {
            "parsed_files": 0,
            "embedded_windows": 0,
            "projected_units": 0,
            "capture_dirty": False,
        }
        assert [sample["capture_dirty"] for _, sample in samples] == [
            False,
            False,
            True,
            True,
            True,
            True,
        ]
        assert all(
            sample["embedded_windows"] > 0 and sample["projected_units"] > 0
            for name, sample in samples
            if name != "no_op"
        )
