"""Monitor hears at once when a channel, an app or a plugin starts or stops needing the person.

Its problems (and Home's "Needs you") were read again only on their next minute's poll: only a
failed run and a start-up warning moved the ``monitor`` live topic (B407). Now an app that fails
to connect or connects again moves it too (a channel started with Row-Bot: test_startup_hardening).
"""
from __future__ import annotations

import asyncio

import pytest

from row_bot.application import client_monitor
from row_bot.application import live_updates as live_module
from row_bot.application.live_updates import LiveUpdates

pytestmark = [pytest.mark.subsystem, pytest.mark.mcp_transport]


@pytest.fixture
def live(monkeypatch):
    fresh = LiveUpdates()
    monkeypatch.setattr(live_module, "live_updates", fresh)
    return fresh


@pytest.fixture
def apps(monkeypatch):
    """The MCP runtime with one app that is on, and no other problem reader."""
    from row_bot.mcp_client import runtime
    monkeypatch.setattr(runtime, "_servers", {})
    monkeypatch.setattr(runtime, "_catalog", {})
    monkeypatch.setattr(runtime, "_statuses", {})
    monkeypatch.setattr(runtime, "sdk_available", lambda: True)
    monkeypatch.setattr(runtime.mcp_config, "clear_agent_cache_if_loaded", lambda: None)
    monkeypatch.setattr(runtime, "_get_effective_config", lambda: {"enabled": True, "servers": {
        "Notes": {"enabled": True, "transport": "stdio"}}})
    for reader in ("_channel_problems", "_tunnel_problems", "_plugin_problems", "_app_problems",
                   "_workflow_problems", "_health_problems"):
        monkeypatch.setattr(client_monitor, reader, lambda: [])
    return runtime


def _moved(live: LiveUpdates, before: dict) -> set[str]:
    return {topic for topic, revision in live.frame()["topics"].items() if revision != before[topic]}


def _needs_you() -> list[str]:
    return [problem["id"] for problem in client_monitor.read_attention(include_update=False)["problems"]]


def test_an_app_that_fails_to_connect_and_then_connects_tells_monitor_each_time(apps, live, monkeypatch):
    async def refused(server):
        raise ConnectionError("synthetic connection refused")
    monkeypatch.setattr(apps.McpServerRuntime, "_connect", refused)
    first = apps.McpServerRuntime("Notes", {"enabled": True})
    apps._servers["Notes"] = first
    before = live.frame()["topics"]
    asyncio.run(first.start())
    assert _moved(live, before) == {"monitor"}
    assert _needs_you() == ["mcp"]  # "An app isn't connected", the moment it failed.

    async def connects(server):
        server._status(status="connected", last_error="")

    async def discovered(server):
        return None
    monkeypatch.setattr(apps.McpServerRuntime, "_connect", connects)
    monkeypatch.setattr(apps.McpServerRuntime, "_discover_tools", discovered)
    again = apps.McpServerRuntime("Notes", {"enabled": True})
    apps._servers["Notes"] = again

    async def reconnect():
        task = asyncio.create_task(again.start())
        while apps._statuses["Notes"].status != "connected":
            await asyncio.sleep(0)
        moved, needs = _moved(live, before), _needs_you()
        again.stop_event.set()
        await task
        return moved, needs
    before = live.frame()["topics"]
    moved, needs = asyncio.run(reconnect())
    assert moved == {"monitor"} and needs == []  # Connected again: it no longer needs the person.
