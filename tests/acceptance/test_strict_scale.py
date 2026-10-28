"""Strict search for an ordinary phrase succeeds at 20, 1,000 and 10,000 documents."""

import pytest

from tests.helpers.legacy import OutputCounter
from tychora import Tychora


def _add_many(kb, count):
    for index in range(count):
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": f"doc-{index:05}.md",
                "collection_id": "default",
                "kind": "document@1",
                "format": "markdown",
                "text": f"# Record {index}\n\nThe verification outcome of item {index} is stored.\n",
                "metadata": {"title": f"Record {index}"},
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"


@pytest.mark.parametrize(
    "count",
    [
        20,
        pytest.param(1000, marks=pytest.mark.slow),
        pytest.param(10_000, marks=pytest.mark.slow),
    ],
)
def test_verification_outcome_succeeds_strict(tmp_path, count):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add_many(kb, count)
        result = kb.search({"question": "verification outcome"})
    assert result.segments
    assert result.coverage.status == "complete"
