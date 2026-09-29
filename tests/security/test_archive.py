"""Archive publication failure leaves no completed backup to trust."""

import pytest

from tychora.adapters.files.archive import ArchiveWriter
from tychora.contracts.errors import TychoraError


def test_archive_exclusive_publish_preserves_racing_destination(tmp_path):
    destination = tmp_path / "backup"
    with ArchiveWriter(destination) as writer:
        destination.mkdir()
        (destination / "sentinel").write_text("existing")
        with pytest.raises(TychoraError) as error:
            writer.finish()
        assert error.value.code == "INVALID_REQUEST"
    assert (destination / "sentinel").read_text() == "existing"
    assert not writer.stage.exists()


def test_archive_failure_cleans_private_staging(tmp_path):
    writer = ArchiveWriter(tmp_path / "backup")
    with pytest.raises(RuntimeError), writer:
        writer.write_json("backup-manifest.json", {"type": "backup"})
        raise RuntimeError("interrupted")
    assert not writer.destination.exists()
    assert not writer.stage.exists()


def test_archive_receipt_files_are_private(tmp_path):
    with ArchiveWriter(tmp_path / "backup") as writer:
        writer.write_json("backup-receipt.json", {"complete": True})
        assert (writer.stage / "backup-receipt.json").stat().st_mode & 0o077 == 0
