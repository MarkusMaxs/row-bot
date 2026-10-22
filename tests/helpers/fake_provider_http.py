"""A provider endpoint on loopback that can hold a request open, for tests of Stop.

    with fake_provider_http([HOLD, Reply(body=b"...")]) as server:
        ...                                   # the client sends its request to server.url
        held = server.held(0)                 # the first request, once it is held
        ...                                   # Stop
        assert held.hung_up.wait(2)           # the server saw the client go

Each request is read in full and answered by the next reply in the script (the last one repeats):

- ``Hold()`` sends nothing, not even headers, and holds the connection open, as a provider still working on
  its answer does;
- ``Hold(chunks=(...))`` sends a streamed response's headers and these chunks, then holds the connection open;
- ``Reply(body=...)`` answers in full and closes the connection (``keep_alive=True``: keeps it for the next
  request).

A held request's ``hung_up`` is set once its client has gone: it shut its connection down or closed it. A client
that only drops a read it is blocked in never gets there, and neither does the provider: it goes on answering.
``finish(*chunks)`` answers a held request after all (the rest of its stream; plain HTTP only). With ``tls=True``
the endpoint speaks HTTPS with a certificate made for the test; clients trust it with ``verify=server.client_tls``.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import tempfile
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class Hold:
    chunks: tuple[bytes, ...] = ()
    content_type: str = "text/event-stream"


@dataclass(frozen=True)
class Reply:
    body: bytes
    content_type: str = "application/json"
    status: int = 200
    keep_alive: bool = False


HOLD = Hold()


@dataclass
class SeenRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    connection: int = 0  # which of the server's connections it came on (counted from 1)
    held: threading.Event = field(default_factory=threading.Event)
    hung_up: threading.Event = field(default_factory=threading.Event)
    _connection: socket.socket | None = None
    _content_type: str = "text/event-stream"
    _head_sent: bool = False

    def finish(self, *chunks: bytes) -> None:
        """Answer the held request after all: these chunks, then the end of the stream."""
        assert self._connection is not None and self.held.is_set(), "only a held request can be finished"
        head = b"" if self._head_sent else _streamed_head(self._content_type)
        self._head_sent = True
        self._connection.sendall(head + b"".join(_chunk(chunk) for chunk in chunks) + b"0\r\n\r\n")


class FakeProviderServer:
    def __init__(self, replies: Sequence[Hold | Reply], *, tls: bool = False) -> None:
        if not replies:
            raise ValueError("a reply script needs at least one reply")
        self._replies = list(replies)
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._server_tls, self.client_tls = _tls_contexts() if tls else (None, None)
        self.url = f"{'https' if tls else 'http'}://127.0.0.1:{self._listener.getsockname()[1]}"
        self.seen: list[SeenRequest] = []
        self._arrived = threading.Condition()
        self._connections: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._closed = False

    def held(self, index: int = 0, timeout: float = 10.0) -> SeenRequest:
        """The ``index``-th request, once the server holds it (it fails if that doesn't happen in ``timeout``).

        Getting the request here is set-up, not what a test measures: a busy CI runner can take over 2 s to
        connect, more over TLS (B395), so the bound only keeps a broken test from hanging.
        """
        with self._arrived:
            if not self._arrived.wait_for(lambda: len(self.seen) > index, timeout):
                raise AssertionError(f"request {index} never arrived")
            request = self.seen[index]
        if not request.held.wait(timeout):
            raise AssertionError(f"request {index} was never held")
        return request

    def start(self) -> None:
        self._spawn(self._accept)

    def close(self) -> None:
        self._closed = True
        # Shut down first: on Linux closing a socket doesn't wake an accept (or a read) blocked on it.
        with suppress(OSError):
            self._listener.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            self._listener.close()
        for connection in list(self._connections):
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                connection.close()
        for thread in list(self._threads):
            thread.join(2.0)

    def _spawn(self, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _accept(self) -> None:
        while not self._closed:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            self._connections.append(connection)
            self._spawn(self._serve, connection, len(self._connections))

    def _serve(self, connection: socket.socket, number: int) -> None:
        if self._server_tls is not None:
            try:
                connection = self._server_tls.wrap_socket(connection, server_side=True)
            except OSError:
                return
            self._connections.append(connection)
        with connection:
            reader = connection.makefile("rb")
            while not self._closed:
                request = _read_request(reader)
                if request is None:
                    return
                request.connection = number
                with self._arrived:
                    reply = self._replies[min(len(self.seen), len(self._replies) - 1)]
                    self.seen.append(request)
                    self._arrived.notify_all()
                try:
                    if isinstance(reply, Reply):
                        connection.sendall(_complete(reply))
                        if reply.keep_alive:
                            continue
                        return
                    request._connection, request._content_type = connection, reply.content_type
                    if reply.chunks:
                        request._head_sent = True
                        connection.sendall(_streamed_head(reply.content_type)
                                           + b"".join(_chunk(chunk) for chunk in reply.chunks))
                except OSError:
                    return
                request.held.set()
                _wait_for_hang_up(connection)
                request.hung_up.set()
                return


def _read_request(reader) -> SeenRequest | None:
    try:
        line = reader.readline()
        if not line.strip():
            return None
        method, path, _ = line.decode("latin-1").split(" ", 2)
        headers: dict[str, str] = {}
        while (header := reader.readline()) not in (b"\r\n", b"\n", b""):
            name, _, value = header.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        if headers.get("transfer-encoding", "").lower() == "chunked":
            body = b""
            while size := int(reader.readline().split(b";")[0].strip() or b"0", 16):
                body += reader.read(size)
                reader.readline()
            reader.readline()
        else:
            body = reader.read(int(headers.get("content-length") or 0))
    except (OSError, ValueError):
        return None
    return SeenRequest(method=method, path=path, headers=headers, body=body)


def _wait_for_hang_up(connection: socket.socket) -> None:
    while True:
        try:
            if not connection.recv(4096):
                return
        except OSError:
            return


def _chunk(data: bytes) -> bytes:
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


def _streamed_head(content_type: str) -> bytes:
    return (f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nTransfer-Encoding: chunked\r\n"
            "Cache-Control: no-cache\r\n\r\n").encode()


def _complete(reply: Reply) -> bytes:
    connection = "keep-alive" if reply.keep_alive else "close"
    return (f"HTTP/1.1 {reply.status} OK\r\nContent-Type: {reply.content_type}\r\n"
            f"Content-Length: {len(reply.body)}\r\nConnection: {connection}\r\n\r\n").encode() + reply.body


def _tls_contexts() -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """A server context with a certificate for 127.0.0.1 made now, and a client context that trusts only it."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                       critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / "cert.pem").write_bytes(certificate_pem)
        (Path(folder) / "key.pem").write_bytes(key_pem)
        server.load_cert_chain(Path(folder) / "cert.pem", Path(folder) / "key.pem")
    client = ssl.create_default_context(cadata=certificate_pem.decode("ascii"))
    return server, client


@contextmanager
def fake_provider_http(replies: Sequence[Hold | Reply], *, tls: bool = False) -> Iterator[FakeProviderServer]:
    server = FakeProviderServer(replies, tls=tls)
    server.start()
    try:
        yield server
    finally:
        server.close()


def assert_stop_ends_requests_of(client) -> None:
    """Stop ends a request this ``httpx.Client`` sends at once, before the provider answers, and the provider
    sees the client go."""
    from row_bot.cancellation import CancellationScope, use_cancellation_scope
    from row_bot.providers.transports.cancellable_http import RequestStopped

    with fake_provider_http([HOLD]) as server:
        scope = CancellationScope()
        outcome: dict = {}

        def send() -> None:
            with use_cancellation_scope(scope):
                try:
                    client.post(f"{server.url}/v1/messages", json={})
                except Exception as error:  # noqa: BLE001 - read below
                    outcome["error"] = error

        worker = threading.Thread(target=send, daemon=True)
        worker.start()
        held = server.held(0)
        scope.cancel("test")
        worker.join(2.0)

        assert not worker.is_alive()
        assert isinstance(outcome.get("error"), RequestStopped)
        assert held.hung_up.wait(2.0)
