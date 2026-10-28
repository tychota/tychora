"""Synthetic real-engine probe; the cold owner lives in a separate child process."""

from __future__ import annotations

import asyncio
import json
import os
import select
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Hashable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Literal

from tests.indexing.test_publication import RecordingCounter, RecordingEmbedder
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.adapters.ladybug.generation import LadybugGraphStore
from tychora.adapters.sqlite.lexical import SqliteLexicalSearch
from tychora.adapters.voyage.query import VoyageTextQueryProvider
from tychora.adapters.voyage.request_types import RawVoyageResponse
from tychora.adapters.voyage.runtime import VoyageRuntime
from tychora.adapters.voyage.text import VoyageTextEmbedder
from tychora.application.owner import WorkspaceOwner
from tychora.application.query import QueryService
from tychora.application.query_plan import QueryBindings
from tychora.application.sources import SourceService
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.embeddings import EmbeddingCapabilities
from tychora.contracts.evidence import QueryResult
from tychora.contracts.identity import digest
from tychora.contracts.indexing import FamilySpec
from tychora.contracts.requests import QueryRequest, RequestScope, SourceInput
from tychora.evaluation.benchmarks import (
    BenchmarkConditions,
    BenchmarkIdentity,
    BenchmarkMemory,
    BenchmarkTelemetry,
    benchmark,
    process_memory,
)
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ports.execution import ExecutionContext
from tychora.ports.search import RankedOwners
from tychora.retrieval.attempt import EligibilityAttempt
from tychora.retrieval.cache import QueryCaches

COLLECTION = "55825936-6a6a-4d29-945a-327d771a76b7"


class Counter:
    model = "synthetic-local-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text.split())


def _scope() -> RequestScope:
    return RequestScope(
        principal_id="benchmark-reader",
        collection_ids=frozenset({COLLECTION}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup", "compose"}),
        upload_providers=frozenset({"voyage"}),
    )


def _config(root: Path):
    return resolve_config(root / "workspace.toml", "test", {})


def setup_corpus(
    root: Path,
    legacy_order: tuple[str, str] | None = None,
    *,
    graph: bool = False,
    graph_vector: bool = False,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "workspace.toml").write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        + ("[configuration]\noffline=false\n" if not graph or graph_vector else "")
        + (
            '[configuration.providers.text]\nprovider="voyage"\nmodel="voyage-context-4"\ndimensions=2\n'
            if not graph or graph_vector
            else ""
        )
        + ('[configuration.index]\ngraph="ladybug"\n' if graph else "")
        + (
            '[configuration.index.text]\nbackend="chroma"\n'
            if not graph or graph_vector
            else ""
        )
    )
    config = _config(root)
    scope = _scope()

    async def create() -> None:
        with WorkspaceOwner.open(root / "data", config, scope) as owner:
            await owner.run_write(
                lambda: owner.db.execute(
                    "INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,?)",
                    (
                        COLLECTION,
                        owner.workspace_id,
                        "manuals",
                        "Manuals",
                        '{"schema_version":1,"upload_providers":["voyage"]}',
                    ),
                ),
                ExecutionContext(),
            )
            counter = RecordingCounter()
            sources = SourceService(
                owner,
                ProjectionCoordinator(
                    store=owner.store,
                    artifacts=owner.artifacts,
                    config=config,
                    counter=counter,
                    text_embedder=RecordingEmbedder(counter),
                    text_index=ChromaVectorIndex(root / "vectors"),
                    graph_factory=graph_factory(owner, root) if graph else None,
                ),
            )
            await sources.add(
                SourceInput.from_mapping(
                    {
                        "type": "text",
                        "text": "Synthetic verification evidence for the benchmark.",
                        "format": "text",
                        "kind": "manual@1",
                        "metadata": {
                            "title": "Synthetic manual",
                            "publisher": "Example Team",
                            "edition": "1",
                        },
                        "source_key": "benchmark.txt",
                        "collection_id": COLLECTION,
                    }
                ),
                scope,
            )

    if legacy_order is not None:
        from tychora.indexing import coordinator

        stable = coordinator.stable_capability_json

        def historical(capabilities: EmbeddingCapabilities):
            return {**stable(capabilities), "input_types": list(legacy_order)}

        vars(coordinator)["stable_capability_json"] = historical
    asyncio.run(create())


