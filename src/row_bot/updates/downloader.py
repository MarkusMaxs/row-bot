"""Resumable, allow-listed, hash-checked downloads for background updates.

Every file the engine fetches is named, with its SHA-256, by the release
(:mod:`.manifest`) or by the lock's ``pylock.toml`` export. Downloads:

- go to HTTPS hosts on :data:`ALLOWED_HOSTS` only, and every redirect is
  checked again (``download.pytorch.org`` redirects to
  ``download-r2.pytorch.org``, GitHub release assets to its CDN);
- resume an interrupted ``.part`` file with an HTTP ``Range`` request;
- are hashed while they stream and only renamed into place when the SHA-256
  (and the size, when known) match;
- report progress and stop between reads when cancelled.

A test or a release rehearsal can point the updater at a loopback feed
(``ROW_BOT_UPDATE_FEED=http://127.0.0.1:<port>/``); that one origin is then
allowed over plain HTTP. Nothing else is.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol

from row_bot.brand import UPDATER_USER_AGENT

logger = logging.getLogger(__name__)

#: HTTPS hosts background updates may download from (PLAN.md "Security").
ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "files.pythonhosted.org",
        "download.pytorch.org",
        "download-r2.pytorch.org",
    }
)
CHUNK = 1 << 20
READ_TIMEOUT_SECONDS = 30.0
MAX_ATTEMPTS = 4
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class DownloadError(RuntimeError):
    """A download failed: refused host, HTTP error, size or hash mismatch."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class DownloadCancelled(RuntimeError):
    """The download was cancelled; the ``.part`` file is kept for resuming."""


class Response(Protocol):
    status: int
    headers: Mapping[str, str]

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


class Transport(Protocol):
    """Opens one GET request. Raises :class:`DownloadError` on HTTP errors."""

    def open(self, url: str, *, headers: Mapping[str, str], timeout: float) -> Response: ...


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


def loopback_feed_origin(feed: str | None) -> tuple[str, str, int | None] | None:
    """The ``http://127.0.0.1:<port>`` origin of a rehearsal feed, if valid."""
    if not feed:
        return None
    scheme, host, port = _origin(feed)
    if scheme != "http" or host not in _LOOPBACK or port is None:
        return None
    return scheme, host, port


def check_url(url: str, *, extra_origins: Iterable[tuple[str, str, int | None]] = ()) -> None:
    """Refuse anything but HTTPS to an allow-listed host (or a loopback feed)."""
    scheme, host, port = _origin(url)
    if (scheme, host, port) in set(extra_origins):
        return
    if scheme != "https":
        raise DownloadError(f"refusing a non-HTTPS download: {scheme or 'no scheme'}", retryable=False)
    if host not in ALLOWED_HOSTS:
        raise DownloadError(f"refusing a download from {host or 'no host'}", retryable=False)


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, extra_origins: tuple[tuple[str, str, int | None], ...]) -> None:
        super().__init__()
        self._extra = extra_origins

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D401
        check_url(newurl, extra_origins=self._extra)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpsTransport:
    """``urllib`` with the allow-list applied to the URL and every redirect."""

    def __init__(self, *, extra_origins: Iterable[tuple[str, str, int | None]] = ()) -> None:
        self._extra = tuple(extra_origins)
        self._opener = urllib.request.build_opener(
            _CheckedRedirects(self._extra),
            # No proxy handler from the environment is dropped on purpose:
            # corporate proxies keep working; the allow-list still applies to
            # every URL urllib is asked to open.
        )

    def open(self, url: str, *, headers: Mapping[str, str], timeout: float) -> Response:
        check_url(url, extra_origins=self._extra)
        request = urllib.request.Request(url, headers={"User-Agent": UPDATER_USER_AGENT, **headers})
        try:
            return self._opener.open(request, timeout=timeout)  # noqa: S310 - checked above
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 416:
                raise DownloadError("range not satisfiable", retryable=True) from None
            raise DownloadError(f"HTTP {exc.code}", retryable=exc.code >= 500 or exc.code == 429) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DownloadError(f"connection failed: {getattr(exc, 'reason', exc)}") from None


