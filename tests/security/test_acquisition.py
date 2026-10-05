import json
from uuid import UUID

import pytest

from tychora.adapters.files.cas import LocalArtifacts
from tychora.configuration.resolve import resolve_config
from tychora.contracts import SourceInput, TychoraError
from tychora.ingestion.acquire import Acquirer
from tychora.ports.execution import ExecutionContext


def configured(tmp_path, settings=None, overrides=None):
    path = tmp_path / "workspace.toml"
    path.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n' + (settings or "")
    )
    return resolve_config(path, "test", overrides or {})


def item(**fields):
    return SourceInput.model_validate(
        {
            "source_key": "notes",
            "collection_id": UUID("00000000-0000-4000-8000-000000000010"),
            "kind": "manual@1",
            **fields,
        }
    )


def test_acquisition_configuration_fingerprint_and_redaction(tmp_path):
    base = configured(tmp_path)
    changed = configured(
        tmp_path,
        "[configuration.acquisition]\nroots=["
        + json.dumps(str(tmp_path.resolve()))
        + "]\n[configuration.acquisition.limits]\nmax_file_bytes=17\n",
    )
    assert changed.acquire_fingerprint != base.acquire_fingerprint
    assert changed.extraction_fingerprint == base.extraction_fingerprint
    assert changed.index_fingerprint == base.index_fingerprint
    assert str(tmp_path.resolve()) not in json.dumps(
        dict(changed.inspect())["values"], default=dict
    )
    assert changed.inspect(owner_authorized=True)["values"]["acquisition"]["roots"] == (
        str(tmp_path.resolve()),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"acquisition": {"roots": ["/"]}},
        {"acquisition": {"url": {"enabled": True}}},
        {"offline": False},
    ],
)
def test_query_overrides_cannot_grant_acquisition_or_network(tmp_path, overrides):
    with pytest.raises(TychoraError) as exc:
        configured(tmp_path, overrides=overrides)
    assert exc.value.code == "INVALID_REQUEST"


def test_lower_only_source_limits_are_enforced(tmp_path):
    config = configured(
        tmp_path, "[configuration.acquisition.limits]\nmax_inline_bytes=5\n"
    )
    acquirer = Acquirer(LocalArtifacts((tmp_path / "cas").resolve()), config)
    with pytest.raises(TychoraError) as exc:
        acquirer.capture(item(type="text", text="Résumé"), ExecutionContext())
    assert exc.value.code == "INVALID_REQUEST"
    with pytest.raises(TychoraError):
        acquirer.capture(
            item(type="text", text="ok", acquisition_limits={"max_inline_bytes": 6}),
            ExecutionContext(),
        )
    with pytest.raises(TychoraError):
        acquirer.capture(
            item(type="text", text="ok", acquisition_limits={"max_inline_bytes": 1}),
            ExecutionContext(),
        )


def test_cas_capacity_rejects_unrepresentable_config(tmp_path):
    config = configured(tmp_path)
    with pytest.raises(TychoraError) as exc:
        Acquirer(
            LocalArtifacts((tmp_path / "cas").resolve(), max_size_bytes=10), config
        )
    assert exc.value.code == "INVALID_REQUEST"


def test_owner_can_raise_file_count_within_reference_capacity(tmp_path):
    config = configured(
        tmp_path,
        "[configuration.acquisition.limits]\nmax_files=20000\nmax_file_bytes=209715200\n",
    )
    assert config.values.acquisition.limits.max_files == 20000


def local_acquirer(tmp_path, **limits):
    root = tmp_path / "inputs"
    root.mkdir(exist_ok=True)
    settings = (
        "[configuration.acquisition]\nroots=[" + json.dumps(str(root.resolve())) + "]\n"
    )
    if limits:
        settings += "[configuration.acquisition.limits]\n" + "".join(
            f"{key}={value}\n" for key, value in limits.items()
        )
    artifacts = LocalArtifacts((tmp_path / "cas").resolve())
    return root, Acquirer(artifacts, configured(tmp_path, settings)), artifacts