def graph_factory(owner: WorkspaceOwner, root: Path):
    def create(build):
        graph = LadybugGraphStore(
            root / "graphs" / build.spec.generation_id, build=build
        )
        owner.register_close(graph.close)
        return graph

    return create


class EngineSession:
    def __init__(self, root: Path, *, process_isolated: bool = False):
        self._owns_trace = not tracemalloc.is_tracing()
        if self._owns_trace:
            tracemalloc.start()
        self.root = root
        self.owner_context = WorkspaceOwner.open(root / "data", _config(root), _scope())
        self.owner = self.owner_context.__enter__()
        self.scope = _scope()
        self.caches = QueryCaches()
        self.counts = BenchmarkTelemetry()
        self.index = ChromaVectorIndex(root / "vectors")
        snapshot = self.owner.run_write_sync(
            lambda: self.owner.store.resolve_snapshot(None), ExecutionContext()
        )
        slot = snapshot.family("text")
        assert slot is not None
        spec_json = slot.family.config_json["spec"]
        capabilities_json = slot.family.config_json["capabilities"]
        origin_sha = slot.family.config_json["effective_config_sha"]
        assert isinstance(spec_json, FrozenJsonMap)
        assert isinstance(capabilities_json, FrozenJsonMap)
        assert isinstance(origin_sha, str)
        retained_spec = FamilySpec.model_validate(dict(spec_json))
        self.index.create_family(retained_spec)
        coordinator = ProjectionCoordinator(
            store=self.owner.store,
            artifacts=self.owner.artifacts,
            config=self.owner.config,
            counter=RecordingCounter(),
            text_embedder=RecordingEmbedder(RecordingCounter()),
            text_index=self.index,
        )
        self.reuse_matches = self.owner.run_write_sync(
            lambda: coordinator._retained_vector_spec(slot, "text") == retained_spec,
            ExecutionContext(),
        )
        self.token_counter = RecordingCounter()
        self.transport = self._Transport(self)
        runtime = VoyageRuntime(
            lambda: self.transport,
            workspace_id=self.owner.workspace_id,
            credential_binding_id="synthetic-credential",
            query_caches=self.caches,
        )
        embedder = VoyageTextEmbedder(
            capabilities=RecordingEmbedder(self.token_counter).capabilities,
            counter=self.token_counter,
            runtime=runtime,
            authorize_document=lambda payload_sha, dependencies, ctx: None,
        )
        input_types = capabilities_json["input_types"]
        assert isinstance(input_types, tuple)
        self.capability_order = (
            input_types,
            tuple(embedder.capabilities.model_dump(mode="json")["input_types"]),
        )
        settings = self.owner.config.values.providers.text.model_dump(mode="json")
        self.family_proof = {
            "retained_matches": digest(
                "text-family-config-v1",
                origin_sha,
                settings,
                capabilities_json,
            )
            == spec_json["config_sha"],
            "fresh_matches": digest(
                "text-family-config-v1",
                origin_sha,
                settings,
                embedder.capabilities.model_dump(mode="json"),
            )
            == spec_json["config_sha"],
        }
        self.service = QueryService(
            self.owner,
            output_counter=Counter(),
            caches=self.caches,
            bindings=QueryBindings(
                text_provider=VoyageTextQueryProvider(embedder), text_index=self.index
            ),
        )
        self._instrument()
        self.identity = BenchmarkIdentity(
            corpus_sha=snapshot.snapshot_id,
            config_sha=self.owner.config.config_sha,
            output_cap=self.owner.config.values.limits.max_tokens,
        )
        self.conditions = BenchmarkConditions(
            process_id=os.getpid(),
            process_start_id=subprocess.check_output(
                ["ps", "-o", "lstart=", "-p", str(os.getpid())], text=True
            ).strip(),
            owner_session_id=str(id(self.owner)),
            process_isolated=process_isolated,
            os_page_cache="uncontrolled",
            provider_cache="synthetic local transport; no external cache",
        )

    class _Transport:
        def __init__(self, session):
            self.session = session

        def post_json(self, path, body, timeout):
            self.session._bump(provider_calls=1, query_embedding_calls=1)
            return RawVoyageResponse(
                200,
                b'{"data":[{"index":0,"data":[{"index":0,"embedding":[1.0,0.0]}]}]}',
            )

        def close(self):
            pass

    def _bump(self, **increments):
        self.counts = replace(
            self.counts,
            **{
                name: getattr(self.counts, name) + value
                for name, value in increments.items()
            },
        )

    def _time(self, stage, call, *args, **kwargs):
        began = time.monotonic()
        try:
            return call(*args, **kwargs)
        finally:
            stages = dict(self.counts.stages_ms)
            stages[stage] = stages.get(stage, 0.0) + (time.monotonic() - began) * 1000
            self.counts = replace(self.counts, stages_ms=stages)

    def _instrument(self) -> None:
        lexical_search = SqliteLexicalSearch.search
        vector_search = self.index.search
        revalidate = EligibilityAttempt.revalidate
        cached_result = self.caches.get_result

        def measured_lexical(
            search: SqliteLexicalSearch,
            view: EligibilityView,
            query: str,
            limit: int,
            ctx: ExecutionContext,
            *,
            stopwords: frozenset[str] = frozenset(),
        ) -> RankedOwners:
            self._bump(retrieval_calls=1)
            return self._time(
                "lexical_search",
                lexical_search,
                search,
                view,
                query,
                limit,
                ctx,
                stopwords=stopwords,
            )

        def measured_vector(
            view: EligibilityView,
            vector: bytes,
            limit: int,
            ctx: ExecutionContext,
        ) -> RankedOwners:
            self._bump(retrieval_calls=1)
            return self._time("vector_search", vector_search, view, vector, limit, ctx)

        async def measured_revalidate(
            attempt: EligibilityAttempt, stage: Literal["provider", "delivery"]
        ) -> None:
            if attempt.owner is not self.owner:
                return await revalidate(attempt, stage)
            self._bump(policy_revalidations=1)
            began = time.monotonic()
            try:
                await revalidate(attempt, stage)
            finally:
                stages = dict(self.counts.stages_ms)
                elapsed = (time.monotonic() - began) * 1000
                stages["policy_revalidation"] = (
                    stages.get("policy_revalidation", 0.0) + elapsed
                )
                self.counts = replace(self.counts, stages_ms=stages)

        def measured_result(key: Hashable) -> bytes | None:
            value = cached_result(key)
            if value is not None:
                self._bump(cache_hits=1)
            return value

        vars(self.index)["search"] = measured_vector
        # Attempts and read transactions are created per query, so the class
        # methods are wrapped until close.
        self._lexical_search = lexical_search
        SqliteLexicalSearch.search = measured_lexical  # ty: ignore[invalid-assignment]
        self._revalidate = revalidate
        EligibilityAttempt.revalidate = measured_revalidate  # ty: ignore[invalid-assignment]
        vars(self.caches)["get_result"] = measured_result

    def prepare(self, state: str) -> None:
        if tracemalloc.is_tracing():
            tracemalloc.reset_peak()
        if state == "warm_resources":
            self.caches.purge()
        elif state == "warm_query_embedding":
            self.caches.purge_results()

    def counters(self) -> BenchmarkTelemetry:
        index_bytes = sum(
            path.stat().st_size
            for path in (self.root / "vectors").rglob("*")
            if path.is_file()
        )
        return replace(self.counts, index_bytes=index_bytes)

    def search(self, request: QueryRequest) -> QueryResult:
        delivered = asyncio.run(
            self.service.search(request, self.scope, ExecutionContext())
        )
        self._bump(
            selected_units=sum(
                len(s.matched_unit_ids) for s in delivered.result.segments
            ),
            output_tokens=delivered.token_count,
        )
        return delivered.result

    def memory(self) -> BenchmarkMemory:
        return process_memory()

    def close(self) -> None:
        EligibilityAttempt.revalidate = self._revalidate
        SqliteLexicalSearch.search = self._lexical_search
        self.owner_context.__exit__(None, None, None)
        if self._owns_trace:
            tracemalloc.stop()


