"""Synthetic source-publication victim for parent-owned SIGKILL tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

from tests.acceptance.faults import FaultInjector
from tests.indexing.test_publication import RecordingCounter, RecordingEmbedder
from tests.performance.engine_probe import COLLECTION, _config, _scope, graph_factory
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.adapters.ladybug.generation import LadybugGraphStore
from tychora.application.owner import WorkspaceOwner
from tychora.application.sources import SourceService
from tychora.configuration.registry import Registry
from tychora.contracts.corpus import ExtractionBundle
from tychora.contracts.extraction import ExtractionArtifact
from tychora.contracts.graph import GraphBatchReceipt, GraphManifest
from tychora.contracts.publication import PublicationPlan
from tychora.contracts.requests import SourceInput
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ports.execution import ExecutionContext


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

                original_normalize = sources.normalize

                def paused_normalize(
                    parsed: ExtractionArtifact, revision_id: str, registry: Registry
                ) -> ExtractionBundle:
                    injector.hit(point)
                    return original_normalize(parsed, revision_id, registry)

                patch.object(sources, "normalize", paused_normalize).start()
            elif point == "artifact_before_sql":
                original_commit = owner.store.commit_extraction

                def paused_commit(bundle: ExtractionBundle) -> str:
                    injector.hit(point)
                    return original_commit(bundle)

                patch.object(owner.store, "commit_extraction", paused_commit).start()
            elif point == "publish_before_event":
                original_publish = owner.store.publish_snapshot

                def paused_publish(plan: PublicationPlan) -> str:
                    published = original_publish(plan)
                    injector.hit(point)
                    return published

                patch.object(owner.store, "publish_snapshot", paused_publish).start()
            elif point == "graph_seal_before_publish":
                original_seal = LadybugGraphStore.seal_generation

                def paused_seal(
                    graph: LadybugGraphStore,
                    generation_id: str,
                    expected: GraphManifest,
                    ctx: ExecutionContext,
                ) -> GraphBatchReceipt:
                    receipt = original_seal(graph, generation_id, expected, ctx)
                    injector.hit(point)
                    return receipt

                patch.object(LadybugGraphStore, "seal_generation", paused_seal).start()
            else:
                raise ValueError(point)
            await service(owner, root).add(source(), _scope(), "crash-retry-key")

    asyncio.run(add())
    raise AssertionError("The expected source boundary was not reached")


if __name__ == "__main__":
    run(Path(sys.argv[1]), sys.argv[2])
