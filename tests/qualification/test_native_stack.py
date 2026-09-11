"""Qualification probes for native persistence dependencies."""

import json
import platform
import selectors
import sqlite3
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

pytestmark = pytest.mark.qualification


def test_chroma_search_honors_explicit_ids(tmp_path):
    import chromadb

    client = chromadb.PersistentClient(path=str(tmp_path / "vectors"))
    col = client.create_collection("eligibility_probe", embedding_function=None)
    col.add(ids=["hidden", "allowed"], embeddings=[[1.0, 0.0], [0.8, 0.6]])
    result = col.query(
        query_embeddings=[[1.0, 0.0]],
        ids=["allowed"],
        n_results=1,
        include=["distances"],
    )
    assert result["ids"] == [["allowed"]]


def test_chroma_persists_and_reopens_without_embedding_provider(tmp_path):
    import chromadb

    path = tmp_path / "vectors"
    client = chromadb.PersistentClient(path=str(path))
    collection = client.create_collection("reopen_probe", embedding_function=None)
    collection.add(ids=["persisted"], embeddings=[[0.0, 1.0]])
    del collection, client

    reopened = chromadb.PersistentClient(path=str(path)).get_collection(
        "reopen_probe", embedding_function=None
    )
    assert reopened.get(ids=["persisted"], include=[])["ids"] == ["persisted"]


def test_sqlite_has_required_fts5_and_json_features(tmp_path):
    connection = sqlite3.connect(tmp_path / "qualification.sqlite3")
    try:
        connection.execute("CREATE VIRTUAL TABLE documents USING fts5(body)")
        connection.execute("INSERT INTO documents(body) VALUES ('public evidence')")
        fts_match = connection.execute(
            "SELECT body FROM documents WHERE documents MATCH 'evidence'"
        ).fetchone()
        json_value = connection.execute(
            "SELECT json_extract('{\"enabled\": true}', '$.enabled')"
        ).fetchone()
    finally:
        connection.close()

    assert sqlite3.sqlite_version_info >= (3, 38, 0)
    assert fts_match == ("public evidence",)
    assert json_value == (1,)


def test_native_platform_is_a_qualification_target():
    assert (platform.system(), platform.machine()) in {
        ("Darwin", "arm64"),
        ("Linux", "x86_64"),
    }


def _wheel_tags(distribution_name: str) -> set[str]:
    wheel_metadata = metadata.distribution(distribution_name).read_text("WHEEL")
    assert wheel_metadata is not None
    return {
        line.removeprefix("Tag: ")
        for line in wheel_metadata.splitlines()
        if line.startswith("Tag: ")
    }


def test_selected_native_versions_and_wheel_tags_match_target():
    assert metadata.version("chromadb") == "1.5.9"
    assert metadata.version("ladybug") == "0.20.3"

    expected_tags = {
        ("Darwin", "arm64"): {
            "chromadb": "cp39-abi3-macosx_11_0_arm64",
            "ladybug": "cp312-cp312-macosx_15_0_arm64",
        },
        ("Linux", "x86_64"): {
            "chromadb": "cp39-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64",
            "ladybug": "cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64",
        },
    }
    platform_tags = expected_tags[(platform.system(), platform.machine())]
    for distribution_name, expected_tag in platform_tags.items():
        assert expected_tag in _wheel_tags(distribution_name)


def _run_graph_probe(
    mode: str, database_path: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("graph_probe.py")),
            mode,
            str(database_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_ladybug_commits_rolls_back_reopens_and_shares_database(tmp_path):
    completed = _run_graph_probe("persist", tmp_path / "graph")
    assert json.loads(completed.stdout) == {
        "connection_one": ["kept"],
        "connection_two": ["kept"],
        "reopened": ["kept"],
    }


def test_ladybug_recovers_after_writer_is_forced_to_terminate(tmp_path):
    database_path = tmp_path / "graph"
    _run_graph_probe("persist", database_path)
    writer = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("graph_probe.py")),
            "uncommitted-writer",
            str(database_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert writer.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(writer.stdout, selectors.EVENT_READ)
    if not selector.select(timeout=10):
        writer.kill()
        _, stderr = writer.communicate(timeout=10)
        pytest.fail(f"Ladybug writer did not reach transaction boundary: {stderr}")
    boundary = writer.stdout.readline().strip()
    if boundary != "transaction-open":
        writer.kill()
        _, stderr = writer.communicate(timeout=10)
        pytest.fail(f"unexpected Ladybug writer boundary {boundary!r}: {stderr}")
    writer.kill()
    _, stderr = writer.communicate(timeout=10)
    assert writer.returncode is not None and writer.returncode < 0, stderr

    completed = _run_graph_probe("verify", database_path)
    assert json.loads(completed.stdout) == ["kept"]
