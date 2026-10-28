"""read_url opens public web pages, and an address on this computer or the person's network once the chat
allows it (5.3.1 A2, shared with the browser). A page the agent reads can't steer it to an address the chat
hasn't allowed or to a cloud metadata service, directly, through a redirect or through a name that rebinds."""

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


@pytest.fixture
def asked(monkeypatch):
    """The person's answers to approvals (True allows), and the approvals asked."""
    from row_bot.browser.service import BrowserSessionManager
    from row_bot.tools import approval_gate, browser_tool

    manager = BrowserSessionManager()
    monkeypatch.setattr(browser_tool, "_session_manager", manager)
    monkeypatch.setattr(approval_gate, "current_approval_mode", lambda: "approve")
    questions: list[dict] = []
    answers: list[bool] = []
    monkeypatch.setattr("langgraph.types.interrupt",
                        lambda payload: questions.append(payload) or (answers.pop(0) if answers else False))
    yield questions, answers, manager
    engine = manager.engine()
    if engine is not None:
        engine._closed = True


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
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "http://[fd00:ec2::254]/latest/meta-data/",
    "http://168.63.129.16/machine?comp=goalstate",
    "http://100.100.100.200/latest/meta-data/",
    "http://rebind.example/",
    "http://intranet.example/",
    "http://cloud.example/latest/meta-data/",
    "http://mixed.example/",
])
def test_metadata_and_public_names_reaching_private_addresses_are_refused_without_asking(web, asked, url):
    from row_bot.tools.url_reader_tool import NOT_PUBLIC_MESSAGE

    questions, _answers, _manager = asked
    assert _read(url) == NOT_PUBLIC_MESSAGE
    assert questions == [] and web.connected == [] and web.served == []


@pytest.mark.parametrize("url", [
    "http://localhost/",
    "http://app.localhost/",
    "http://10.0.0.5/admin",
    "http://192.168.1.1/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
])
def test_an_address_on_this_computer_or_the_network_asks_and_a_no_reads_nothing(web, asked, url):
    questions, _answers, manager = asked

    assert "wasn't read" in _read(url)
    assert questions[0]["tool"] == "read_url" and questions[0]["question"].startswith("Allow Row-Bot to read ")
    assert web.connected == [] and manager.local_address_grants("default") == []


def test_an_allowed_address_is_read_and_the_browser_shares_the_grant(web, asked):
    questions, answers, manager = asked
    origin = f"http://127.0.0.1:{web.loopback_port}"
    web.page(f"{origin}/notes", PAGE)
    answers.append(True)

    assert "Rent rises to 1,250." in _read(f"{origin}/notes")
    assert "Rent rises to 1,250." in _read(f"{origin}/notes")  # Allowed in this chat: not asked again.

    assert len(questions) == 1
    assert [(grant.origin, grant.source) for grant in manager.local_address_grants("default")] == [(origin, "you")]


def test_row_bot_itself_is_never_read(web, asked, monkeypatch):
    questions, _answers, _manager = asked
    monkeypatch.setattr("row_bot.app_port.get_app_port", lambda: 8080)

    assert "is Row-Bot itself" in _read("http://localhost:8080/api/v1/settings")
    assert questions == [] and web.connected == []


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
