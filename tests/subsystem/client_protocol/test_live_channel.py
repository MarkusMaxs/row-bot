"""One app-wide push channel for background state (B407).

Every open client hears which background state changed (a workflow run, an
approval, the conversation list) on one authenticated stream, and reads it
again through the normal APIs. The stream carries topic names and revisions,
never content.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading

import pytest
from fastapi.testclient import TestClient

from row_bot.api.v1.routes import create_client_platform_app
from row_bot.api.v1.schemas import LiveFrame
from row_bot.api.v1.security import ClientSecurity
from row_bot.application import live_updates as live_module
from row_bot.application.live_updates import TOPICS, LiveUpdates
from tests.subsystem.client_protocol.test_protocol_security import Service, bootstrap

pytestmark = pytest.mark.subsystem


@pytest.fixture
def live(monkeypatch):
    fresh = LiveUpdates()
    monkeypatch.setattr(live_module, "live_updates", fresh)
    return fresh


def live_client():
    security = ClientSecurity("fixture", clock=lambda: 10.0)
    app = create_client_platform_app(Service(), security=security, choices=lambda: {"models": [], "capabilities": []})
    return app, security


def read_stream(app, headers, *, frames: int, during=None) -> list[dict]:
    """Read `frames` live frames from the stream, running `during` after the first."""

    async def run() -> list[dict]:
        disconnected = asyncio.Event()
        received: list[dict] = []
        buffer = ""

        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal buffer
            if message["type"] == "http.response.start":
                assert message["status"] == 200, message
            if message["type"] != "http.response.body" or not message.get("body"):
                return
            buffer += message["body"].decode()
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                if frame.startswith("event: live"):
                    received.append(json.loads(frame.split("data: ", 1)[1]))
                    if len(received) == 1 and during:
                        during()
                    if len(received) >= frames:
                        disconnected.set()

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/api/v1/live",
                 "raw_path": b"/api/v1/live", "root_path": "", "query_string": b"",
                 "headers": [(b"host", b"localhost"), *((key.lower().encode(), value.encode())
                                                        for key, value in headers.items())],
                 "client": ("127.0.0.1", 1234), "server": ("localhost", 80)}
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
        return received

    return asyncio.run(run())


def test_topics_move_on_from_any_thread_and_bursts_count_once(live):
    start = live.frame()
    workers = [threading.Thread(target=lambda: [live.publish("runs", "conversations") for _ in range(50)])
               for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    frame = live.frame()
    assert frame["epoch"] == start["epoch"]
    assert frame["topics"]["runs"] == frame["topics"]["conversations"] == 200
    assert frame["topics"]["approvals"] == 0
    assert frame["revision"] == start["revision"] + 200
    with pytest.raises(ValueError):
        live.publish("conversation")  # a typo is caught where it is written


def test_the_stream_sends_revisions_at_once_then_one_frame_per_burst(live):
    app, security = live_client()
    live.publish("monitor")
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        _, headers = bootstrap(client)

        def burst():
            for _ in range(25):
                live.publish("runs", "conversations")
            live.publish("approvals")

        first, second = read_stream(app, headers, frames=2, during=burst)
    # The first frame says where things stand, so a client resumes from it.
    assert first["topics"]["monitor"] == 1 and first["topics"]["runs"] == 0
    # The burst arrives coalesced: one frame with each topic's latest revision.
    assert second["topics"]["runs"] == 25 and second["topics"]["approvals"] == 1
    # Topic names and numbers only: nothing a client could not already read.
    for frame in (first, second):
        LiveFrame.model_validate(frame)
        assert set(frame) == {"epoch", "revision", "topics"}
        assert set(frame["topics"]) == set(TOPICS)
        assert all(isinstance(value, int) for value in frame["topics"].values())
    assert security._live == {}


def test_the_log_says_when_a_window_s_stream_opened_and_why_it_ended(live, caplog):
    """A dropped window left no trace in the log (#568)."""
    app, _security = live_client()
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        _, headers = bootstrap(client)
        with caplog.at_level(logging.DEBUG, logger="row_bot.api.v1.routes"):
            read_stream(app, headers, frames=1)
    lines = [record.getMessage() for record in caplog.records if "Live stream" in record.getMessage()]
    assert lines[0].startswith("Live stream opened (session ")
    assert lines[-1].startswith("Live stream ended after ") and ": the window went away" in lines[-1]


def test_a_client_that_missed_changes_resumes_and_a_new_server_resyncs(live, monkeypatch):
    app, _ = live_client()
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        _, headers = bootstrap(client)
        seen = client.get("/api/v1/live/poll", headers=headers).json()
        # Changes while the client was away (asleep, offline) are in its next answer.
        live.publish("approvals")
        live.publish("approvals", "conversations")
        page = client.get("/api/v1/live/poll", headers=headers,
                          params={"after": seen["revision"], "epoch": seen["epoch"]})
        assert page.status_code == 200, page.text
        resumed = page.json()
        assert resumed["topics"]["approvals"] == seen["topics"]["approvals"] + 2
        assert resumed["topics"]["conversations"] == seen["topics"]["conversations"] + 1
        assert resumed["topics"]["runs"] == seen["topics"]["runs"]
        # Another server process numbers again from its own epoch: answered at once.
        restarted = LiveUpdates()
        monkeypatch.setattr(live_module, "live_updates", restarted)
        fresh = client.get("/api/v1/live/poll", headers=headers,
                           params={"after": resumed["revision"], "epoch": resumed["epoch"]}).json()
        assert fresh["epoch"] == restarted.epoch != resumed["epoch"]


def test_the_stream_needs_a_signed_in_client_and_is_bounded(live):
    app, security = live_client()
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        assert client.get("/api/v1/live").status_code == 401
        assert client.get("/api/v1/live/poll").status_code == 401
        _, headers = bootstrap(client)
        forged = {**headers, "X-CSRF-Token": "forged"}
        assert client.get("/api/v1/live/poll", headers=forged).status_code == 403
        assert client.get("/api/v1/live/poll", headers=headers, params={"after": -1}).status_code == 422
        # A handful of streams per window; the rest are refused, not queued.
        current = security._sessions[headers["X-Client-Session"]]
        for _ in range(4):
            security.enter_live(current)
        assert client.get("/api/v1/live/poll", headers=headers).status_code == 429
        for _ in range(4):
            security.leave_live(current)
        assert security._live == {}
