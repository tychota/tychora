"""Four distinct query-cache benchmark states and report accounting."""

import os
import subprocess
import sys
from dataclasses import replace
from os import getpid

import pytest

from tests.performance.engine_probe import run_real_benchmark
from tychora.contracts.requests import QueryRequest
from tychora.evaluation.benchmarks import (
    BenchmarkConditions,
    BenchmarkIdentity,
    BenchmarkMemory,
    BenchmarkTelemetry,
    benchmark,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        self.now += 0.001
        return self.now


class SyntheticInstrumentedSession:
    def __init__(self, state, warm):
        self.state = state
        self.warm = warm
        self.identity = BenchmarkIdentity(
            corpus_sha="corpus", config_sha="config", output_cap=1000
        )
        self.conditions = BenchmarkConditions(
            process_id=getpid() + 1 if state == "cold_owner" else getpid(),
            process_start_id="child" if state == "cold_owner" else "parent",
            owner_session_id="child" if state == "cold_owner" else str(id(warm)),
            process_isolated=state == "cold_owner",
            os_page_cache="uncontrolled",
            provider_cache="synthetic local",
        )
        self.counts = BenchmarkTelemetry()

    def prepare(self, state):
        assert state == self.state

    def counters(self):
        return self.counts

    def memory(self):
        return BenchmarkMemory(self.conditions.process_id, 0, 0)

    def search(self, request):
        assert request.input.text == "synthetic"
        if self.state == "warm_result":
            self.counts = replace(
                self.counts,
                policy_revalidations=self.counts.policy_revalidations + 1,
                cache_hits=self.counts.cache_hits + 1,
            )
        else:
            self.counts = replace(
                self.counts,
                retrieval_calls=self.counts.retrieval_calls + 1,
                query_embedding_calls=self.counts.query_embedding_calls
                + (self.state != "warm_query_embedding"),
            )
        return object()


@pytest.fixture
def benchmark_case():
    warm = object()

    class Case:
        def run_states(self):
            return benchmark(
                (QueryRequest.from_mapping({"question": "synthetic"}),),
                (
                    "cold_owner",
                    "warm_resources",
                    "warm_query_embedding",
                    "warm_result",
                ),
                lambda state: SyntheticInstrumentedSession(state, warm),
                Clock(),
            )

    return Case()


def test_warm_result_is_not_reported_as_uncached_search(benchmark_case):
    result = benchmark_case.run_states()
    assert result["warm_query_embedding"].query_embedding_calls == 0
    assert result["warm_query_embedding"].retrieval_calls > 0
    assert result["warm_result"].retrieval_calls == 0
    assert result["warm_result"].provider_calls == 0
    assert result["warm_result"].policy_revalidations > 0
    assert result["cold_owner"].conditions.process_id != getpid()


def test_benchmark_rejects_in_process_cold_owner():
    request = QueryRequest.from_mapping({"question": "synthetic"})

    def in_process(state):
        session = SyntheticInstrumentedSession(state, object())
        session.conditions = BenchmarkConditions(
            process_id=getpid(),
            process_start_id="same",
            process_isolated=False,
            os_page_cache="uncontrolled",
            provider_cache="synthetic local",
        )
        return session

    with pytest.raises(ValueError, match="process"):
        benchmark(
            (request,),
            (
                "cold_owner",
                "warm_resources",
                "warm_query_embedding",
                "warm_result",
            ),
            in_process,
            Clock(),
        )


@pytest.mark.parametrize(
    "legacy_order,child_seed",
    [(("query", "document"), "2"), (("document", "query"), "1")],
)
def test_real_four_states_use_subprocess_and_engine_counters(
    tmp_path, legacy_order, child_seed
):
    report = run_real_benchmark(tmp_path, legacy_order, child_seed)
    assert report["cold_owner"].conditions.process_id != getpid()
    assert (
        report["cold_owner"].memory_process_id
        == report["cold_owner"].conditions.process_id
    )
    assert report["cold_owner"].traced_python_peak_bytes > 0
    assert report["warm_resources"].query_embedding_calls == 1
    assert report["warm_query_embedding"].query_embedding_calls == 0
    assert report["warm_query_embedding"].retrieval_calls > 0
    assert report["warm_result"].retrieval_calls == 0
    assert report["warm_result"].provider_calls == 0
    assert report["warm_result"].policy_revalidations > 0


def test_new_family_capability_projection_is_hash_seed_independent():
    code = (
        "from tests.indexing.test_publication import RecordingCounter,RecordingEmbedder\n"
        "from tychora.contracts.embeddings import stable_capability_json\n"
        "from tychora.contracts.identity import digest\n"
        "print(digest('new-family-proof',stable_capability_json(RecordingEmbedder(RecordingCounter()).capabilities)))\n"
    )
    values = []
    for seed in ("1", "2"):
        completed = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": seed},
        )
        values.append(completed.stdout.strip())
    assert len(set(values)) == 1
