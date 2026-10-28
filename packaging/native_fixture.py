"""Prepare a synthetic canonical native backup with the installed release wheel.

This prepares inputs for frozen-executable restore/rebuild tests; it is not
itself evidence that the executable can open or recover either native store.
No provider transport is created or called.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
from pathlib import Path

import tychora
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.adapters.ladybug.generation import LadybugGraphStore
from tychora.adapters.voyage.validation import qualify_voyage_capabilities
from tychora.application.backup import BackupService
from tychora.application.default_bindings import _VerifiedVoyageCounter
from tychora.application.owner import WorkspaceOwner
from tychora.application.sources import SourceService
from tychora.configuration.resolve import resolve_config
from tychora.contracts.embeddings import EmbeddingRecord
from tychora.contracts.identity import digest
from tychora.contracts.requests import RequestScope, SourceInput
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ports.execution import ExecutionContext

COLLECTION = "00000000-0000-4000-8000-000000000001"


class RecordedEmbedder:
    def __init__(self, counter):
        self.capabilities = qualify_voyage_capabilities(
            model="voyage-context-4", contextualization="joint", counter=counter
        )
        assert self.capabilities.dimensions == 1024

    def embed_groups(self, groups, ctx):
        values = struct.pack("<" + "f" * 1024, 1.0, *(0.0 for _ in range(1023)))
        return tuple(
            EmbeddingRecord.from_normalized_bytes(
                record_id=digest(
                    "embedding-record-v1",
                    window_id,
                    group.input_sha,
                    self.capabilities.space_id,
                ),
                input_sha=group.input_sha,
                space_id=self.capabilities.space_id,
                stored=values,
            )
            for group in groups
            for window_id in group.window_ids
        )

    def embed_queries(self, queries, ctx):
        raise AssertionError("Native fixture must not send a query to a provider")

    def probe_queries(self, queries, ctx):
        raise AssertionError("Native fixture must not probe a provider")


def _whole_workspace_scope(owner: WorkspaceOwner, scope: RequestScope) -> RequestScope:
    """Widen the fixture grant to every collection and every use the workspace defines."""
    uses = set(scope.uses)
    for definitions in (owner.store.registry.kinds, owner.store.registry.modes):
        for definition in definitions.values():
            uses.update(definition.get("allowed_uses", ()))
    for (raw,) in owner.db.execute(
        "SELECT r.allowed_uses_json FROM revisions r JOIN sources s USING(source_id) "
        "JOIN collections c USING(collection_id) WHERE c.workspace_id=?",
        (owner.workspace_id,),
    ):
        uses.update(json.loads(raw))
    for (definition,) in owner.db.execute("SELECT definition_json FROM kinds"):
        uses.update(json.loads(definition)["kind"].get("allowed_uses", ()))
    collections = {
        cid
        for (cid,) in owner.db.execute(
            "SELECT collection_id FROM collections WHERE workspace_id=?",
            (owner.workspace_id,),
        )
    }
    return scope.model_copy(
        update={"uses": frozenset(uses), "collection_ids": frozenset(collections)}
    )


async def create(root: Path, models: Path) -> None:
    package = Path(tychora.__file__).resolve()
    assert "site-packages" in package.parts, package
    root.mkdir(parents=True, exist_ok=True)
    settings = root / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[defaults]\nprofile="test"\n[configuration]\noffline=false\n'
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ndimensions=1024\n'
        '[configuration.index]\ngraph="ladybug"\n'
        '[configuration.index.text]\nbackend="chroma"\n'
    )
    config = resolve_config(settings, "test", {})
    scope = RequestScope(
        principal_id="synthetic-native-owner",
        collection_ids=frozenset({COLLECTION}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup", "compose"}),
        upload_providers=frozenset({"voyage"}),
    )
    data = root / "data"
    with WorkspaceOwner.open(data, config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                "INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) "
                "VALUES (?,?,?,?,0,?)",
                (
                    COLLECTION,
                    owner.workspace_id,
                    "default",
                    "Synthetic manuals",
                    '{"schema_version":1,"upload_providers":["voyage"]}',
                ),
            ),
            ExecutionContext(),
        )
        counter = _VerifiedVoyageCounter(models, "voyage-context-4")
        graphs: list[LadybugGraphStore] = []

        def graph_factory(build):
            if not graphs:
                graph = LadybugGraphStore(data / "indexes/graph")
                owner.register_close(graph.close)
                graphs.append(graph)
            return graphs[0].prepare_build(build)

        service = SourceService(
            owner,
            ProjectionCoordinator(
                store=owner.store,
                artifacts=owner.artifacts,
                config=config,
                counter=counter,
                text_embedder=RecordedEmbedder(counter),
                text_index=ChromaVectorIndex(data / "indexes/text"),
                graph_factory=graph_factory,
            ),
        )
        result = await service.add(
            SourceInput.from_mapping(
                {
                    "type": "text",
                    "text": "Synthetic amber verification evidence with a graph and vector record.",
                    "format": "text",
                    "kind": "manual@1",
                    "metadata": {
                        "title": "Synthetic native evidence",
                        "publisher": "Example Team",
                        "edition": "1",
                    },
                    "source_key": "native-manual.txt",
                    "collection_id": COLLECTION,
                }
            ),
            scope,
        )
        assert result.state == "succeeded", result
        snapshot = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        slots = await owner.run_write(
            lambda: sorted(
                {
                    family.slot
                    for family, _ in owner.store.iter_snapshot_batches(snapshot)
                }
            ),
            ExecutionContext(),
        )
        assert set(slots) >= {"text", "graph"}
        backup = root / "canonical-backup"
        admin = await owner.run_write(
            lambda: _whole_workspace_scope(owner, scope), ExecutionContext()
        )
        owner.scope = (
            admin  # Backup requires whole-workspace authority, never a partial grant.
        )
        await asyncio.to_thread(
            BackupService(owner).export, backup, admin, ctx=ExecutionContext()
        )
        print(
            f"PREPARED installed={package} snapshot={snapshot.snapshot_id} "
            f"slots={','.join(slots)} "
            f"archive={backup}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    model_path = os.environ.get("TYCHORA_TEST_MODEL_CACHE")
    if not model_path:
        raise ValueError("Verified external model cache is required")
    asyncio.run(create(args.root.resolve(), Path(model_path).resolve()))


if __name__ == "__main__":
    main()
