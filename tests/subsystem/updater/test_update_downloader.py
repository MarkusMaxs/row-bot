"""Downloads are HTTPS to allow-listed hosts, resumable and hash-checked."""

from __future__ import annotations

import hashlib
import http.server
import threading

import pytest

from row_bot.updates.downloader import (
    ALLOWED_HOSTS,
    DownloadCancelled,
    DownloadError,
    Downloader,
    HttpsTransport,
    check_url,
    loopback_feed_origin,
)
from tests.fixtures.updates import FakeTransport

pytestmark = [pytest.mark.subsystem]

URL = "https://files.pythonhosted.org/packages/demo-1.0-py3-none-any.whl"
DATA = bytes(range(256)) * 40


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _downloader(transport, **kwargs):
    return Downloader(transport, sleep=lambda _seconds: None, **kwargs)


def test_only_https_to_the_allow_list_is_accepted():
    assert {"download.pytorch.org", "download-r2.pytorch.org", "release-assets.githubusercontent.com"} <= ALLOWED_HOSTS
    assert "pypi.org" not in ALLOWED_HOSTS
    check_url("https://download-r2.pytorch.org/whl/cpu/torch.whl")
    for url in (
        "http://files.pythonhosted.org/x.whl",
        "https://evil.example/x.whl",
        "https://files.pythonhosted.org.evil.example/x.whl",
        "file:///etc/passwd",
        "ftp://github.com/x",
    ):
        with pytest.raises(DownloadError) as refused:
            check_url(url)
        assert refused.value.retryable is False
    # A rehearsal feed is a loopback HTTP origin, and nothing else.
    assert loopback_feed_origin("http://127.0.0.1:8765/") == ("http", "127.0.0.1", 8765)
    assert loopback_feed_origin("http://192.168.1.2:8765/") is None
    assert loopback_feed_origin("https://127.0.0.1:8765/") is None


def test_a_refused_url_never_reaches_the_network(tmp_path):
    transport = HttpsTransport()
    with pytest.raises(DownloadError):
        transport.open("https://evil.example/payload", headers={}, timeout=1)
    with pytest.raises(DownloadError):
        _downloader(transport).fetch("http://github.com/x", tmp_path / "x", sha256="0" * 64)
    assert not (tmp_path / "x.part").exists()


def test_a_dropped_connection_resumes_with_range_and_reports_progress(tmp_path):
    transport = FakeTransport({URL: DATA})
    transport.drop_after[URL] = 1000
    seen: list[int] = []

    fetched = _downloader(transport).fetch(
        URL, tmp_path / "demo.whl", sha256=_sha(DATA), size=len(DATA), progress=seen.append
    )

    assert fetched.path.read_bytes() == DATA
    assert fetched.resumed is True
    assert [rng for _url, rng in transport.requests] == [None, "bytes=1000-"]
    assert sum(seen) == len(DATA)
    assert not (tmp_path / "demo.whl.part").exists()


def test_a_server_that_ignores_range_restarts_from_the_beginning(tmp_path):
    transport = FakeTransport({URL: DATA}, ignore_range=True)
    (tmp_path / "demo.whl.part").write_bytes(DATA[:500])
    seen: list[int] = []

    _downloader(transport).fetch(URL, tmp_path / "demo.whl", sha256=_sha(DATA), size=len(DATA), progress=seen.append)

    assert (tmp_path / "demo.whl").read_bytes() == DATA
    assert sum(seen) == len(DATA) - 500


def test_a_hash_or_size_mismatch_discards_the_file(tmp_path):
    transport = FakeTransport({URL: DATA})
    with pytest.raises(DownloadError) as mismatch:
        _downloader(transport).fetch(URL, tmp_path / "demo.whl", sha256="0" * 64, size=len(DATA))
    assert mismatch.value.retryable is False
    with pytest.raises(DownloadError):
        _downloader(transport).fetch(URL, tmp_path / "demo.whl", sha256=_sha(DATA), size=len(DATA) - 1)
    assert list(tmp_path.iterdir()) == []


def test_a_complete_file_is_reused_and_cancel_keeps_the_partial_download(tmp_path):
    transport = FakeTransport({URL: DATA})
    downloader = _downloader(transport)
    downloader.fetch(URL, tmp_path / "demo.whl", sha256=_sha(DATA), size=len(DATA))
    cached = downloader.fetch(URL, tmp_path / "demo.whl", sha256=_sha(DATA), size=len(DATA))
    assert cached.cached is True and len(transport.requests) == 1

    cancel = threading.Event()
    seen: list[int] = []

    def progress(added: int) -> None:
        seen.append(added)
        cancel.set()

    with pytest.raises(DownloadCancelled):
        Downloader(transport, cancel=cancel, sleep=lambda _s: None).fetch(
            URL, tmp_path / "again.whl", sha256=_sha(DATA), size=len(DATA), progress=progress
        )
    assert (tmp_path / "again.whl.part").stat().st_size == sum(seen) > 0
    assert not (tmp_path / "again.whl").exists()


class _Feed(http.server.BaseHTTPRequestHandler):
    payload = DATA
    ranges: list[str | None] = []

    def log_message(self, *_args):
        return None

    def do_GET(self):  # noqa: N802
        if self.path == "/away":
            self.send_response(302)
            self.send_header("Location", "https://evil.example/stolen")
            self.end_headers()
            return
        self.ranges.append(self.headers.get("Range"))
        start = 0
        if self.headers.get("Range"):
            start = int(self.headers["Range"].removeprefix("bytes=").split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(self.payload) - 1}/{len(self.payload)}")
        else:
            self.send_response(200)
        body = self.payload[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def feed():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Feed)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Feed.ranges = []
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_a_loopback_feed_resumes_over_http_and_redirects_are_checked(tmp_path, feed):
    origin = loopback_feed_origin(feed + "/")
    transport = HttpsTransport(extra_origins=[origin])
    (tmp_path / "part.bin.part").write_bytes(DATA[:300])

    _downloader(transport).fetch(f"{feed}/part.bin", tmp_path / "part.bin", sha256=_sha(DATA), size=len(DATA))

    assert (tmp_path / "part.bin").read_bytes() == DATA
    assert _Feed.ranges == ["bytes=300-"]
    # A redirect off the allow-list is refused before any connection to it.
    with pytest.raises(DownloadError) as refused:
        transport.open(f"{feed}/away", headers={}, timeout=2)
    assert "evil.example" in str(refused.value)
    # Without the feed origin, the same loopback URL is refused.
    with pytest.raises(DownloadError):
        HttpsTransport().open(f"{feed}/part.bin", headers={}, timeout=2)
