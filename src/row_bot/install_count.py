"""Install stats (N6): counting active installations. On by default, documented, off in one click.

Row-Bot's maker counts the installations reporting in the last 7 and 15 days. With a collector set,
Row-Bot's server process sends ``POST <collector>/v1/ping`` with exactly::

    {"schema": 1, "install": "<random 128-bit id>", "version": "5.3.0", "os": "windows|macos|linux|docker"}

on the first start (after the start-up delay and up to an hour of jitter), then at most once every 24
hours while it runs, 0-60 minutes after the 24 hours. Nothing else is sent: no IP field, location,
account, machine name, prompts, files, tools or models. The collector is ``services/install-count``
(a Cloudflare Worker with D1). The owner's decision (N6): on by default, shown only in Settings ›
System › Privacy, and explained on the docs' privacy page and in the release notes.

- Dormant while ``COLLECTOR_URL`` is empty: no ID, nothing sent, nothing shown. The owner fills it in
  once the collector is deployed.
- ``ROW_BOT_INSTALL_COUNT=0`` (or ``false``, ``off``, ``no``) turns it off: no ID, nothing sent.
- The ID and its deletion secret are made at the first send, from random bytes, never from the
  machine. The ID is the first 128 bits of SHA-256 over the secret: a ping carries nothing more, and
  the collector can check a deletion without keeping any secret.
- Both live in ``install_count.json`` in the data folder (a backup never holds it and a restore never
  touches it, ``profile_restore.SECRET_ENTRIES``). They are never logged, and never in diagnostics or
  exports. A file that can't be read sends nothing: it may hold an off.
- Off ("Share install stats" in Settings) stops at once: a request on its way is cut (the cancellable
  HTTP client), and nothing pending is kept. Off is kept across restarts, updates and repairs. On again
  reuses the same ID; nothing is backfilled.
- "Delete what was sent…" turns it off, then asks the collector to delete this installation's rows
  with the ID and the secret; once it has, the ID and the secret are forgotten.
- It never blocks or slows anything: the scheduler is its own thread, and a failure waits for the
  next daily slot.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import random
import secrets
import sys
import tempfile
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from row_bot.cancellation import CancellationScope, use_cancellation_scope
from row_bot.data_paths import get_row_bot_data_dir

logger = logging.getLogger(__name__)

#: Where the count goes, ``https://row-bot-count.<account>.workers.dev``. Empty: the feature is dormant.
COLLECTOR_URL = ""
#: ``0`` (or ``false``, ``off``, ``no``) turns install stats off for this installation.
ENV = "ROW_BOT_INSTALL_COUNT"
SCHEMA = 1
STATE_FILE = "install_count.json"

_START_DELAY_SEC = 60
_DAY_SEC = 24 * 60 * 60
_JITTER_SEC = 60 * 60
_START_JITTER_SEC = 60 * 60
_IDLE_SEC = 6 * 60 * 60
_TIMEOUT_SEC = 10.0
_ID_PREFIX = b"row-bot-install-count:"
_OFF_VALUES = {"0", "false", "off", "no"}
_HEADERS = {"User-Agent": "Row-Bot", "Accept": "*/*"}

_lock = threading.RLock()
_wake = threading.Event()
_stop = threading.Event()
_thread: threading.Thread | None = None
_sending: CancellationScope | None = None


@dataclass
class _State:
    enabled: bool = True
    install: str = ""
    secret: str = ""
    # The next daily slot (epoch seconds); None until the first attempt.
    next_due: float | None = None
    # The UTC day of the last count the collector accepted.
    last_sent: str = ""


# ── where and whether ──────────────────────────────────────────────────────────


def collector() -> str | None:
    """The collector's base address, or None while dormant (or not a safe address)."""
    url = str(COLLECTOR_URL or "").strip().rstrip("/")
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    if parts.scheme == "https" and host:
        return url
    # Tests point it at a loopback fake collector; nothing else goes over plain HTTP.
    if parts.scheme == "http" and _loopback(host):
        return url
    return None


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def turned_off_here() -> bool:
    """``ROW_BOT_INSTALL_COUNT=0``: this installation's set-up turns counting off."""
    return os.environ.get(ENV, "").strip().lower() in _OFF_VALUES


def active() -> bool:
    """Counting can happen here at all: a collector is set and the environment allows it."""
    return collector() is not None and not turned_off_here()


def os_name() -> str:
    from row_bot.runtime_paths import is_containerized_runtime

    if is_containerized_runtime():
        return "docker"
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _version() -> str:
    from row_bot.version import __version__

    return __version__


def install_id(secret: str) -> str:
    """The install ID that belongs to a deletion secret (64 hex characters)."""
    return hashlib.sha256(_ID_PREFIX + bytes.fromhex(secret)).hexdigest()[:32]


# ── the saved state ────────────────────────────────────────────────────────────


def _path() -> Path:
    return get_row_bot_data_dir(create=False) / STATE_FILE


def _hex(value: object, length: int) -> str:
    text = value if isinstance(value, str) else ""
    return text if len(text) == length and all(ch in "0123456789abcdef" for ch in text) else ""


