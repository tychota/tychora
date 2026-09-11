import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tychora.adapters.sqlite.jobs import JobRepository
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pytest

from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.sqlite.connection import open_corpus
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.requests import RequestScope


@dataclass
class JobsFixture:
    db: sqlite3.Connection
    artifacts: LocalArtifacts
    scope: RequestScope
    config: ArtifactRef
    repo: "JobRepository"
    path: Path

    def accept(self, *args):
        return self.repo.accept(*args)

    def manifest(self, kind="registry", **fields):
        return self.artifacts.put_stream(
            BytesIO(canonical_bytes({"type": kind, "schema_version": "1", **fields})),
            "application/json",
        )


class NoPublication:
    def lookup(self, job_id):
        return None


@pytest.fixture
def jobs(tmp_path):
    from tychora.adapters.sqlite.jobs import JobRepository

    path = tmp_path / "corpus.sqlite3"
    db = open_corpus(path, initialize=True)
    db.execute("INSERT INTO workspaces VALUES ('workspace',1,NULL,0,0,'2026-01-01')")
    db.execute(
        "INSERT INTO collections VALUES ('collection','workspace','manuals','Manuals',0,'{}')"
    )
    artifacts = LocalArtifacts((tmp_path / "artifacts").resolve())
    config = artifacts.put_stream(
        BytesIO(b'{"type":"registry","schema_version":"1","data":{"offline":true}}'),
        "application/json",
    )
    scope = RequestScope(
        principal_id="writer",
        collection_ids=frozenset({"collection"}),
        capabilities=frozenset({"read", "write"}),
    )
    fixture = JobsFixture(
        db,
        artifacts,
        scope,
        config,
        JobRepository(
            db, artifacts=artifacts, workspace_id="workspace", receipts=NoPublication()
        ),
        path,
    )
    yield fixture
    db.close()
