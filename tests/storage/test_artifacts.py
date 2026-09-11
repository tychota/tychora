import io
import os
import stat
from hashlib import sha256

import pytest

from tychora.adapters.files.cas import LocalArtifacts
from tychora.contracts.errors import TychoraError


def test_artifact_roundtrip_is_verified_and_idempotent(tmp_path):
    store = LocalArtifacts(tmp_path)
    a = store.put_stream(io.BytesIO(b"source bytes"), "text/plain")
    b = store.put_stream(io.BytesIO(b"source bytes"), "text/plain")
    assert a == b
    with store.open_verified(a) as stream:
        assert stream.read() == b"source bytes"


class BoundedStream(io.BytesIO):
    def read(self, size=-1):
        assert 0 < size <= 1024 * 1024
        return super().read(size)


def test_streaming_limit_cleans_staging_without_publishing(tmp_path):
    store = LocalArtifacts(tmp_path, max_size_bytes=4)
    with pytest.raises(TychoraError, match="limit"):
        store.put_stream(BoundedStream(b"12345"), "text/plain")
    assert list(tmp_path.iterdir()) == []
    ref = store.put_stream(BoundedStream(b"1234"), "text/plain")
    assert ref.size_bytes == 4


def test_put_refuses_corrupt_existing_payload(tmp_path):
    store = LocalArtifacts(tmp_path)
    ref = store.put_stream(io.BytesIO(b"original"), "text/plain")
    (tmp_path / ref.storage_key).write_bytes(b"tampered")
    with pytest.raises(TychoraError, match="verification"):
        store.put_stream(io.BytesIO(b"original"), "text/plain")
    assert (tmp_path / ref.storage_key).read_bytes() == b"tampered"


