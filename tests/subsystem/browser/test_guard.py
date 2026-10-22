"""The local-address guard (F4, N12-A): only requests that look local pause, and each is decided.

Public sites run untouched. An address on this computer runs only for the conversation that
allowed it, and only to read. The local network (private, link-local and metadata IPs, .local
and .internal names) never runs. A fake CDP session stands in for Chromium.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from row_bot.browser import guard

pytestmark = pytest.mark.subsystem


def _granted(conversation: str, origin: str) -> bool:
    return (conversation, origin) == ("chat-a", "http://localhost:5173")


@pytest.mark.parametrize(("url", "conversation", "method", "expected"), [
    ("https://www.homeserve.co.uk/claim", "chat-a", "POST", True),  # The public internet: as any browser.
    ("http://localhost:5173/app", "chat-a", "GET", True),  # Allowed in this chat.
    ("ws://localhost:5173/hmr", "chat-a", "GET", True),  # Its live-reload socket too.
    ("http://localhost:5173/api", "chat-a", "POST", False),  # A grant only reads.
    ("http://localhost:5173/app", "chat-b", "GET", False),  # Another chat's grant.
    ("http://localhost:5173/app", None, "GET", False),  # A page no chat owns.
    ("http://127.0.0.1:5173/app", "chat-a", "GET", False),  # Another origin, though the same computer.
    ("http://localhost:8080/app-v2/", "chat-a", "GET", False),
    ("http://192.168.1.1/admin", "chat-a", "GET", False),  # A router.
    ("http://10.0.0.20:8080/", "chat-a", "GET", False),
    ("http://169.254.169.254/latest/meta-data/", "chat-a", "GET", False),  # Cloud metadata.
    ("http://metadata.google.internal/computeMetadata/v1/", "chat-a", "GET", False),
    ("http://printer.local/", "chat-a", "GET", False),
    ("http://[fd00::1]:3000/", "chat-a", "GET", False),
    ("http://10.example.com/", "chat-a", "GET", True),  # A public name that only looks local.
    ("data:text/html,hi", "chat-a", "GET", True),
])
def test_what_a_page_may_reach(url, conversation, method, expected):
    assert guard.allowed(url, conversation, _granted, method) is expected


def _pattern(pattern: str) -> re.Pattern[str]:
    return re.compile("^" + "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern) + "$")


@pytest.mark.parametrize("url", [
    "http://localhost:5173/", "https://app.localhost/", "http://127.0.0.1:8000/x", "http://[::1]:3000/",
    "http://192.168.0.10/", "http://10.1.2.3/", "http://172.16.0.1/", "http://169.254.169.254/",
    "http://printer.local/", "http://metadata.google.internal/", "http://0.0.0.0:5173/", "http://100.64.0.1/",
])
def test_every_local_looking_address_is_paused_for_a_decision(url):
    assert any(_pattern(pattern).match(url) for pattern in guard.PATTERNS)


@pytest.mark.parametrize("url", ["https://www.homeserve.co.uk/", "https://accounts.google.com/o/oauth2",
                                 "https://js.stripe.com/v3/", "http://example.com/"])
def test_public_sites_are_never_paused(url):
    assert not any(_pattern(pattern).match(url) for pattern in guard.PATTERNS)


class _Session:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.handlers: dict = {}

    def on(self, event, handler) -> None:
        self.handlers[event] = handler

    async def send(self, method, params=None):
        self.sent.append((method, params or {}))
        return {}


def test_a_paused_request_continues_or_fails_and_a_blocked_page_is_reported():
    session = _Session()
    blocked: list[tuple[str, bool]] = []

    async def note(url, document):
        blocked.append((url, document))

    page_guard = guard.PageGuard(session, owner=lambda: "chat-a", granted=_granted, on_blocked=note)

    async def run():
        await page_guard.start()
        await session.handlers["Fetch.requestPaused"]({
            "requestId": "1", "resourceType": "Script",
            "request": {"url": "http://localhost:5173/main.js", "method": "GET"}})
        await session.handlers["Fetch.requestPaused"]({
            "requestId": "2", "resourceType": "Document",
            "request": {"url": "http://192.168.1.1/", "method": "GET"}})
        await session.handlers["Fetch.requestPaused"]({
            "requestId": "3", "resourceType": "XHR",
            "request": {"url": "http://localhost:5173/api/delete", "method": "POST"}})

    asyncio.run(run())
    answers = [(method, params.get("requestId"), params.get("errorReason")) for method, params in session.sent[1:]]
    assert answers == [("Fetch.continueRequest", "1", None), ("Fetch.failRequest", "2", "BlockedByClient"),
                       ("Fetch.failRequest", "3", "BlockedByClient")]
    assert blocked == [("http://192.168.1.1/", True), ("http://localhost:5173/api/delete", False)]
