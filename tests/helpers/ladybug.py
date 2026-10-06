"""Select the tabular native result variant used by graph fixtures."""

from ladybug import Connection


def query_rows(connection: Connection, query: str) -> list[list[object]]:
    result = connection.execute(query)
    assert not isinstance(result, list)
    rows: list[list[object]] = []
    for row in result.get_all():
        assert isinstance(row, list)
        rows.append(row)
    return rows
