"""Synthetic native graph seal victim for parent-owned SIGKILL tests."""

from __future__ import annotations

import sys
from pathlib import Path

from tests.acceptance.faults import FaultInjector
from tests.storage.test_graph_generation import _build
from tychora.adapters.ladybug.generation import LadybugGraphStore
from tychora.contracts.graph import GraphManifest
from tychora.ports.execution import ExecutionContext


def fixture(root: Path, stable: bool):
    spec, build, units, assertion = _build(root)
    if stable:
        spec = spec.model_copy(update={"projection_version": "stable-v2"})
        build = build.model_copy(update={"spec": spec})
    return spec, build, units, assertion


def run(root: Path, point: str) -> None:
    injector = FaultInjector(root, point)
    stable = not point.startswith("legacy-")
    spec, build, units, assertion = fixture(root, stable)
    graph = LadybugGraphStore(root / "graph", build=build)
    generation = graph.begin_generation(spec)
    try:
        if not point.endswith("verify"):
            graph.upsert_batch(generation, (assertion,), units, ExecutionContext())
        receipt = graph.seal_generation(
            generation, GraphManifest.from_build(build), ExecutionContext()
        )
        assert receipt.assertion_count == 1
        if point.endswith("verify"):
            print(receipt.manifest_digest, flush=True)
            return
        if point.endswith("create"):
            return
        injector.hit(point)
        raise AssertionError("The expected graph-seal boundary was not reached")
    finally:
        graph.close()


if __name__ == "__main__":
    run(Path(sys.argv[1]), sys.argv[2])
