"""Subprocess entry point for the Ladybug persistence qualification gate."""

from __future__ import annotations

import json
import signal
import sys
from pathlib import Path

from ladybug import Connection, Database

PERSISTENCE_FIXTURE = """CREATE NODE TABLE Probe(id STRING PRIMARY KEY, value STRING);
BEGIN TRANSACTION;
CREATE (:Probe {id: 'kept', value: 'source'});
COMMIT;
BEGIN TRANSACTION;
CREATE (:Probe {id: 'rolled_back', value: 'discard'});
ROLLBACK;
MATCH (p:Probe) RETURN p.id ORDER BY p.id;"""


def _execute_fixture(connection: Connection) -> list[str]:
    result = None
    for statement in PERSISTENCE_FIXTURE.splitlines():
        result = connection.execute(statement)
    assert result is not None
    return [row[0] for row in result.get_all()]


def _read_ids(connection: Connection) -> list[str]:
    result = connection.execute("MATCH (p:Probe) RETURN p.id ORDER BY p.id;")
    return [row[0] for row in result.get_all()]


def persist(database_path: Path) -> dict[str, list[str]]:
    database = Database(database_path)
    connection_one = Connection(database)
    connection_two = Connection(database)
    try:
        first_rows = _execute_fixture(connection_one)
        second_rows = _read_ids(connection_two)
    finally:
        connection_two.close()
        connection_one.close()
        database.close()

    reopened_database = Database(database_path)
    reopened_connection = Connection(reopened_database)
    try:
        reopened_rows = _read_ids(reopened_connection)
    finally:
        reopened_connection.close()
        reopened_database.close()
    return {
        "connection_one": first_rows,
        "connection_two": second_rows,
        "reopened": reopened_rows,
    }


def hold_uncommitted_write(database_path: Path) -> None:
    database = Database(database_path)
    connection = Connection(database)
    connection.execute("BEGIN TRANSACTION;")
    connection.execute("CREATE (:Probe {id: 'interrupted', value: 'discard'});")
    print("transaction-open", flush=True)
    signal.pause()


def verify(database_path: Path) -> list[str]:
    database = Database(database_path)
    connection = Connection(database)
    try:
        return _read_ids(connection)
    finally:
        connection.close()
        database.close()


def main() -> None:
    mode, raw_path = sys.argv[1:]
    database_path = Path(raw_path)
    if mode == "persist":
        print(json.dumps(persist(database_path), sort_keys=True))
    elif mode == "uncommitted-writer":
        hold_uncommitted_write(database_path)
    elif mode == "verify":
        print(json.dumps(verify(database_path)))
    else:
        raise SystemExit(f"unknown probe mode: {mode}")


if __name__ == "__main__":
    main()