class ColdProcessSession:
    def __init__(self, root: Path, child_seed: str):
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.performance.engine_probe",
                "child",
                str(root),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
                "PYTHONHASHSEED": child_seed,
            },
        )
        hello = self._read()
        assert hello["capability_order"][0] != hello["capability_order"][1]
        assert hello["family_proof"] == {
            "retained_matches": True,
            "fresh_matches": False,
        }
        assert hello["reuse_matches"] is True
        self.identity = BenchmarkIdentity(**hello["identity"])
        self.conditions = BenchmarkConditions(**hello["conditions"])
        assert self.conditions.process_id == self.process.pid
        self.counts = BenchmarkTelemetry()
        self._memory = BenchmarkMemory(self.conditions.process_id, 0, 0)

    def _read(self):
        assert self.process.stdout is not None
        ready, _, _ = select.select([self.process.stdout], [], [], 30)
        if not ready:
            self.process.kill()
            raise AssertionError("Synthetic benchmark child timed out")
        line = self.process.stdout.readline()
        if not line:
            assert self.process.stderr is not None
            detail = (
                self.process.stderr.read() if self.process.poll() is not None else ""
            )
            raise AssertionError(
                f"Synthetic benchmark child exited before reply: {detail}"
            )
        return json.loads(line)

    def prepare(self, state: str) -> None:
        assert state == "cold_owner"

    def counters(self) -> BenchmarkTelemetry:
        return self.counts

    def search(self, request: QueryRequest) -> object:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(request.model_dump(mode="json")) + "\n")
        self.process.stdin.flush()
        reply = self._read()
        self.counts = BenchmarkTelemetry(**reply["telemetry"])
        self._memory = BenchmarkMemory(**reply["memory"])
        return reply["result"]

    def memory(self) -> BenchmarkMemory:
        return self._memory

    def close(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        assert self.process.returncode == 0


def run_real_benchmark(
    root: Path,
    legacy_order: tuple[str, str] = ("query", "document"),
    child_seed: str = "2",
):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.performance.engine_probe",
            "setup",
            str(root),
            *legacy_order,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "PYTHONHASHSEED": "1",
        },
    )
    assert completed.returncode == 0, completed.stderr
    cold: ColdProcessSession | None = None
    warm: EngineSession | None = None

    def factory(state: str):
        nonlocal cold, warm
        if state == "cold_owner":
            cold = ColdProcessSession(root, child_seed)
            return cold
        if warm is None:
            warm = EngineSession(root)
        return warm

    try:
        return benchmark(
            (QueryRequest.from_mapping({"question": "verification"}),),
            (
                "cold_owner",
                "warm_resources",
                "warm_query_embedding",
                "warm_result",
            ),
            factory,
            time,
        )
    finally:
        if cold is not None:
            cold.close()
        if warm is not None:
            warm.close()


def _child(root: Path) -> None:
    session = EngineSession(root, process_isolated=True)
    try:
        print(
            json.dumps(
                {
                    "identity": asdict(session.identity),
                    "conditions": asdict(session.conditions),
                    "capability_order": session.capability_order,
                    "family_proof": session.family_proof,
                    "reuse_matches": session.reuse_matches,
                }
            ),
            flush=True,
        )
        request = json.loads(sys.stdin.readline())
        result = session.search(QueryRequest.from_mapping(request))
        print(
            json.dumps(
                {
                    "result": result.model_dump(mode="json"),
                    "telemetry": asdict(session.counters()),
                    "memory": asdict(session.memory()),
                }
            ),
            flush=True,
        )
    finally:
        session.close()


if __name__ == "__main__":
    if sys.argv[1] == "setup-graph-vector":
        setup_corpus(Path(sys.argv[2]), graph=True, graph_vector=True)
    elif sys.argv[1] == "setup":
        setup_corpus(Path(sys.argv[2]), (sys.argv[3], sys.argv[4]))
    else:
        _child(Path(sys.argv[2]))
