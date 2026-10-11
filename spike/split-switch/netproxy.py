"""Counting HTTPS CONNECT proxy on loopback (Phase 1 spike).

uv and pip honour HTTPS_PROXY; every TLS tunnel goes through here, so the
bytes each host sent us are exact (TLS overhead included) and any host
outside the allow-list shows up.
"""

from __future__ import annotations

import select
import socket
import socketserver
import threading
import time


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:  # noqa: C901 - small state machine
        server: CountingProxy = self.server.owner  # type: ignore[attr-defined]
        client = self.request
        client.settimeout(60)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                return
            data += chunk
            if len(data) > 65536:
                return
        head, _, rest = data.partition(b"\r\n\r\n")
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            server.record("non-connect:" + line[:80], 0, 0)
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            return
        host, _, port = parts[1].rpartition(":")
        try:
            upstream = socket.create_connection((host, int(port or 443)), timeout=30)
        except OSError:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            server.record(host, 0, 0, failed=True)
            return
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        up = down = 0
        if rest:
            upstream.sendall(rest)
            up += len(rest)
        sockets = [client, upstream]
        try:
            while True:
                readable, _, broken = select.select(sockets, [], sockets, 120)
                if broken or not readable:
                    break
                done = False
                for sock in readable:
                    try:
                        chunk = sock.recv(1 << 16)
                    except OSError:
                        done = True
                        break
                    if not chunk:
                        done = True
                        break
                    if sock is client:
                        upstream.sendall(chunk)
                        up += len(chunk)
                    else:
                        client.sendall(chunk)
                        down += len(chunk)
                if done:
                    break
        finally:
            upstream.close()
            server.record(host, down, up)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class CountingProxy:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.hosts: dict[str, dict] = {}
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.owner = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> CountingProxy:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()

    def record(self, host: str, down: int, up: int, failed: bool = False) -> None:
        with self._lock:
            row = self.hosts.setdefault(host, {"tunnels": 0, "bytes_down": 0, "bytes_up": 0, "failed": 0, "last": 0.0})
            row["tunnels"] += 1
            row["bytes_down"] += down
            row["bytes_up"] += up
            row["failed"] += int(failed)
            row["last"] = time.time()

    def reset(self) -> None:
        with self._lock:
            self.hosts = {}

    def snapshot(self) -> dict:
        time.sleep(0.3)  # let closing tunnels record
        with self._lock:
            hosts = {k: dict(v) for k, v in self.hosts.items()}
        return {"hosts": hosts, "bytes_down": sum(v["bytes_down"] for v in hosts.values())}
