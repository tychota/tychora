"""Synthetic source-publication victim for parent-owned SIGKILL tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from tests.acceptance.faults import FaultInjector
from tests.indexing.test_publication import RecordingCounter, RecordingEmbedder
from tests.performance.engine_probe import COLLECTION, _config, _scope, graph_factory
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.adapters.ladybug.generation import LadybugGraphStore
from tychora.application.owner import WorkspaceOwner
from tychora.application.sources import SourceService
from tychora.contracts.requests import SourceInput
from tychora.indexing.coordinator import ProjectionCoordinator


def source() -> SourceInput:
    return SourceInput.from_mapping(
        {
            "type": "text",
            "text": "Additional synthetic evidence after a process interruption.",
            "format": "text",
            "kind": "manual@1",
            "metadata": {
                "title": "Additional manual",
                "publisher": "Example Team",
                "edition": "1",
            },
            "source_key": "additional.txt",
            "collection_id": COLLECTION,
        }
    )


def service(owner: WorkspaceOwner, root: Path) -> SourceService:
    counter = RecordingCounter()
    return SourceService(
        owner,
        ProjectionCoordinator(
            store=owner.store,
            artifacts=owner.artifacts,
            config=owner.config,
            counter=counter,
            text_embedder=RecordingEmbedder(counter),
            text_index=ChromaVectorIndex(root / "vectors"),
            graph_factory=(
                graph_factory(owner, root)
                if owner.config.values.index.graph == "ladybug"
                else None
            ),
        ),
    )


def run(root: Path, point: str) -> None:
    injector = FaultInjector(root, point)

    async def add() -> None:
        with WorkspaceOwner.open(root / "data", _config(root), _scope()) as owner:
            if point == "mid_extraction":
                from tychora.application import sources

                normalize = sources.normalize

                def paused(*args, **kwargs):
                    injector.hit(point)
                    return normalize(*args, **kwargs)

                sources.normalize = paused
            elif point == "artifact_before_sql":
                original = owner.store.commit_extraction

                def paused(bundle):
                    injector.hit(point)
                    return original(bundle)

                owner.store.commit_extraction = paused
            elif point == "publish_before_event":
                original = owner.store.publish_snapshot

                def paused(plan):
                    published = original(plan)
                    injector.hit(point)
                    return published

                owner.store.publish_snapshot = paused
            elif point == "graph_seal_before_publish":
                original = LadybugGraphStore.seal_generation

                def paused(self, *args, **kwargs):
                    receipt = original(self, *args, **kwargs)
                    injector.hit(point)
                    return receipt

                LadybugGraphStore.seal_generation = paused
            else:
                raise ValueError(point)
            await service(owner, root).add(source(), _scope(), "crash-retry-key")

    asyncio.run(add())
    raise AssertionError("The expected source boundary was not reached")


if __name__ == "__main__":
    run(Path(sys.argv[1]), sys.argv[2])
