"""The collection pass after a publication never fails the add."""

import pytest

from tests.indexing.test_sources import _text_source, _text_workspace
from tychora.application.snapshot_gc import SnapshotGc


@pytest.mark.asyncio
async def test_native_store_error_in_the_post_publication_pass_never_fails_the_add(
    tmp_path, monkeypatch, caplog
):
    def broken(self, *args, **kwargs):
        raise RuntimeError("Chroma I/O failure")

    async with _text_workspace(tmp_path) as (_owner, service, _coordinator, scope):
        monkeypatch.setattr(SnapshotGc, "collect", broken)
        handle = await service.add(_text_source("first"), scope)
    assert handle.state == "succeeded"
    assert "Snapshot collection after publication failed" in caplog.text
