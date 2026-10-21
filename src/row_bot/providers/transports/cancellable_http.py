"""httpx clients whose requests Stop ends at once, on every OS.

Stop cancels the run's ``CancellationScope``. Each request sent through these clients registers with the scope
active where it is sent, as it starts, and Stop shuts the request's socket down: a read blocked on it ends at
once, whether the provider has sent headers yet or is mid-stream, and the provider sees the client go, so it
stops working on the answer. Closing the response or the client isn't enough: on Linux a close doesn't wake a
read blocked in another thread and the server never sees the client leave, so a turn stayed "stopping" until
the provider answered (and before headers nothing was registered at all, on any OS).

- httpcore's ``trace`` extension hands over the request's socket as it connects (with TLS, the TLS socket); a
  request Stop came before is shut down as it connects, and one Stop came before it was sent isn't sent.
- One connection per request (no keep-alive), so a request's socket is its own: Stop for one run never touches
  another run's request on a shared client. A request on a connection opened earlier (a client that keeps
  connections and borrows these hooks) learns its socket when the response arrives.
- A request Stop ended raises ``RequestStopped``. Provider SDKs retry a request that failed to connect or read;
  given their own subclass (``stopped=``), which the SDK passes on as one of its own errors, they don't send it
  again.

Row-Bot's own transports (``openai_compatible.py``, ``codex_responses.py``, ...) send through these clients, and
SDK chat models get them as their caller-owned HTTP clients.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from row_bot.cancellation import current_cancellation_scope

_ONE_REQUEST_PER_CONNECTION = httpx.Limits(max_keepalive_connections=0)
_CONNECTED = (".connect_tcp.complete", ".start_tls.complete")
_ENDED = (".response_closed.started", ".connect_tcp.failed", ".start_tls.failed")


class RequestStopped(InterruptedError):
    """Stop ended this request, or came before it was sent."""


def _shut(sock: Any) -> None:
    # A shutdown, not a close: only a shutdown wakes a read blocked in another thread on Linux, and it tells the
    # provider the client has gone. The plain socket's shutdown also takes a TLS socket's, with no TLS goodbye.
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except (OSError, TypeError):
        pass  # already closed


class _Request:
    """One request, which Stop ends by shutting its socket down, connected yet or not."""

    def __init__(self, earlier: Callable[..., Any] | None) -> None:
        self._lock = threading.Lock()
        self._socket: Any = None
        self._stopped = False
        self._ended = False
        self._earlier = earlier
        self.unregister: Callable[[], None] = lambda: None

    def stop(self) -> None:
        # Under the lock: once the request is over its connection closes, and its socket is never shut after that.
        with self._lock:
            self._stopped = True
            if self._socket is not None:
                _shut(self._socket)

    def learn(self, sock: Any) -> None:
        with self._lock:
            if self._ended or sock is None:
                return
            self._socket = sock
            if self._stopped:
                _shut(sock)

    def _see(self, event: str, info: Mapping[str, Any]) -> None:
        if event.endswith(_CONNECTED):
            self.learn(info["return_value"].get_extra_info("socket"))
        elif event.endswith(".start_tls.started"):
            # TLS takes the connected socket over; its own socket is learned once TLS is up.
            with self._lock:
                self._socket = None
        elif event.endswith(_ENDED):
            with self._lock:
                self._socket, self._ended = None, True
            self.unregister()

    def trace(self, event: str, info: Mapping[str, Any]) -> None:
        """httpcore's ``trace`` extension (sync clients)."""
        self._see(event, info)
        if self._earlier is not None:
            self._earlier(event, info)

    async def atrace(self, event: str, info: Mapping[str, Any]) -> None:
        """httpcore's ``trace`` extension (async clients)."""
        self._see(event, info)
        if self._earlier is not None:
            await self._earlier(event, info)


def _start(request: httpx.Request, stopped: type[RequestStopped], *, asynchronous: bool) -> None:
    scope = current_cancellation_scope()
    if scope is None:
        return
    if scope.is_cancelled():
        raise stopped("execution_cancelled")
    stop = _Request(request.extensions.get("trace"))
    request.extensions["trace"] = stop.atrace if asynchronous else stop.trace
    stop.unregister = scope.register(stop.stop, "cancellable_http.request.stop")


def _learn_from_response(response: httpx.Response) -> None:
    stop = getattr(response.request.extensions.get("trace"), "__self__", None)
    stream = response.extensions.get("network_stream")
    if isinstance(stop, _Request) and stream is not None:
        stop.learn(stream.get_extra_info("socket"))


async def _alearn_from_response(response: httpx.Response) -> None:
    _learn_from_response(response)


def _raise_if_stopped(stopped: type[RequestStopped], error: Exception) -> None:
    scope = current_cancellation_scope()
    if scope is not None and scope.is_cancelled() and not isinstance(error, RequestStopped):
        raise stopped("execution_cancelled") from error


class _StoppableClient(httpx.Client):
    def __init__(self, *, stopped: type[RequestStopped], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._stopped = stopped

    def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        try:
            return super().send(request, **kwargs)
        except Exception as error:
            _raise_if_stopped(self._stopped, error)
            raise


class _StoppableAsyncClient(httpx.AsyncClient):
    def __init__(self, *, stopped: type[RequestStopped], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._stopped = stopped

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        try:
            return await super().send(request, **kwargs)
        except Exception as error:
            _raise_if_stopped(self._stopped, error)
            raise


def _hooks(kwargs: dict[str, Any], start: Callable[..., Any], learn: Callable[..., Any]) -> dict[str, list]:
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["request"] = [*hooks.get("request", []), start]
    hooks["response"] = [*hooks.get("response", []), learn]
    return hooks


def cancellable_http_client(*, stopped: type[RequestStopped] = RequestStopped, **kwargs: Any) -> httpx.Client:
    """An ``httpx.Client`` whose requests Stop ends at once (see the module docstring).

    ``stopped`` is the error a stopped request raises: an SDK's own subclass of ``RequestStopped``, so the SDK
    doesn't retry it. A caller's event hooks run first.
    """

    def start(request: httpx.Request) -> None:
        _start(request, stopped, asynchronous=False)

    kwargs.setdefault("limits", _ONE_REQUEST_PER_CONNECTION)
    return _StoppableClient(stopped=stopped, event_hooks=_hooks(kwargs, start, _learn_from_response), **kwargs)


def cancellable_async_http_client(
    *, stopped: type[RequestStopped] = RequestStopped, **kwargs: Any
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` whose requests Stop ends at once (see ``cancellable_http_client``)."""

    async def start(request: httpx.Request) -> None:
        _start(request, stopped, asynchronous=True)

    kwargs.setdefault("limits", _ONE_REQUEST_PER_CONNECTION)
    return _StoppableAsyncClient(stopped=stopped, event_hooks=_hooks(kwargs, start, _alearn_from_response),
                                 **kwargs)
