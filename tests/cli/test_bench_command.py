"""The benchmark entry point runs a small offline suite end to end."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from tychora.contracts.operations import JobEvent
from tychora.interfaces.bench import TychoraBenchClient, main


class FakeJobs:
    """Job operations of a workspace whose jobs have many events."""

    def __init__(self, total: int, states: tuple[str, ...] = ("succeeded",)) -> None:
        self.total = total
        self.states = iter(states)
        self.timeouts: list[float] = []
        self.pages: list[int] = []

    def events_after(self, job_id: str, *, after: int = 0):
        self.pages.append(after)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        return tuple(
            JobEvent(
                job_id=job_id,
                sequence=sequence,
                timestamp=start + timedelta(seconds=sequence),
                stage="parse",
                state="running",
            )
            for sequence in range(after + 1, min(after + 100, self.total) + 1)
        )

    def wait(self, job_id: str, *, timeout: float):
        self.timeouts.append(timeout)
        return SimpleNamespace(state=next(self.states))


def _client(jobs: FakeJobs, tmp_path) -> TychoraBenchClient:
    return TychoraBenchClient(SimpleNamespace(jobs=jobs), tmp_path, ("lexical",))


def test_events_pages_through_every_event(tmp_path):
    jobs = FakeJobs(250)
    events = _client(jobs, tmp_path).events("job")
    assert [event.sequence for event in events] == list(range(1, 251))
    assert jobs.pages == [0, 100, 200]


def test_events_stop_on_an_exact_page_boundary(tmp_path):
    jobs = FakeJobs(200)
    assert len(_client(jobs, tmp_path).events("job")) == 200


def test_waits_are_bounded_to_the_job_wait_cap(tmp_path):
    jobs = FakeJobs(0, states=("running", "running", "succeeded"))
    assert _client(jobs, tmp_path).wait("job") == "succeeded"
    assert jobs.timeouts
    assert max(jobs.timeouts) <= 300.0


def test_footprint_counts_workspace_files(tmp_path):
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "big").write_bytes(b"x" * 50)
    (tmp_path / "small").write_bytes(b"x" * 10)
    footprint = _client(FakeJobs(0), tmp_path).footprint()
    assert (footprint.total_bytes, footprint.largest_artifact_bytes) == (60, 50)


def test_offline_query_suite_writes_a_result(tmp_path):
    code = main(
        [
            "run",
            "--corpus",
            "bench-small",
            "--suite",
            "query",
            "--profile",
            "offline",
            "--limit",
            "5",
            "--out",
            str(tmp_path / "results"),
            "--cache",
            str(tmp_path / "cache"),
        ]
    )
    assert code == 0
    [path] = list((tmp_path / "results").rglob("query-offline.json"))
    document = json.loads(path.read_text())
    assert document["schema"] == "tychora.bench-result"
    assert document["stages"]["total"]["n"] >= 1
    assert document["corpus"]["name"] == "bench-small"
    assert {"cpu", "cores", "ram_bytes", "os", "python", "sqlite", "chroma"} <= set(
        document["machine"]
    )


def test_families_suite_exits_with_usage_code(tmp_path, capsys):
    code = main(
        [
            "run",
            "--corpus",
            "bench-small",
            "--suite",
            "families",
            "--profile",
            "offline",
            "--cache",
            str(tmp_path),
        ]
    )
    assert code == 2
    assert "families suite" in capsys.readouterr().err


def test_incomparable_results_are_reported_cleanly(tmp_path, capsys):
    arguments = [
        "run",
        "--corpus",
        "bench-small",
        "--suite",
        "ingest",
        "--profile",
        "offline",
        "--limit",
        "2",
        "--out",
        str(tmp_path / "results"),
        "--cache",
        str(tmp_path / "cache"),
    ]
    assert main(arguments) == 0
    [first] = (tmp_path / "results").rglob("ingest-offline.json")
    document = json.loads(first.read_text())
    document["reference_machine"] = not document["reference_machine"]
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps(document))
    capsys.readouterr()
    assert (
        main(
            [
                *arguments[:-4],
                "--out",
                str(tmp_path / "again"),
                "--cache",
                str(tmp_path / "cache"),
                "--compare",
                str(previous),
            ]
        )
        == 2
    )
    assert "bench.incomparable" in capsys.readouterr().err