@dataclass
class Fetched:
    path: Path
    bytes_downloaded: int
    resumed: bool
    cached: bool


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Downloader:
    """Fetch files into a folder, resuming and verifying each one."""

    def __init__(
        self,
        transport: Transport,
        *,
        cancel: threading.Event | None = None,
        timeout: float = READ_TIMEOUT_SECONDS,
        attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport
        self.cancel = cancel or threading.Event()
        self.timeout = timeout
        self.attempts = max(1, attempts)
        self._sleep = sleep

    def fetch(
        self,
        url: str,
        dest: Path,
        *,
        sha256: str,
        size: int | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> Fetched:
        """Download *url* to *dest*. ``progress`` receives bytes added since the last call.

        A complete *dest* whose hash matches is reused without a request.
        """
        dest = Path(dest)
        expected = sha256.lower()
        if dest.is_file() and (size is None or dest.stat().st_size == size) and file_sha256(dest) == expected:
            if progress:
                progress(dest.stat().st_size)
            return Fetched(dest, 0, False, True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        downloaded = 0
        resumed = False
        last_error: DownloadError | None = None
        for attempt in range(self.attempts):
            if self.cancel.is_set():
                raise DownloadCancelled(dest.name)
            try:
                added, was_resumed = self._stream(url, part, size=size, progress=progress)
                downloaded += added
                resumed = resumed or was_resumed
                break
            except DownloadCancelled:
                raise
            except DownloadError as exc:
                last_error = exc
                if not exc.retryable or attempt == self.attempts - 1:
                    if not exc.retryable:
                        part.unlink(missing_ok=True)
                    raise
                logger.info("Download of %s interrupted (%s); retrying", dest.name, exc)
                self._sleep(min(8.0, 0.5 * 2**attempt))
        else:  # pragma: no cover - the loop always breaks or raises
            raise last_error or DownloadError("download failed")
        actual_size = part.stat().st_size
        if size is not None and actual_size != size:
            part.unlink(missing_ok=True)
            raise DownloadError(f"{dest.name}: expected {size} bytes, got {actual_size}", retryable=False)
        actual = file_sha256(part)
        if actual != expected:
            part.unlink(missing_ok=True)
            raise DownloadError(f"{dest.name}: SHA-256 does not match", retryable=False)
        os.replace(part, dest)
        return Fetched(dest, downloaded, resumed, False)

    def _stream(
        self,
        url: str,
        part: Path,
        *,
        size: int | None,
        progress: Callable[[int], None] | None,
    ) -> tuple[int, bool]:
        offset = part.stat().st_size if part.is_file() else 0
        if size is not None and offset > size:
            part.unlink()
            offset = 0
        if size is not None and offset == size and offset > 0:
            return 0, True
        headers = {"Accept": "application/octet-stream"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            response = self.transport.open(url, headers=headers, timeout=self.timeout)
        except DownloadError as exc:
            if str(exc) == "range not satisfiable":
                # The server has nothing past what we hold: start again.
                part.unlink(missing_ok=True)
            raise
        added = 0
        resumed = False
        try:
            status = int(getattr(response, "status", 200) or 200)
            if offset and status == 206:
                content_range = str(response.headers.get("Content-Range") or "")
                if not content_range.startswith(f"bytes {offset}-"):
                    part.unlink(missing_ok=True)
                    raise DownloadError("unexpected Content-Range", retryable=True)
                mode = "ab"
                resumed = True
            elif status == 200:
                mode = "wb"
                if offset and progress:
                    progress(-offset)
                offset = 0
            else:
                raise DownloadError(f"HTTP {status}", retryable=status >= 500)
            with part.open(mode) as handle:
                while True:
                    if self.cancel.is_set():
                        raise DownloadCancelled(part.name)
                    try:
                        chunk = response.read(CHUNK)
                    except (OSError, TimeoutError) as exc:
                        raise DownloadError(f"connection lost: {exc}") from None
                    if not chunk:
                        break
                    handle.write(chunk)
                    added += len(chunk)
                    if size is not None and offset + added > size:
                        raise DownloadError("more data than expected", retryable=False)
                    if progress:
                        progress(len(chunk))
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
        return added, resumed


__all__ = [
    "ALLOWED_HOSTS",
    "DownloadCancelled",
    "DownloadError",
    "Downloader",
    "Fetched",
    "HttpsTransport",
    "Transport",
    "check_url",
    "file_sha256",
    "loopback_feed_origin",
]
