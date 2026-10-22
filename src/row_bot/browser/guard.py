"""Keep Row-Bot's browser off this computer and the local network unless a chat allows it (F4).

Every other request runs natively, exactly as in a normal browser: no proxy,
no per-request interception, service workers on, the HTTP cache on. Only
requests whose address *looks* local are paused, through Chromium's own
request interception (CDP ``Fetch``) with narrow URL patterns, and each is
decided here:

- an address on this computer (``localhost``, ``127.0.0.1``, ``[::1]``) runs
  only when the page's conversation has allowed that exact origin (the person
  allowed it, so its forms and uploads work too; paying, sending, deleting and
  the rest still ask first, D19);
- every other local-network address (private, link-local and metadata IPs,
  ``.local`` and ``.internal`` names) never runs.

Residual risk, accepted and documented: a public name that resolves to a
local address (DNS rebinding), requests from workers and service workers,
and WebSockets aren't paused here; Chromium's own local-network protection
(Private Network Access / Local Network Access) refuses public pages' requests
to local addresses, and Row-Bot's browser never grants that permission.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from row_bot.browser.addresses import url_host_kind
from row_bot.browser.local_access import local_development_origin

logger = logging.getLogger(__name__)

# Chromium's Fetch patterns use * and ?; the decision below is exact, so a pattern
# may over-match (``10.example.com``) and the request simply continues.
PATTERNS: tuple[str, ...] = tuple(
    f"{scheme}://{host}"
    for scheme in ("http", "https")
    for host in (
        "localhost*", "*.localhost*", "127.*", "0.*", "10.*", "172.*", "192.168.*", "169.254.*", "100.*",
        "[*", "*.local*", "*.internal*", "*.lan*", "*.home.arpa*", "metadata*", "instance-data*",
    )
)
BLOCKED_REASON = "BlockedByClient"


def allowed(url: str, conversation: str | None, granted: Callable[[str, str], bool], method: str = "GET") -> bool:
    """Whether a request from *conversation*'s page to *url* may run (any method)."""

    del method  # A granted address takes form posts and uploads too.

    kind = url_host_kind(url)
    if kind in {None, "public"}:
        return True
    if kind == "this_computer":
        # A server's live-reload socket (ws://localhost:5173) is covered by its page's grant.
        text = str(url)
        origin = local_development_origin(
            "http" + text[2:] if text[:3].casefold() == "ws:" else "https" + text[3:]
            if text[:4].casefold() == "wss:" else text)
        return bool(origin and conversation and granted(conversation, origin))
    return False


class PageGuard:
    """Decides the paused requests of one page target (or out-of-process frame)."""

    def __init__(
        self,
        session: Any,
        *,
        owner: Callable[[], str | None],
        granted: Callable[[str, str], bool],
        on_blocked: Callable[[str, bool], Awaitable[None] | None] | None = None,
    ) -> None:
        self._session = session
        self._owner = owner
        self._granted = granted
        self._on_blocked = on_blocked

    async def start(self) -> None:
        self._session.on("Fetch.requestPaused", self._paused)
        await self._session.send("Fetch.enable", {"patterns": [{"urlPattern": pattern} for pattern in PATTERNS]})

    async def _paused(self, params: dict[str, Any]) -> None:
        request_id = params.get("requestId")
        request = params.get("request") or {}
        url = str(request.get("url") or "")
        try:
            if allowed(url, self._owner(), self._granted, str(request.get("method") or "GET")):
                await self._session.send("Fetch.continueRequest", {"requestId": request_id})
                return
            await self._session.send("Fetch.failRequest", {"requestId": request_id, "errorReason": BLOCKED_REASON})
        except Exception:
            # The page or its request is gone already: nothing ran.
            logger.debug("A paused browser request could not be answered", exc_info=True)
            return
        logger.info("Row-Bot's browser kept a page off a local address")
        if self._on_blocked is not None:
            outcome = self._on_blocked(url, str(params.get("resourceType") or "") == "Document")
            if outcome is not None:
                await outcome
