"""Opt-in HTTPS acquisition with numeric peer pinning and bounded DNS ownership."""

import http.client
import ipaddress
import socket
import ssl
import time
from contextlib import contextmanager
from io import BufferedReader, RawIOBase
from typing import cast
from urllib.parse import urljoin, urlsplit

from tychora.application.runtime import BoundedWorker
from tychora.configuration.models import AcquisitionCeilings, UrlAcquisitionSettings
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


def _deny() -> TychoraError:
    return TychoraError("FORBIDDEN_SCOPE", "URL destination is not permitted")


def _public(value: str):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise _deny() from None
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or "%" in value
        or getattr(address, "ipv4_mapped", None)
        or getattr(address, "sixtofour", None)
        or getattr(address, "teredo", None)
    ):
        raise _deny()
    return address


def _remaining(ctx: ExecutionContext, deadline: float) -> float:
    ctx.checkpoint()
    remaining = min(deadline, ctx.deadline or deadline) - time.monotonic()
    if remaining <= 0:
        raise TychoraError(
            "DEADLINE_EXCEEDED", "URL acquisition deadline exceeded", retryable=True
        )
    return remaining


class _SocketReader(RawIOBase):
    def __init__(self, sock, ctx: ExecutionContext, deadline: float):
        self.sock, self.ctx, self.deadline = sock, ctx, deadline
        self.header_budget: int | None = 65536

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(_remaining(self.ctx, self.deadline))
        count = self.sock.recv_into(buffer)
        _remaining(self.ctx, self.deadline)
        return count


class _HeaderBufferedReader(BufferedReader):
    def __init__(self, reader: _SocketReader):
        super().__init__(reader)
        self._reader = reader

    def readline(self, size: int | None = -1, /) -> bytes:
        line = super().readline(size)
        if self._reader.header_budget is not None:
            self._reader.header_budget -= len(line)
            if self._reader.header_budget < 0:
                raise TychoraError(
                    "INVALID_REQUEST", "URL response headers exceed byte limit"
                )
        return line


class _ResponseSocket:
    def __init__(self, reader: _SocketReader):
        self.reader = reader

    def makefile(self, mode):
        return _HeaderBufferedReader(self.reader)


