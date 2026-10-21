"""A provider endpoint on loopback that can hold a request open, for tests of Stop.

    with fake_provider_http([HOLD, Reply(body=b"...")]) as server:
        ...                                   # the client sends its request
        held = server.held(0)                 # the first request, once it is held
        ...                                   # Stop
        assert held.hung_up.wait(2)           # the server saw the client go

Each request is read in full and answered by the next reply in the script (the last one repeats):

- ``Hold()`` sends nothing, not even headers, and holds the connection open, as a provider still working on
  its answer does;
- ``Hold(chunks=[...])`` sends a streamed response's headers and these chunks, then holds the connection open;
- ``Reply(body=...)`` answers in full and closes the connection.

A held request's ``hung_up`` is set once its client has gone: it shut its connection down or closed it. A client
that only drops a read it is blocked in never gets there, and neither does the provider: it goes on answering.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Hold:
    chunks: tuple[bytes, ...] = ()
    content_type: str = "text/event-stream"


@dataclass(frozen=True)
class Reply:
    body: bytes
    content_type: str = "application/json"
    status: int = 200


HOLD = Hold()


@dataclass
class SeenRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    held: threading.Event = field(default_factory=threading.Event)
    hung_up: threading.Event = field(default_factory=threading.Event)


class FakeProviderServer:
    def __init__(self, replies: Sequence[Hold | Reply]) -> None:
        if not replies:
            raise ValueError("a reply script needs at least one reply")
        self._replies = list(replies)
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._listener.getsockname()[1]}"
        self.seen: list[SeenRequest] = []
        self._arrived = threading.Condition()
        self._connections: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._closed = False

    def held(self, index: int = 0, timeout: float = 2.0) -> SeenRequest:
        """The ``index``-th request, once the server holds it (it fails if that doesn't happen in ``timeout``)."""
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
            self._spawn(self._serve, connection)

    def _serve(self, connection: socket.socket) -> None:
        with connection:
            reader = connection.makefile("rb")
            while not self._closed:
                request = _read_request(reader)
                if request is None:
                    return
                with self._arrived:
                    reply = self._replies[min(len(self.seen), len(self._replies) - 1)]
                    self.seen.append(request)
                    self._arrived.notify_all()
                try:
                    if isinstance(reply, Reply):
                        connection.sendall(_complete(reply))
                        return
                    if reply.chunks:
                        connection.sendall(_streamed_head(reply.content_type))
                        for chunk in reply.chunks:
                            connection.sendall(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
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


def _streamed_head(content_type: str) -> bytes:
    return (f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nTransfer-Encoding: chunked\r\n"
            "Cache-Control: no-cache\r\n\r\n").encode()


def _complete(reply: Reply) -> bytes:
    return (f"HTTP/1.1 {reply.status} OK\r\nContent-Type: {reply.content_type}\r\n"
            f"Content-Length: {len(reply.body)}\r\nConnection: close\r\n\r\n").encode() + reply.body


@contextmanager
def fake_provider_http(replies: Sequence[Hold | Reply]) -> Iterator[FakeProviderServer]:
    server = FakeProviderServer(replies)
    server.start()
    try:
        yield server
    finally:
        server.close()
