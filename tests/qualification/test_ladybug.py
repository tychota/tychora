"""Installed Ladybug graph schema qualification."""

import selectors
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from ladybug import Connection

from tests.helpers.ladybug import query_rows
from tests.helpers.timing import scaled
from tychora.adapters.ladybug.generation import LadybugGraphStore

pytestmark = pytest.mark.qualification


def test_graph_creates_seven_native_tables(tmp_path):
    graph = LadybugGraphStore(tmp_path / "graph")
    rows = query_rows(graph._connection, "CALL SHOW_TABLES() RETURN name ORDER BY name")
    assert {row[0] for row in rows} == {
        "Entity",
        "Evidence",
        "RelationAssertion",
        "GroundedIn",
        "Subject",
        "Object",
        "SupportedBy",
    }


def test_native_reader_does_not_see_uncommitted_generation_node(tmp_path):
    graph = LadybugGraphStore(tmp_path / "graph")
    reader = Connection(graph._database)
    graph._connection.execute("BEGIN TRANSACTION")
    try:
        graph._connection.execute(
            "CREATE (:Evidence {pk:'pending',generation_id:'g',evidence_id:'u'})"
        )
        assert query_rows(reader, "MATCH (e:Evidence) RETURN count(e)") == [[0]]
    finally:
        graph._connection.execute("ROLLBACK")
        reader.close()
        graph.close()


def test_adapter_reopens_after_forced_uncommitted_writer_termination(tmp_path):
    path = tmp_path / "graph"
    graph = LadybugGraphStore(path)
    graph.close()
    script = """import signal,sys
from tychora.adapters.ladybug.generation import LadybugGraphStore
graph=LadybugGraphStore(sys.argv[1])
graph._connection.execute("BEGIN TRANSACTION")
graph._connection.execute("CREATE (:Evidence {pk:'pending',generation_id:'g',evidence_id:'u'})")
print("transaction-open",flush=True)
signal.pause()
"""
    writer = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert writer.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(writer.stdout, selectors.EVENT_READ)
    if not selector.select(timeout=scaled(10)):
        writer.kill()
        _, stderr = writer.communicate(timeout=scaled(10))
        pytest.fail(
            f"Ladybug graph writer did not reach transaction boundary: {stderr}"
        )
    assert writer.stdout.readline().strip() == "transaction-open"
    writer.kill()
    writer.communicate(timeout=scaled(10))
    assert writer.returncode is not None and writer.returncode < 0
    reopened = LadybugGraphStore(path)
    assert query_rows(reopened._connection, "MATCH (e:Evidence) RETURN count(e)") == [
        [0]
    ]
    reopened.close()


def test_native_database_serves_concurrent_read_connections(tmp_path):
    graph = LadybugGraphStore(tmp_path / "graph")
    graph._connection.execute(
        "CREATE (:Evidence {pk:'persisted',generation_id:'g',evidence_id:'u'})"
    )

    def read_count():
        reader = Connection(graph._database)
        try:
            return query_rows(reader, "MATCH (e:Evidence) RETURN count(e)")
        finally:
            reader.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: read_count(), range(8))) == [[[1]]] * 8
    graph.close()