@pytest.mark.parametrize(
    "escape", ["absolute", "parent", "symlink", "ancestor_symlink"]
)
def test_file_capture_rejects_scope_escape(tmp_path, escape):
    root, acquirer, _ = local_acquirer(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    if escape == "absolute":
        path = str(outside)
    elif escape == "parent":
        path = "../outside"
    elif escape == "symlink":
        (root / "link").symlink_to(outside)
        path = "link"
    else:
        (root / "link").symlink_to(tmp_path, target_is_directory=True)
        path = "link/outside"
    with pytest.raises(TychoraError) as exc:
        acquirer.capture(item(type="file", path=path), ExecutionContext())
    assert exc.value.code == "FORBIDDEN_SCOPE"


def test_file_and_sidecar_are_immutable_and_missing_sidecar_fails(tmp_path):
    root, acquirer, artifacts = local_acquirer(tmp_path)
    (root / "sub").mkdir()
    (root / "sub/main").write_bytes(b"first")
    (root / "sub/meta").write_bytes(b"edition: 1")
    captured = acquirer.capture(
        item(type="file", path="sub/main", sidecars=("meta",)), ExecutionContext()
    )
    (root / "sub/main").write_bytes(b"later")
    with artifacts.open_verified(captured.original_ref) as stream:
        assert stream.read() == b"first"
    with artifacts.open_verified(captured.sidecar_refs[0]) as stream:
        assert stream.read() == b"edition: 1"
    with pytest.raises(TychoraError) as exc:
        acquirer.capture(
            item(type="file", path="sub/main", sidecars=("missing",)),
            ExecutionContext(),
        )
    assert exc.value.code == "NOT_FOUND"


def test_folder_capture_is_sorted_and_limits_count_total_and_file_bytes(tmp_path):
    root, acquirer, artifacts = local_acquirer(tmp_path)
    (root / "b").write_bytes(b"two")
    (root / "a").write_bytes(b"one")
    captured = acquirer.capture(item(type="folder", path=str(root)), ExecutionContext())
    assert [entry.relative_path for entry in captured.entries] == ["a", "b"]
    assert [entry.source_key for entry in captured.entries] == ["notes/a", "notes/b"]
    with artifacts.open_verified(captured.original_ref) as stream:
        assert [row["relative_path"] for row in json.load(stream)["entries"]] == [
            "a",
            "b",
        ]
    for limits in ({"max_files": 1}, {"max_file_bytes": 2}, {"max_total_bytes": 5}):
        with pytest.raises(TychoraError) as exc:
            acquirer.capture(
                item(type="folder", path=str(root), acquisition_limits=limits),
                ExecutionContext(),
            )
        assert exc.value.code == "INVALID_REQUEST"


def test_duplicate_sidecar_identity_rejected(tmp_path):
    root, acquirer, _ = local_acquirer(tmp_path)
    (root / "main").write_bytes(b"one")
    (root / "meta").write_bytes(b"two")
    with pytest.raises(TychoraError) as exc:
        acquirer.capture(
            item(type="file", path="main", sidecars=("meta", "./meta")),
            ExecutionContext(),
        )
    assert exc.value.code == "INVALID_REQUEST"


def test_unstable_file_retries_then_fails_without_returning_manifest(
    tmp_path, monkeypatch
):
    root, acquirer, artifacts = local_acquirer(tmp_path)
    path = root / "moving"
    path.write_bytes(b"start")
    original_put = artifacts.put_stream
    captures = []

    def move(stream, media_type):
        ref = original_put(stream, media_type)
        captures.append(ref)
        path.write_bytes(b"changed" + str(len(captures)).encode())
        return ref

    monkeypatch.setattr(artifacts, "put_stream", move)
    with pytest.raises(TychoraError) as exc:
        acquirer.capture(item(type="file", path="moving"), ExecutionContext())
    assert exc.value.code == "INTEGRITY_ERROR"
    assert len(captures) == 3


class FakeSocket:
    def __init__(
        self,
        response=b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello",
        peer="93.184.216.34",
    ):
        self.response = bytearray(response)
        self.peer = peer
        self.sent = []
        self.connected = None
        self.closed = False

    def settimeout(self, timeout):
        assert 0 < timeout <= 30

    def connect(self, address):
        self.connected = address

    def getpeername(self):
        return self.peer, 443

    def sendall(self, data):
        self.sent.append(data)

    def recv_into(self, buffer):
        count = min(len(buffer), len(self.response))
        buffer[:count] = self.response[:count]
        del self.response[:count]
        return count

    def close(self):
        self.closed = True


def network_acquirer(
    tmp_path, monkeypatch, *, addresses=None, peers=None, responses=None, offline=False
):
    import socket

    from tychora.adapters.files.url_acquisition import UrlAcquisition

    sockets = []
    resolutions = []
    addresses = addresses or [["93.184.216.34"]]

    def resolve(host, port, **kwargs):
        index = min(len(resolutions), len(addresses) - 1)
        resolutions.append(host)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
            for ip in addresses[index]
        ]

    def make_socket(*args):
        index = len(sockets)
        sock = FakeSocket(
            **({"response": responses[index]} if responses else {}),
            **({"peer": peers[index]} if peers else {}),
        )
        sockets.append(sock)
        return sock

    class TLS:
        def wrap_socket(self, sock, server_hostname):
            assert server_hostname == "example.com"
            return sock

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", make_socket)
    import ssl

    monkeypatch.setattr(ssl, "create_default_context", TLS)
    settings = f'[configuration]\noffline={str(offline).lower()}\n[configuration.acquisition.url]\nenabled=true\nallowed_hosts=["example.com"]\n'
    adapter = UrlAcquisition()
    artifacts = LocalArtifacts((tmp_path / "cas").resolve())
    return (
        Acquirer(artifacts, configured(tmp_path, settings), urls=adapter),
        adapter,
        sockets,
        resolutions,
    )


