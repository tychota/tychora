"""Share the explicit synthetic real vector fixtures with qualification cases."""

import pytest

from tests.storage.conftest import _Execution, vector_family, vector_index  # noqa: F401


@pytest.fixture
def vector_execution() -> _Execution:
    # Not named ``execution``: that would shadow the Docling plugin's fixture of that name.
    return _Execution()
