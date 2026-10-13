"""Record what Row-Bot's provider clients send and receive, for the live verification checks.

:class:`HttpCapture` wraps ``httpx.Client.send`` / ``httpx.AsyncClient.send``
while it is open. It keeps the method, the URL without its query string, a
short allow-list of headers (never credentials), the JSON body, the status,
and the response body (streamed bodies are copied as they are read, up to a
cap). Captured requests ask for an uncompressed reply so streamed bodies can be
parsed.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import httpx

MAX_BODY_BYTES = 4 * 1024 * 1024
REQUEST_HEADERS = frozenset({
    "anthropic-beta", "x-grok-conv-id", "session_id", "x-session-id", "x-opencode-session", "content-type",
})
RESPONSE_HEADERS = frozenset({"content-type", "request-id", "x-request-id", "openai-processing-ms"})
_SECRET_HEADERS = frozenset({"authorization", "x-api-key", "api-key", "x-goog-api-key", "cookie"})


@dataclass
class Exchange:
    method: str
    url: str
    request_headers: dict[str, str] = field(default_factory=dict)
    request_json: Any = None
    status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    body: bytearray = field(default_factory=bytearray)

    def text(self) -> str:
        return bytes(self.body).decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.text())

    def sse_events(self) -> list[dict[str, Any]]:
        """``data:`` JSON payloads of a server-sent-event body, in order."""
        events = []
        for line in self.text().splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    try:
                        events.append(json.loads(payload))
                    except json.JSONDecodeError:
                        continue
        return events

    def ndjson(self) -> list[dict[str, Any]]:
        """One JSON object per line (Ollama's streamed replies)."""
        lines = []
        for line in self.text().splitlines():
            if line.strip():
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return lines


def _url(request: httpx.Request) -> str:
    return str(request.url.copy_with(query=None))


def _headers(headers: httpx.Headers, allowed: frozenset[str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items() if key.lower() in allowed and key.lower() not in _SECRET_HEADERS}


def _request_json(request: httpx.Request) -> Any:
    try:
        content = request.content
    except httpx.RequestNotRead:
        return None
    if not content:
        return None
    try:
        return json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


class _TeeSync(httpx.SyncByteStream):
    def __init__(self, inner: Any, sink: bytearray) -> None:
        self._inner = inner
        self._sink = sink

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._inner:
            room = MAX_BODY_BYTES - len(self._sink)
            if room > 0:
                self._sink.extend(chunk[:room])
            yield chunk

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()


class _TeeAsync(httpx.AsyncByteStream):
    def __init__(self, inner: Any, sink: bytearray) -> None:
        self._inner = inner
        self._sink = sink

    async def __aiter__(self):
        async for chunk in self._inner:
            room = MAX_BODY_BYTES - len(self._sink)
            if room > 0:
                self._sink.extend(chunk[:room])
            yield chunk

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if callable(close):
            await close()


class HttpCapture:
    """Capture provider HTTP exchanges whose request matches *match* while open."""

    def __init__(self, match: Callable[[httpx.Request], bool] | None = None) -> None:
        self._match = match or (lambda _request: True)
        self.exchanges: list[Exchange] = []
        self._lock = threading.Lock()
        self._originals: tuple[Any, Any] | None = None

    def _start(self, request: httpx.Request) -> Exchange | None:
        if not self._match(request):
            return None
        request.headers["Accept-Encoding"] = "identity"
        exchange = Exchange(
            method=request.method,
            url=_url(request),
            request_headers=_headers(request.headers, REQUEST_HEADERS),
            request_json=_request_json(request),
        )
        with self._lock:
            self.exchanges.append(exchange)
        return exchange

    @staticmethod
    def _finish(exchange: Exchange, response: httpx.Response, *, streamed: bool, asynchronous: bool) -> None:
        exchange.status = response.status_code
        exchange.response_headers = _headers(response.headers, RESPONSE_HEADERS)
        if streamed and not getattr(response, "is_stream_consumed", False):
            stream = response.stream
            response.stream = _TeeAsync(stream, exchange.body) if asynchronous else _TeeSync(stream, exchange.body)
        else:
            try:
                exchange.body.extend(response.content[:MAX_BODY_BYTES])
            except httpx.ResponseNotRead:
                pass

    def __enter__(self) -> HttpCapture:
        capture = self
        original_sync = httpx.Client.send
        original_async = httpx.AsyncClient.send

        def send(client: httpx.Client, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
            exchange = capture._start(request)
            response = original_sync(client, request, *args, **kwargs)
            if exchange is not None:
                capture._finish(exchange, response, streamed=bool(kwargs.get("stream")), asynchronous=False)
            return response

        async def asend(client: httpx.AsyncClient, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
            exchange = capture._start(request)
            response = await original_async(client, request, *args, **kwargs)
            if exchange is not None:
                capture._finish(exchange, response, streamed=bool(kwargs.get("stream")), asynchronous=True)
            return response

        self._originals = (original_sync, original_async)
        httpx.Client.send = send  # type: ignore[method-assign]
        httpx.AsyncClient.send = asend  # type: ignore[method-assign]
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._originals is not None:
            httpx.Client.send, httpx.AsyncClient.send = self._originals  # type: ignore[method-assign]
            self._originals = None

    def matching(self, fragment: str) -> list[Exchange]:
        with self._lock:
            return [exchange for exchange in self.exchanges if fragment in exchange.url]
