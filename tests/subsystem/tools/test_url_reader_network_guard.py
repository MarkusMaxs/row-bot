"""read_url opens public web pages only: a page the agent reads can't steer it to this computer, the local
network or a cloud metadata service, directly, through a redirect or through a name that rebinds."""

from __future__ import annotations

import pytest

from tests.fixtures.public_web import PUBLIC_ADDRESS, public_web

pytestmark = pytest.mark.subsystem

PAGE = b"<html><body><p>Rent rises to 1,250.</p></body></html>"


@pytest.fixture
def web(monkeypatch):
    with public_web(monkeypatch) as site:
        site.dns.update({
            "news.example": [PUBLIC_ADDRESS],
            "rebind.example": ["127.0.0.1"],
            "intranet.example": ["192.168.0.10"],
            "cloud.example": ["169.254.169.254"],
            "mixed.example": [PUBLIC_ADDRESS, "10.0.0.7"],
        })
        yield site


def _read(url: str) -> str:
    from row_bot.tools.url_reader_tool import _read_url

    return _read_url(url)


def test_a_public_page_is_read_over_a_connection_to_its_checked_address(web):
    web.page("http://news.example/rent", PAGE)

    text = _read("http://news.example/rent")

    assert text.startswith("SOURCE_URL: http://news.example/rent")
    assert "Rent rises to 1,250." in text
    # Connected to the address that was checked, still asking for the site by name.
    assert web.connected == [(PUBLIC_ADDRESS, "news.example")]
    assert web.resolved == ["news.example"]


@pytest.mark.parametrize("url", [
    "http://localhost/",
    "http://app.localhost/",
    "http://printer.local/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "http://[fd00:ec2::254]/latest/meta-data/",
    "http://168.63.129.16/machine?comp=goalstate",
    "http://100.100.100.200/latest/meta-data/",
    "http://10.0.0.5/admin",
    "http://192.168.1.1/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://rebind.example/",
    "http://intranet.example/",
    "http://cloud.example/latest/meta-data/",
    "http://mixed.example/",
])
def test_links_to_this_computer_the_network_or_metadata_are_refused_before_connecting(web, url):
    from row_bot.tools.url_reader_tool import NOT_PUBLIC_MESSAGE

    assert _read(url) == NOT_PUBLIC_MESSAGE
    assert web.connected == []
    assert web.served == []


def test_the_loopback_server_itself_is_refused(web):
    from row_bot.tools.url_reader_tool import NOT_PUBLIC_MESSAGE

    assert _read(f"http://127.0.0.1:{web.loopback_port}/") == NOT_PUBLIC_MESSAGE
    assert web.served == []


@pytest.mark.parametrize("location", [
    "http://169.254.169.254/latest/meta-data/",
    "http://rebind.example/",
    "http://localhost:8080/api/v1/settings",
    "http://[fd00:ec2::254]/",
])
def test_a_redirect_to_a_private_address_is_refused_at_that_hop(web, location):
    from row_bot.tools.url_reader_tool import NOT_PUBLIC_MESSAGE

    web.redirect("http://news.example/story", location)

    assert _read("http://news.example/story") == NOT_PUBLIC_MESSAGE
    assert web.connected == [(PUBLIC_ADDRESS, "news.example")]


def test_each_redirect_hop_is_resolved_and_checked_again(web):
    web.dns["mirror.example"] = [PUBLIC_ADDRESS]
    web.redirect("http://news.example/old", "/new", status=301)
    web.redirect("http://news.example/new", "http://mirror.example/rent", status=307)
    web.page("http://mirror.example/rent", PAGE)

    text = _read("http://news.example/old")

    assert text.startswith("SOURCE_URL: http://mirror.example/rent")
    assert web.resolved == ["news.example", "news.example", "mirror.example"]
    assert web.served == ["news.example/old", "news.example/new", "mirror.example/rent"]


def test_a_name_is_looked_up_once_per_hop_so_it_cannot_rebind_before_the_connection(web):
    lookups = iter([[PUBLIC_ADDRESS], ["127.0.0.1"], ["127.0.0.1"]])
    web.dns["news.example"] = lambda: next(lookups)
    web.page("http://news.example/rent", PAGE)

    assert "Rent rises to 1,250." in _read("http://news.example/rent")
    assert web.resolved == ["news.example"]
    assert web.connected == [(PUBLIC_ADDRESS, "news.example")]


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://news.example/file",
    "gopher://news.example/",
    "http://user:secret@news.example/",
    "news.example/rent",
    "",
])
def test_other_schemes_and_credentials_in_the_link_are_refused(web, url):
    assert _read(url).startswith("Failed to fetch URL: ")
    assert web.connected == []


def test_a_redirect_loop_stops(web):
    web.redirect("http://news.example/loop", "/loop")

    assert "redirected more than" in _read("http://news.example/loop")
    assert len(web.served) == 11
