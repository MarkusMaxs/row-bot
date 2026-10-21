"""Row-Bot's HTTP clients end a request at once on Stop: over TLS, from async code, before it is sent.

Each request registers with the run's cancellation scope as it starts, and Stop shuts its socket down (closing it
doesn't wake a read blocked in another thread on Linux). These send requests to a provider on loopback that holds
them open and read what reached it.
"""

import asyncio
import contextvars
import threading

import httpx
import pytest

from row_bot.cancellation import CancellationScope, use_cancellation_scope
from row_bot.providers.transports.cancellable_http import (
    RequestStopped,
    cancellable_async_http_client,
    cancellable_http_client,
)
from tests.helpers.fake_provider_http import HOLD, Hold, Reply, fake_provider_http

# How a blocked read is ended differs by OS (closing a socket doesn't wake it on Linux), so these run everywhere.
pytestmark = [pytest.mark.subsystem, pytest.mark.platform]

# Stop ends the request at once; the bound only keeps a broken test from hanging.
PROMPTLY = 2.0


def _under_scope(call, scope: CancellationScope) -> tuple[threading.Thread, dict]:
    outcome: dict = {}

    def run() -> None:
        with use_cancellation_scope(scope):
            try:
                outcome["result"] = call()
            except BaseException as error:  # noqa: BLE001 - the test reads it
                outcome["error"] = error

    worker = threading.Thread(target=contextvars.copy_context().run, args=(run,), daemon=True)
    worker.start()
    return worker, outcome


def _read_stream(client: httpx.Client, url: str, first: threading.Event) -> list[bytes]:
    received = []
    with client.stream("POST", url, json={"stream": True}) as response:
        for chunk in response.iter_raw():
            received.append(chunk)
            first.set()
    return received


@pytest.mark.parametrize("mid_stream", [False, True], ids=["waiting for headers", "mid-stream"])
def test_stop_ends_a_request_over_tls(mid_stream: bool) -> None:
    reply = Hold(chunks=(b"data: first\n\n",)) if mid_stream else HOLD
    with fake_provider_http([reply], tls=True) as server:
        client = cancellable_http_client(verify=server.client_tls)
        first = threading.Event()
        scope = CancellationScope()
        worker, outcome = _under_scope(lambda: _read_stream(client, f"{server.url}/v1/chat", first), scope)
        held = server.held(0)
        if mid_stream:
            assert first.wait(PROMPTLY)

        scope.cancel("user")
        worker.join(PROMPTLY)

        assert not worker.is_alive()
        assert held.hung_up.wait(PROMPTLY)
        assert isinstance(outcome.get("error"), RequestStopped if not mid_stream else httpx.TransportError)


@pytest.mark.parametrize("mid_stream", [False, True], ids=["waiting for headers", "mid-stream"])
def test_stop_ends_an_async_request(mid_stream: bool) -> None:
    reply = Hold(chunks=(b"data: first\n\n",)) if mid_stream else HOLD
    with fake_provider_http([reply]) as server:
        first = threading.Event()

        async def send() -> list[bytes]:
            async with cancellable_async_http_client() as client:
                received = []
                async with client.stream("POST", f"{server.url}/v1/chat", json={}) as response:
                    async for chunk in response.aiter_raw():
                        received.append(chunk)
                        first.set()
                return received

        scope = CancellationScope()
        worker, outcome = _under_scope(lambda: asyncio.run(send()), scope)
        held = server.held(0)
        if mid_stream:
            assert first.wait(PROMPTLY)

        scope.cancel("user")
        worker.join(PROMPTLY)

        assert not worker.is_alive()
        assert held.hung_up.wait(PROMPTLY)
        assert isinstance(outcome.get("error"), RequestStopped if not mid_stream else httpx.TransportError)


def test_a_request_stopped_before_it_is_sent_raises_the_clients_own_error() -> None:
    class SdkStopped(RequestStopped):
        pass

    with fake_provider_http([HOLD]) as server:
        client = cancellable_http_client(stopped=SdkStopped)
        scope = CancellationScope()
        scope.cancel("user")
        with use_cancellation_scope(scope), pytest.raises(SdkStopped):
            client.post(f"{server.url}/v1/chat", json={})

        assert server.seen == []


def test_a_request_that_failed_after_stop_raises_the_clients_own_error() -> None:
    class SdkStopped(RequestStopped):
        pass

    with fake_provider_http([HOLD]) as server:
        client = cancellable_http_client(stopped=SdkStopped)
        scope = CancellationScope()
        worker, outcome = _under_scope(lambda: client.post(f"{server.url}/v1/chat", json={}), scope)
        server.held(0)

        scope.cancel("user")
        worker.join(PROMPTLY)

        assert isinstance(outcome.get("error"), SdkStopped)
        assert isinstance(outcome["error"].__cause__, httpx.TransportError)


def test_requests_without_a_scope_and_after_a_stop_work_as_usual() -> None:
    with fake_provider_http([HOLD, Reply(body=b'{"ok": true}')]) as server:
        client = cancellable_http_client()
        scope = CancellationScope()
        worker, _ = _under_scope(lambda: client.post(f"{server.url}/v1/chat", json={}), scope)
        server.held(0)
        scope.cancel("user")
        worker.join(PROMPTLY)

        assert client.post(f"{server.url}/v1/chat", json={}).json() == {"ok": True}


def test_cancellable_http_client_preserves_caller_event_hooks():
    seen: list[str] = []

    def custom_hook(response: httpx.Response) -> None:
        seen.append("custom")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = cancellable_http_client(
        transport=httpx.MockTransport(handler),
        event_hooks={"response": [custom_hook]},
    )

    client.get("http://example.test/ping")

    assert seen == ["custom"]
