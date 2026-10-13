"""A loopback HTTP server for the eval's web tasks: pages on 127.0.0.1 only."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping

from .tasks import FIXTURE_PAGES


class FixtureServer:
    """Serves fixed HTML pages on an ephemeral 127.0.0.1 port while open."""

    def __init__(self, pages: Mapping[str, Callable[[], str]] | None = None) -> None:
        self._pages = {path: render() for path, render in (pages or FIXTURE_PAGES).items()}
        pages_by_path = self._pages

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's name
                body = pages_by_path.get(self.path.split("?", 1)[0])
                if body is None:
                    self.send_error(404)
                    return
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="live-eval-fixtures", daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FixtureServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