def _load() -> _State:
    """The saved state: on by default; a file that can't be read sends nothing (it may hold an off)."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _State()
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        logger.warning("Install stats settings couldn't be read; nothing is sent until the switch is set again")
        return _State(enabled=False)
    state = _State(
        enabled=raw.get("enabled") is not False,
        last_sent=raw["last_sent"] if isinstance(raw.get("last_sent"), str) else "",
    )
    due = raw.get("next_due")
    if isinstance(due, (int, float)) and not isinstance(due, bool) and due > 0:
        state.next_due = float(due)
    secret = _hex(raw.get("secret"), 64)
    if secret and _hex(raw.get("install"), 32) == install_id(secret):
        state.install, state.secret = install_id(secret), secret
    return state


def _save(state: _State) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Readable by this user only: it holds the deletion secret.
    handle, temporary = tempfile.mkstemp(prefix=f".{STATE_FILE}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump({"schema": 1, **asdict(state)}, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _today(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).date().isoformat()


# ── what the client sees ───────────────────────────────────────────────────────


def view() -> dict[str, Any]:
    """Settings › System › Privacy: whether install stats can be sent here, the switch, and the facts."""
    url = collector()
    with _lock:
        state = _load()
    return {
        "available": url is not None and not turned_off_here(),
        "turned_off_here": url is not None and turned_off_here(),
        "enabled": state.enabled,
        "service": urllib.parse.urlsplit(url).netloc if url else "",
        "version": _version(),
        "os": os_name(),
        "last_sent_on": state.last_sent or None,
        "can_delete": url is not None and bool(state.install) and state.next_due is not None,
    }


def choose(enabled: bool) -> dict[str, Any]:
    """The "Share install stats" switch."""
    if not active():
        return view()
    with _lock:
        state = _load()
        state.enabled = bool(enabled)
        _save(state)
        if not enabled:
            _cut()
    _wake.set()
    return view()


def delete_counted() -> dict[str, Any]:
    """Turn counting off, then ask the collector to delete this installation's rows."""
    url = collector()
    with _lock:
        state = _load()
        if url is None or not state.install:
            return {"outcome": "deleted", "snapshot": view()}
        state.enabled = False
        _save(state)
        _cut()
        body = {"schema": SCHEMA, "install": state.install, "secret": state.secret}
    if not _post(f"{url}/v1/delete", body, CancellationScope()):
        return {"outcome": "failed", "snapshot": view()}
    with _lock:
        state = _load()
        if state.install == body["install"]:
            state.install, state.secret, state.next_due, state.last_sent = "", "", None, ""
            _save(state)
    return {"outcome": "deleted", "snapshot": view()}


# ── sending ─────────────────────────────────────────────────────────────────────


def payload(state: _State) -> dict[str, Any]:
    """Exactly what a ping says."""
    return {"schema": SCHEMA, "install": state.install, "version": _version(), "os": os_name()}


def _cut() -> None:
    """Stop the request on its way, if any (called under ``_lock``)."""
    if _sending is not None:
        _sending.cancel("install_count_off")


def _post(url: str, body: dict[str, Any], scope: CancellationScope) -> bool:
    import httpx

    from row_bot.providers.transports.cancellable_http import RequestStopped, cancellable_http_client

    try:
        with use_cancellation_scope(scope), cancellable_http_client(
            timeout=_TIMEOUT_SEC, follow_redirects=False, trust_env=False, headers=_HEADERS,
        ) as client:
            response = client.post(url, json=body)
    except (httpx.HTTPError, RequestStopped, OSError) as exc:
        # The address and the error's kind only: never the body, the ID or the secret.
        logger.debug("Install stats didn't reach %s (%s)", urllib.parse.urlsplit(url).netloc,
                     type(exc).__name__)
        return False
    if not 200 <= response.status_code < 300:
        logger.debug("Install stats were refused (HTTP %s)", response.status_code)
        return False
    return True


def tick(now: float | None = None) -> float:
    """One scheduler round: send if allowed and the daily slot has come. Seconds until the next round."""
    global _sending
    now = time.time() if now is None else now
    url = collector()
    with _lock:
        state = _load()
        if url is None or turned_off_here() or not state.enabled:
            return _IDLE_SEC
        if state.next_due is not None and now < state.next_due:
            return min(state.next_due - now, _IDLE_SEC)
        if not state.install:
            state.secret = secrets.token_hex(32)
            state.install = install_id(state.secret)
        # The slot is taken before sending: a failure waits for the next one.
        due = state.next_due = now + _DAY_SEC + random.uniform(0, _JITTER_SEC)
        _save(state)
        body = payload(state)
        scope = _sending = CancellationScope()
    try:
        sent = _post(f"{url}/v1/ping", body, scope)
    finally:
        with _lock:
            if _sending is scope:
                _sending = None
    if sent:
        with _lock:
            state = _load()
            if state.install == body["install"]:
                state.last_sent = _today(now)
                _save(state)
    return min(due - now, _IDLE_SEC)


def _loop() -> None:
    # Off the start-up path, and up to an hour later so installations don't all send at once.
    if _stop.wait(_START_DELAY_SEC + random.uniform(0, _START_JITTER_SEC)):
        return
    while not _stop.is_set():
        try:
            wait = tick()
        except Exception:
            logger.debug("The install stats round failed", exc_info=True)
            wait = _IDLE_SEC
        _wake.wait(wait)
        _wake.clear()


def start() -> None:
    """Start the daily scheduler (its own thread; nothing here waits on the network)."""
    global _thread
    if not active():
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _wake.clear()
        _thread = threading.Thread(target=_loop, name="row-bot-install-count", daemon=True)
        _thread.start()


def stop() -> None:
    """Row-Bot is stopping: end the scheduler and cut a request on its way."""
    _stop.set()
    _wake.set()
    with _lock:
        _cut()