@pytest.mark.parametrize("boundary", ["file_sync", "rename", "directory_sync"])
@pytest.mark.parametrize("after", [False, True])
def test_interruption_never_returns_reference_before_durable_payload(
    tmp_path, monkeypatch, boundary, after
):
    store = LocalArtifacts(tmp_path)
    data = b"durable source bytes"
    digest = sha256(data).hexdigest()
    fsync, replace = os.fsync, os.replace
    events = []

    def action(name, function, *args, **kwargs):
        events.append(name)
        if name == boundary and not after:
            raise KeyboardInterrupt()
        result = function(*args, **kwargs)
        if name == boundary and after:
            raise KeyboardInterrupt()
        return result

    def sync(fd):
        kind = "directory_sync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file_sync"
        return action(kind, fsync, fd)

    def rename(*args, **kwargs):
        return action("rename", replace, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", rename)
    references = []
    with pytest.raises(KeyboardInterrupt):
        references.append(store.put_stream(io.BytesIO(data), "text/plain"))
    assert not references
    assert boundary in events
    paths = list(tmp_path.iterdir())
    assert all(path.name == digest and path.read_bytes() == data for path in paths)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    ref = store.put_stream(io.BytesIO(data), "text/plain")
    with store.open_verified(ref) as stream:
        assert stream.read() == data


def test_private_files_and_durability_order_including_retry(tmp_path, monkeypatch):
    store = LocalArtifacts(tmp_path)
    events = []
    fsync, replace = os.fsync, os.replace

    def sync(fd):
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return fsync(fd)

    def rename(*args, **kwargs):
        events.append("rename")
        return replace(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", rename)
    ref = store.put_stream(io.BytesIO(b"private"), "text/plain")
    assert events == ["file", "rename", "directory"]
    assert stat.S_IMODE((tmp_path / ref.storage_key).stat().st_mode) == 0o600
    events.clear()
    store.put_stream(io.BytesIO(b"private"), "text/plain")
    assert "file" in events and events[-1] == "directory"


@pytest.mark.parametrize(
    "key", ["../escape", "/absolute", "different", "nested/payload"]
)
def test_reference_storage_key_cannot_override_hash_path(tmp_path, key):
    store = LocalArtifacts(tmp_path)
    ref = store.put_stream(io.BytesIO(b"source"), "text/plain")
    escaped = ref.model_copy(update={"storage_key": key})
    with pytest.raises(TychoraError), store.open_verified(escaped):
        pytest.fail("Invalid path was opened")


@pytest.mark.parametrize("operation", ["read", "put", "stat"])
def test_symlink_payload_is_rejected(tmp_path, operation):
    root = tmp_path / "cas"
    store = LocalArtifacts(root)
    ref = store.put_stream(io.BytesIO(b"source"), "text/plain")
    outside = tmp_path / "outside"
    outside.write_bytes(b"source")
    (root / ref.storage_key).unlink()
    (root / ref.storage_key).symlink_to(outside)
    with pytest.raises(TychoraError):
        if operation == "read":
            with store.open_verified(ref):
                pytest.fail("Symlink was opened")
        elif operation == "stat":
            store.stat(ref)
        else:
            store.put_stream(io.BytesIO(b"source"), "text/plain")
    assert outside.read_bytes() == b"source"


@pytest.mark.parametrize("nested", [False, True])
def test_symlink_root_or_ancestor_is_rejected(tmp_path, nested):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(TychoraError):
        LocalArtifacts(link / "nested" if nested else link)
    assert list(outside.iterdir()) == []


def test_stat_checks_content_and_does_not_infer_intentional_deletion(tmp_path):
    store = LocalArtifacts(tmp_path)
    ref = store.put_stream(io.BytesIO(b"source"), "text/plain")
    result = store.stat(ref)
    assert result.available and result.payload_state == "present"
    assert result.payload_deleted_at is None
    (tmp_path / ref.storage_key).write_bytes(b"broken")
    with pytest.raises(TychoraError):
        store.stat(ref)
    (tmp_path / ref.storage_key).unlink()
    missing = store.stat(ref)
    assert not missing.available and missing.payload_state is None
    assert missing.payload_deleted_at is None
    with pytest.raises(TychoraError), store.open_verified(ref):
        pytest.fail("Missing payload was opened")


def test_reader_and_nested_pins_share_state_across_store_instances(tmp_path):
    first, second = LocalArtifacts(tmp_path), LocalArtifacts(tmp_path)
    ref = first.put_stream(io.BytesIO(b"source"), "text/plain")
    assert not second.payload_is_pinned(ref.sha256)
    with pytest.raises(KeyboardInterrupt), first.pin_payloads(frozenset({ref.sha256})):
        with second.open_verified(ref) as stream:
            assert first.payload_is_pinned(ref.sha256)
            assert stream.read() == b"source"
        assert second.payload_is_pinned(ref.sha256)
        raise KeyboardInterrupt()
    assert not second.payload_is_pinned(ref.sha256)


def test_guard_serializes_writer_and_deletion_recheck(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    first, second = LocalArtifacts(tmp_path), LocalArtifacts(tmp_path)
    ref = first.put_stream(io.BytesIO(b"source"), "text/plain")
    started = Event()

    def writer():
        started.set()
        return second.put_stream(io.BytesIO(b"source"), "text/plain")

    with ThreadPoolExecutor() as pool:
        with first.payload_guard(ref.sha256):
            future = pool.submit(writer)
            assert started.wait(2)
            assert not future.done()
            (tmp_path / ref.storage_key).unlink()
        assert future.result(timeout=2) == ref
    with first.open_verified(ref) as stream:
        assert stream.read() == b"source"


def test_cancellation_during_streaming_cleans_partial_payload(tmp_path):
    store = LocalArtifacts(tmp_path)

    class CancelledStream(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise KeyboardInterrupt()
            return super().read(2)

    with pytest.raises(KeyboardInterrupt):
        store.put_stream(CancelledStream(b"partial source"), "text/plain")
    assert list(tmp_path.iterdir()) == []


def test_root_replacement_is_rejected_without_writing_elsewhere(tmp_path):
    root = tmp_path / "cas"
    store = LocalArtifacts(root)
    root.rename(tmp_path / "old")
    root.mkdir()
    with pytest.raises(TychoraError):
        store.put_stream(io.BytesIO(b"source"), "text/plain")
    assert list(root.iterdir()) == []


def test_guard_blocks_completed_put_until_deletion_recheck_exits(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store, other = LocalArtifacts(tmp_path), LocalArtifacts(tmp_path)
    data = b"guarded source"
    ref = store.put_stream(io.BytesIO(data), "text/plain")
    with ThreadPoolExecutor() as pool:
        with store.payload_guard(ref.sha256):
            future = pool.submit(other.put_stream, io.BytesIO(data), "text/plain")
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
        assert future.result(timeout=2) == ref


@pytest.mark.parametrize("nested", [False, True], ids=["root", "ancestor"])
def test_initialization_retry_syncs_parent_of_interrupted_directory(
    tmp_path, monkeypatch, nested
):
    interrupted_directory = tmp_path / "created"
    root = interrupted_directory / "cas" if nested else interrupted_directory
    parent_info = tmp_path.stat()
    parent_identity = (parent_info.st_dev, parent_info.st_ino)
    fsync = os.fsync

    def interrupt_parent_sync(fd):
        info = os.fstat(fd)
        if (
            info.st_dev,
            info.st_ino,
        ) == parent_identity and interrupted_directory.exists():
            raise KeyboardInterrupt()
        fsync(fd)

    monkeypatch.setattr(os, "fsync", interrupt_parent_sync)
    with pytest.raises(KeyboardInterrupt):
        LocalArtifacts(root)
    assert interrupted_directory.is_dir()

    synced = []

    def record_sync(fd):
        fsync(fd)
        info = os.fstat(fd)
        synced.append((info.st_dev, info.st_ino))

    monkeypatch.setattr(os, "fsync", record_sync)
    store = LocalArtifacts(root)
    ref = store.put_stream(
        io.BytesIO(b"durable after initialization retry"), "text/plain"
    )
    assert parent_identity in synced
    with store.open_verified(ref) as stream:
        assert stream.read() == b"durable after initialization retry"