def test_url_capture_pins_public_address_and_ignores_proxy_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1234")
    acquirer, adapter, sockets, resolutions = network_acquirer(tmp_path, monkeypatch)
    with adapter:
        captured = acquirer.capture(
            item(type="url", url="https://example.com/doc?q=1"), ExecutionContext()
        )
    with acquirer.artifacts.open_verified(captured.original_ref) as stream:
        assert stream.read() == b"hello"
    assert sockets[0].connected == ("93.184.216.34", 443)
    assert sockets[0].sent[0].startswith(b"GET /doc?q=1 HTTP/1.1\r\n")
    assert b"Host: example.com\r\n" in sockets[0].sent[0]
    assert resolutions == ["example.com"]
    assert all(sock.closed for sock in sockets)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x",
        "https://other.example/x",
        "https://user@example.com/x",
        "https://example.com:80/x",
        "https://example.com./x",
        "https://127.0.0.1/x",
        "https://example.com/x#fragment",
    ],
)
def test_url_grammar_denies_before_network(tmp_path, monkeypatch, url):
    acquirer, adapter, sockets, resolutions = network_acquirer(tmp_path, monkeypatch)
    with adapter, pytest.raises(TychoraError) as exc:
        acquirer.capture(item(type="url", url=url), ExecutionContext())
    assert exc.value.code == "FORBIDDEN_SCOPE"
    assert not sockets and not resolutions


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "224.0.0.1", "0.0.0.0", "192.0.2.1"],
)
def test_private_dns_answers_fail_before_connect(tmp_path, monkeypatch, ip):
    acquirer, adapter, sockets, _ = network_acquirer(
        tmp_path, monkeypatch, addresses=[["93.184.216.34", ip]]
    )
    with adapter, pytest.raises(TychoraError) as exc:
        acquirer.capture(
            item(type="url", url="https://example.com/doc"), ExecutionContext()
        )
    assert exc.value.code == "FORBIDDEN_SCOPE"
    assert not sockets


def test_connected_peer_change_is_rejected_before_request(tmp_path, monkeypatch):
    acquirer, adapter, sockets, _ = network_acquirer(
        tmp_path, monkeypatch, peers=["127.0.0.1"]
    )
    with adapter, pytest.raises(TychoraError) as exc:
        acquirer.capture(
            item(type="url", url="https://example.com/doc"), ExecutionContext()
        )
    assert exc.value.code == "FORBIDDEN_SCOPE"
    assert sockets[0].sent == []
    assert sockets[0].closed


@pytest.mark.parametrize(
    "location,answers",
    [
        ("https://127.0.0.1/private", [["93.184.216.34"]]),
        ("/second", [["93.184.216.34"], ["10.0.0.1"]]),
    ],
)
def test_redirect_rechecks_host_and_dns_before_next_request(
    tmp_path, monkeypatch, location, answers
):
    response = f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode()
    acquirer, adapter, sockets, _ = network_acquirer(
        tmp_path, monkeypatch, addresses=answers, responses=[response]
    )
    with adapter, pytest.raises(TychoraError) as exc:
        acquirer.capture(
            item(type="url", url="https://example.com/doc"), ExecutionContext()
        )
    assert exc.value.code == "FORBIDDEN_SCOPE"
    assert len(sockets) == 1