class UrlAcquisition:
    """Reuse one per owner; close/drain after all acquisition calls have exited.

    Construct explicitly only for enabled online ingestion. Construction creates
    no threads until a DNS lookup is submitted. A supplied DNS lane is owned by
    its caller; otherwise this adapter owns and closes its 2-active/2-queued lane.
    OS DNS lookup cannot be forcibly interrupted; timed-out slots remain occupied
    until lookup exit, and close deliberately drains them.
    """

    def __init__(self, dns: BoundedWorker | None = None):
        self._dns = dns if dns is not None else BoundedWorker(workers=2, queued=2)
        self._owns_dns = dns is None
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._closed = True
        if self._owns_dns:
            self._dns.close()

    def _destination(self, url: str, settings: UrlAcquisitionSettings):
        try:
            if (
                len(url) > 8192
                or any(ord(char) <= 32 or ord(char) >= 127 for char in url)
                or "\\" in url
            ):
                raise _deny()
            parsed = urlsplit(url)
            host = parsed.hostname
            if (
                parsed.scheme not in settings.allowed_schemes
                or not host
                or host not in settings.allowed_hosts
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or parsed.port not in (None, 443)
            ):
                raise _deny()
            authority = f"[{host}]" if ":" in host else host
            if parsed.netloc not in (authority, authority + ":443"):
                raise _deny()
            return (
                host,
                authority,
                (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
            )
        except ValueError:
            raise _deny() from None

    def _addresses(self, host: str, ctx: ExecutionContext, deadline: float):
        try:
            direct = ipaddress.ip_address(host)
        except ValueError:
            direct = None
        if direct is not None:
            return [_public(host)]
        future = self._dns.submit(
            lambda: socket.getaddrinfo(
                host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
        )
        try:
            while True:
                try:
                    answers = future.result(
                        timeout=min(0.05, _remaining(ctx, deadline))
                    )
                    break
                except TimeoutError:
                    if future.done():
                        raise
                    _remaining(ctx, deadline)
        finally:
            # Cancels work still queued; a running lookup keeps its lane slot.
            if not future.done():
                future.cancel()
        if not answers or len(answers) > 64:
            raise _deny()
        addresses = []
        for family, kind, protocol, _, sockaddr in answers:
            if (
                family not in (socket.AF_INET, socket.AF_INET6)
                or kind != socket.SOCK_STREAM
            ):
                raise _deny()
            parts = list(sockaddr)
            resolved_host = parts[0]
            if not isinstance(resolved_host, str):
                raise _deny()
            address = _public(resolved_host)
            if (
                (address.version == 4) != (family == socket.AF_INET)
                or parts[1] != 443
                or family == socket.AF_INET6
                and (len(parts) != 4 or parts[2] or parts[3])
            ):
                raise _deny()
            if address not in addresses:
                addresses.append(address)
        return addresses

    @contextmanager
    def open(
        self,
        url: str,
        settings: UrlAcquisitionSettings,
        limits: AcquisitionCeilings,
        ctx: ExecutionContext,
    ):
        if not settings.enabled or self._closed:
            raise TychoraError(
                "CAPABILITY_MISSING", "URL acquisition is disabled or closed"
            )
        deadline = time.monotonic() + limits.download_timeout_seconds
        try:
            for redirect in range(limits.max_redirects + 1):
                _remaining(ctx, deadline)
                host, authority, target = self._destination(url, settings)
                addresses = self._addresses(host, ctx, deadline)
                selected = addresses[0]
                _remaining(ctx, deadline)
                raw = socket.socket(
                    socket.AF_INET if selected.version == 4 else socket.AF_INET6,
                    socket.SOCK_STREAM,
                )
                connection = raw
                response = None
                try:
                    raw.settimeout(_remaining(ctx, deadline))
                    address = (
                        (str(selected), 443)
                        if selected.version == 4
                        else (str(selected), 443, 0, 0)
                    )
                    raw.connect(address)
                    if _public(raw.getpeername()[0]) != selected:
                        raise _deny()
                    raw.settimeout(_remaining(ctx, deadline))
                    connection = ssl.create_default_context().wrap_socket(
                        raw, server_hostname=host
                    )
                    if _public(connection.getpeername()[0]) != selected:
                        raise _deny()
                    connection.settimeout(_remaining(ctx, deadline))
                    connection.sendall(
                        f"GET {target} HTTP/1.1\r\nHost: {authority}\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n".encode(
                            "ascii"
                        )
                    )
                    reader = _SocketReader(connection, ctx, deadline)
                    # HTTPResponse uses only makefile; adapt its nominal socket annotation.
                    response = http.client.HTTPResponse(
                        cast(socket.socket, _ResponseSocket(reader))
                    )
                    response.begin()
                    reader.header_budget = None
                    if response.status in (301, 302, 303, 307, 308):
                        if redirect == limits.max_redirects:
                            raise TychoraError(
                                "INVALID_REQUEST", "URL redirect limit exceeded"
                            )
                        location = response.getheader("Location")
                        if not location:
                            raise TychoraError(
                                "PROVIDER_FAILURE", "URL redirect has no location"
                            )
                        url = urljoin(url, location)
                        continue
                    if response.status != 200:
                        raise TychoraError(
                            "PROVIDER_FAILURE",
                            "URL server did not return a successful document",
                        )
                    lengths = response.headers.get_all("Content-Length", [])
                    if (
                        len(lengths) > 1
                        or lengths
                        and (not lengths[0].isascii() or not lengths[0].isdecimal())
                        or response.getheader("Content-Encoding", "identity")
                        != "identity"
                        or response.getheader("Transfer-Encoding")
                        not in (None, "chunked")
                        or lengths
                        and response.chunked
                    ):
                        raise TychoraError(
                            "PROVIDER_FAILURE", "URL response framing is unsupported"
                        )
                    if lengths and int(lengths[0]) > limits.max_file_bytes:
                        raise TychoraError(
                            "INVALID_REQUEST", "URL body exceeds acquisition byte limit"
                        )
                    yield response
                    _remaining(ctx, deadline)
                    if response.length not in (None, 0):
                        raise TychoraError(
                            "PROVIDER_FAILURE",
                            "URL response ended before its declared body",
                        )
                    return
                finally:
                    if response is not None:
                        response.close()
                    connection.close()
                    if connection is not raw:
                        raw.close()
        except TimeoutError:
            raise TychoraError(
                "DEADLINE_EXCEEDED", "URL acquisition deadline exceeded", retryable=True
            ) from None
        except (OSError, http.client.HTTPException, ValueError):
            raise TychoraError("PROVIDER_FAILURE", "URL acquisition failed") from None