def test_offline_url_never_resolves_or_connects(tmp_path, monkeypatch):
    acquirer, adapter, sockets, resolutions = network_acquirer(
        tmp_path, monkeypatch, offline=True
    )
    with adapter, pytest.raises(TychoraError) as exc:
        acquirer.capture(
            item(type="url", url="https://example.com/doc"), ExecutionContext()
        )
    assert exc.value.code == "CAPABILITY_MISSING"
    assert not sockets and not resolutions


@pytest.mark.parametrize(
    "response",
    [
        b"HTTP/1.1 200 OK\r\n\r\ntoolarge",
        b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\nshort",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n8\r\ntoolarge\r\n0\r\n\r\n",
    ],
)
def test_url_body_size_enforced_without_trusting_headers(
    tmp_path, monkeypatch, response
):
    acquirer, adapter, _, _ = network_acquirer(
        tmp_path, monkeypatch, responses=[response]
    )
    with adapter, pytest.raises(TychoraError) as exc:
        acquirer.capture(
            item(
                type="url",
                url="https://example.com/doc",
                acquisition_limits={"max_file_bytes": 4},
            ),
            ExecutionContext(),
        )
    assert exc.value.code == "INVALID_REQUEST"


def test_dns_deadline_never_dispatches_late_request_and_close_drains(
    tmp_path, monkeypatch
):
    import socket
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    acquirer, adapter, sockets, _ = network_acquirer(tmp_path, monkeypatch)
    started, release, closed = threading.Event(), threading.Event(), threading.Event()

    def blocked_dns(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", blocked_dns)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(
            acquirer.capture,
            item(
                type="url",
                url="https://example.com/doc",
                acquisition_limits={"download_timeout_seconds": 0.05},
            ),
            ExecutionContext(),
        )
        assert started.wait(2)
        with pytest.raises(TychoraError) as exc:
            future.result(timeout=2)
        assert exc.value.code == "DEADLINE_EXCEEDED"

        def close():
            adapter.close()
            closed.set()

        closing = pool.submit(close)
        try:
            assert not closed.wait(0.05)
            assert not sockets
        finally:
            release.set()
            closing.result(timeout=2)
    assert closed.is_set()
    time.sleep(0.01)
    assert not sockets


def test_truncated_http_body_fails_instead_of_capturing_prefix(tmp_path, monkeypatch):
    acquirer, adapter, _, _ = network_acquirer(
        tmp_path,
        monkeypatch,
        responses=[b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\nshort"],
    )
    with adapter, pytest.raises(TychoraError) as exc:
        acquirer.capture(
            item(type="url", url="https://example.com/doc"), ExecutionContext()
        )
    assert exc.value.code == "PROVIDER_FAILURE"


def test_headers_have_a_total_byte_bound(tmp_path, monkeypatch):
    response = (
        b"HTTP/1.1 200 OK\r\n"
        + b"X-Filler: "
        + b"a" * 65000
        + b"\r\nY-Filler: "
        + b"b" * 1000
        + b"\r\n\r\n"
    )
    acquirer, adapter, _, _ = network_acquirer(
        tmp_path, monkeypatch, responses=[response]
    )
    with adapter, pytest.raises(TychoraError) as exc:
        acquirer.capture(
            item(type="url", url="https://example.com/doc"), ExecutionContext()
        )
    assert exc.value.code == "INVALID_REQUEST"


def test_header_budget_counts_consumed_bytes_with_fragmented_body_read_ahead(
    tmp_path, monkeypatch
):
    prefix = b"HTTP/1.1 200 OK\r\nX-Filler: "
    suffix = b"\r\nContent-Length: 10000\r\n\r\n"
    header = prefix + b"a" * (63951 - len(prefix) - len(suffix)) + suffix
    assert len(header) == 63951
    body = b"b" * 10000
    original = FakeSocket.recv_into

    def fragmented(self, buffer):
        return original(self, memoryview(buffer)[:6000])

    monkeypatch.setattr(FakeSocket, "recv_into", fragmented)
    acquirer, adapter, _, _ = network_acquirer(
        tmp_path, monkeypatch, responses=[header + body]
    )
    with adapter:
        captured = acquirer.capture(
            item(type="url", url="https://example.com/doc"), ExecutionContext()
        )
    with acquirer.artifacts.open_verified(captured.original_ref) as stream:
        assert stream.read() == body


def test_cancelled_capture_returns_no_manifest(tmp_path):
    root, acquirer, _ = local_acquirer(tmp_path)
    (root / "main").write_bytes(b"one")
    ctx = ExecutionContext()
    ctx.cancelled.set()
    with pytest.raises(TychoraError) as exc:
        acquirer.capture(item(type="file", path="main"), ctx)
    assert exc.value.code == "CANCELLED"
    assert not list((tmp_path / "cas").glob("[a-f0-9]*"))


def test_registered_root_replacement_is_denied(tmp_path):
    root, acquirer, _ = local_acquirer(tmp_path)
    root.rename(tmp_path / "old-root")
    root.mkdir()
    (root / "new").write_bytes(b"replacement")
    with pytest.raises(TychoraError) as exc:
        acquirer.capture(item(type="file", path="new"), ExecutionContext())
    assert exc.value.code == "FORBIDDEN_SCOPE"


def test_equal_source_and_sidecar_bytes_keep_their_binding_hints(tmp_path):
    root, acquirer, artifacts = local_acquirer(tmp_path)
    (root / "meta").write_bytes(b"same")
    captured = acquirer.capture(
        item(type="text", text="same", sidecars=("meta",)), ExecutionContext()
    )
    assert captured.original_ref.sha256 == captured.sidecar_refs[0].sha256
    assert captured.original_ref.media_type == "text/plain"
    assert captured.sidecar_refs[0].media_type == "application/octet-stream"
    with artifacts.open_verified(captured.manifest_ref) as stream:
        manifest = json.load(stream)
    assert len(manifest["original_artifacts"]) == 2


def test_lowered_redirect_count_prevents_second_connection(tmp_path, monkeypatch):
    response = b"HTTP/1.1 302 Found\r\nLocation: /again\r\nContent-Length: 0\r\n\r\n"
    acquirer, adapter, sockets, _ = network_acquirer(
        tmp_path, monkeypatch, responses=[response]
    )
    with adapter, pytest.raises(TychoraError) as exc:
        acquirer.capture(
            item(
                type="url",
                url="https://example.com/doc",
                acquisition_limits={"max_redirects": 0},
            ),
            ExecutionContext(),
        )
    assert exc.value.code == "INVALID_REQUEST"
    assert len(sockets) == 1


def test_dns_cancellation_keeps_lookup_owned_without_late_request(
    tmp_path, monkeypatch
):
    import socket
    import threading
    from concurrent.futures import ThreadPoolExecutor

    acquirer, adapter, sockets, _ = network_acquirer(tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()
    ctx = ExecutionContext()

    def blocked_dns(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", blocked_dns)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            acquirer.capture, item(type="url", url="https://example.com/doc"), ctx
        )
        assert started.wait(2)
        ctx.cancelled.set()
        try:
            with pytest.raises(TychoraError) as exc:
                future.result(timeout=2)
            assert exc.value.code == "CANCELLED"
            assert not sockets
        finally:
            release.set()
            adapter.close()
    assert not sockets


def test_relative_source_never_uses_cwd_or_chooses_an_ambiguous_root(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "same").write_bytes(b"one")
    (second / "same").write_bytes(b"two")
    settings = (
        "[configuration.acquisition]\nroots="
        + json.dumps([str(first.resolve()), str(second.resolve())])
        + "\n"
    )
    acquirer = Acquirer(
        LocalArtifacts((tmp_path / "cas").resolve()), configured(tmp_path, settings)
    )
    with pytest.raises(TychoraError) as exc:
        acquirer.capture(item(type="file", path="same"), ExecutionContext())
    assert exc.value.code == "INVALID_REQUEST"


def test_missing_repository_path_is_typed_not_found(tmp_path):
    _, acquirer, _ = local_acquirer(tmp_path)
    with pytest.raises(TychoraError) as exc:
        acquirer.capture(item(type="repository", path="anywhere"), ExecutionContext())
    assert exc.value.code == "NOT_FOUND"


def test_symlink_swap_between_resolution_and_open_cannot_read_outside(
    tmp_path, monkeypatch
):
    root, acquirer, _ = local_acquirer(tmp_path)
    (root / "sub").mkdir()
    (root / "sub/main").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "main").write_bytes(b"outside")
    resolve = acquirer._path

    def swap(name, base=None):
        path = resolve(name, base)
        (root / "sub").rename(root / "old")
        (root / "sub").symlink_to(outside, target_is_directory=True)
        return path

    monkeypatch.setattr(acquirer, "_path", swap)
    with pytest.raises(TychoraError) as exc:
        acquirer.capture(item(type="file", path="sub/main"), ExecutionContext())
    assert exc.value.code == "FORBIDDEN_SCOPE"


def test_one_moving_file_retries_to_a_stable_capture(tmp_path, monkeypatch):
    root, acquirer, artifacts = local_acquirer(tmp_path)
    path = root / "main"
    path.write_bytes(b"first")
    put = artifacts.put_stream
    changed = False

    def move_once(stream, media):
        nonlocal changed
        ref = put(stream, media)
        if not changed:
            path.write_bytes(b"stable")
            changed = True
        return ref

    monkeypatch.setattr(artifacts, "put_stream", move_once)
    captured = acquirer.capture(item(type="file", path="main"), ExecutionContext())
    with artifacts.open_verified(captured.original_ref) as stream:
        assert stream.read() == b"stable"


def test_invalid_folder_source_key_is_a_typed_error(tmp_path):
    root, acquirer, _ = local_acquirer(tmp_path)
    (root / "bad\\name").write_bytes(b"one")
    with pytest.raises(TychoraError) as exc:
        acquirer.capture(item(type="folder", path=str(root)), ExecutionContext())
    assert exc.value.code == "INVALID_REQUEST"


def test_identical_bytes_have_consistent_refs_across_acquisition_variants(tmp_path):
    from tychora.contracts.artifacts import physical_identity

    root, acquirer, _ = local_acquirer(tmp_path)
    (root / "main").write_bytes(b"same")
    inline = acquirer.capture(
        item(type="text", text="same", media_type="text/plain"), ExecutionContext()
    )
    file = acquirer.capture(
        item(type="file", path="main", media_type="application/custom"),
        ExecutionContext(),
    )
    assert physical_identity(inline.original_ref) == physical_identity(
        file.original_ref
    )
    assert inline.original_ref.media_type == "text/plain"
    assert file.original_ref.media_type == "application/custom"
    assert inline.source["media_type"] == "text/plain"
    assert file.source["media_type"] == "application/custom"


def test_dns_admission_is_bounded_across_concurrent_captures(tmp_path, monkeypatch):
    import socket
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    acquirer, adapter, sockets, _ = network_acquirer(tmp_path, monkeypatch)
    release = threading.Event()
    started = 0
    lock = threading.Lock()

    def blocked_dns(*args, **kwargs):
        nonlocal started
        with lock:
            started += 1
        assert release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", blocked_dns)
    contexts = [ExecutionContext() for _ in range(4)]
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(
                    acquirer.capture,
                    item(type="url", url="https://example.com/doc"),
                    ctx,
                )
                for ctx in contexts
            ]
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with adapter._dns._lock:
                        if adapter._dns._pending == 4:
                            break
                    time.sleep(0.005)
                with pytest.raises(TychoraError) as exc:
                    acquirer.capture(
                        item(type="url", url="https://example.com/doc"),
                        ExecutionContext(),
                    )
                assert exc.value.code == "WORKSPACE_BUSY"
                assert started == 2
            finally:
                for ctx in contexts:
                    ctx.cancelled.set()
                release.set()
                for future in futures:
                    with pytest.raises(TychoraError):
                        future.result(timeout=2)
    finally:
        adapter.close()
    assert not sockets


def test_nested_folder_order_uses_complete_relative_paths(tmp_path):
    root, acquirer, _ = local_acquirer(tmp_path)
    (root / "a").mkdir()
    (root / "a/x").write_bytes(b"one")
    (root / "a.txt").write_bytes(b"two")
    capture = acquirer.capture(item(type="folder", path=str(root)), ExecutionContext())
    assert [entry.relative_path for entry in capture.entries] == ["a.txt", "a/x"]


def test_root_disappearing_after_resolution_is_a_typed_denial(tmp_path):
    root = tmp_path / "inputs"
    root.mkdir()
    config = configured(
        tmp_path,
        "[configuration.acquisition]\nroots=["
        + json.dumps(str(root.resolve()))
        + "]\n",
    )
    root.rmdir()
    with pytest.raises(TychoraError) as exc:
        Acquirer(LocalArtifacts((tmp_path / "cas").resolve()), config)
    assert exc.value.code == "FORBIDDEN_SCOPE"
