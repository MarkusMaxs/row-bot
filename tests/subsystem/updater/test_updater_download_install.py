"""The updater finds a release's background-update files and starts the engine
only when the preferences allow it."""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]


def _release(version: str, *, with_update: bool = True, listed: bool = True, relative: bool = False) -> dict:
    installer = f"Row-Bot-{version}-Windows-x64.exe"
    update = f"row-bot-update-{version}.json"
    lines = [f"  {installer}: sha256={'a' * 64}"]
    if with_update and listed:
        lines.append(f"  {update}: sha256={'b' * 64}")
    body = (
        "Notes\n\n<!-- row-bot-update-manifest -->\n```manifest\nschema: 1\nfiles:\n"
        + "\n".join(lines) + "\n```\n"
    )
    base = "" if relative else f"https://github.com/example/row-bot/releases/download/v{version}/"
    assets = [{"name": installer, "size": 700, "browser_download_url": base + installer}]
    if with_update:
        assets.append({"name": update, "size": 900, "browser_download_url": base + update})
        assets.append({"name": f"row-bot-code-{version}.tar.xz", "size": 8_000,
                       "browser_download_url": base + f"row-bot-code-{version}.tar.xz"})
    return {
        "tag_name": f"v{version}",
        "prerelease": False,
        "published_at": "2026-10-11T12:00:00Z",
        "html_url": f"https://github.com/example/row-bot/releases/tag/v{version}",
        "body": body,
        "assets": assets,
    }


@pytest.fixture
def updater(tmp_path, monkeypatch, reload_for_data_dir):
    (module,) = reload_for_data_dir(tmp_path, "row_bot.updater")
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.delenv("ROW_BOT_UPDATES", raising=False)
    monkeypatch.delenv(module.UPDATE_FEED_ENV, raising=False)
    return module


class _Engine:
    def __init__(self, phase: str = "idle", version: str | None = None, retryable: bool = False):
        from row_bot.updates.engine import EngineStatus

        self.state = EngineStatus(phase, version, retryable=retryable)
        self.started: list[str] = []
        self.full: list[str] = []

    def status(self):
        return self.state

    def start(self, release):
        self.started.append(release.version)
        return True

    def offer_full_installer(self, release, reason):
        self.full.append(release.version)

    def acknowledge(self):
        return None


def test_a_release_carries_its_verified_update_description_and_files(updater):
    info = updater._parse_release(_release("5.2.1"), "stable")
    assert info.update_manifest_sha256 == "b" * 64
    assert info.update_manifest_url.endswith("/row-bot-update-5.2.1.json")
    assert info.update_manifest_size == 900
    assert info.files["row-bot-code-5.2.1.tar.xz"][1] == 8_000
    files = updater.release_files(info)
    assert files.url_for("row-bot-code-5.2.1.tar.xz").startswith("https://github.com/")
    assert files.installer_url.endswith("Row-Bot-5.2.1-Windows-x64.exe")

    # A description the manifest block does not vouch for is never used.
    unlisted = updater._parse_release(_release("5.2.1", listed=False), "stable")
    assert unlisted.update_manifest_url == "" and unlisted.update_manifest_sha256 == ""


def test_a_rehearsal_feed_needs_the_testing_opt_in_and_lists_files_relative_to_itself(updater, monkeypatch):
    monkeypatch.setenv(updater.UPDATE_FEED_ENV, "http://127.0.0.1:8765")
    monkeypatch.setattr(updater, "__version__", "5.2.0")
    requested: list[str] = []

    def fake_feed(feed):
        requested.append(feed)
        return [_release("5.2.1", relative=True)]

    monkeypatch.setattr(updater, "_fetch_feed_releases", fake_feed)
    # Without the test-only opt-in the feed is ignored and GitHub is asked.
    monkeypatch.delenv(updater.UPDATE_FEED_TESTING_ENV, raising=False)
    assert updater.feed_url() is None
    monkeypatch.setattr(updater, "_http_get", lambda *_a, **_k: None)
    updater.check_for_updates(force=True)
    assert requested == []

    monkeypatch.setenv(updater.UPDATE_FEED_TESTING_ENV, "1")
    monkeypatch.setattr(updater, "_http_get", lambda *_a, **_k: pytest.fail("a feed never asks GitHub"))
    info = updater.check_for_updates(force=True)
    assert requested == ["http://127.0.0.1:8765/"]
    assert info.update_manifest_url == "http://127.0.0.1:8765/row-bot-update-5.2.1.json"
    # Anything but a loopback HTTP feed is ignored.
    monkeypatch.setenv(updater.UPDATE_FEED_ENV, "http://example.com/feed/")
    assert updater.feed_url() is None


def test_the_feed_is_read_without_proxies_and_never_redirected_away(updater, monkeypatch):
    """The feed opener is the checked transport with the environment's proxies off."""
    import http.server
    import threading

    class Away(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return None

        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "https://evil.example/releases.json")
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Away)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
        monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
        feed = f"http://127.0.0.1:{server.server_address[1]}/"
        assert updater._fetch_feed_releases(feed) is None  # the redirect was refused, no proxy used
    finally:
        server.shutdown()
        server.server_close()


def test_background_preparation_follows_the_download_preference(updater, monkeypatch):
    engine = _Engine()
    monkeypatch.setattr(updater, "get_engine", lambda: engine)
    state = updater.get_update_state()
    state.available = updater._parse_release(_release("5.2.1"), "stable")

    updater.set_preference("auto_download", False)
    assert updater.start_background_update() is False
    assert engine.started == []
    saved = json.loads(updater._CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["auto_download"] is False and saved["auto_check"] is True
    # An explicit Download still works.
    assert updater.start_background_update(explicit=True) is True
    assert engine.started == ["5.2.1"]

    updater.set_preference("auto_download", True)
    engine.state.phase, engine.state.version = "ready", "5.2.1"
    assert updater.start_background_update() is False  # already ready
    engine.state.phase = "failed"
    assert updater.start_background_update() is False  # not retryable
    engine.state.retryable = True
    assert updater.start_background_update() is True

    with pytest.raises(ValueError):
        updater.set_preference("auto_install", True)


def test_the_version_row_bot_went_back_from_is_only_prepared_when_asked(updater, monkeypatch, tmp_path):
    from row_bot import install_layout
    from tests.fixtures.updates import make_install

    layout = make_install(tmp_path / "app")
    state = install_layout.read_state(layout.root)
    state.last_rollback = {"from": "5.2.1", "to": "5.2.0", "at": "2026-10-11T12:00:00Z",
                           "reason": "2 launches in a row did not become ready"}
    install_layout.write_state(layout.root, state)
    monkeypatch.setattr(install_layout, "detect", lambda **_kwargs: layout)
    monkeypatch.setattr(updater, "is_dev_install", lambda: False)
    engine = _Engine()
    monkeypatch.setattr(updater, "get_engine", lambda: engine)
    updater.get_update_state().available = updater._parse_release(_release("5.2.1"), "stable")
    updater.set_preference("auto_download", True)

    assert updater.held_back_update() == ("5.2.1", "rolled_back")
    assert updater.start_background_update() is False
    assert engine.started == []
    assert updater.start_background_update(explicit=True) is True  # Download again
    assert engine.started == ["5.2.1"]
    # A newer release is prepared as usual.
    updater.get_update_state().available = updater._parse_release(_release("5.2.2"), "stable")
    assert updater.start_background_update() is True


def test_a_release_without_a_background_update_offers_the_full_installer(updater, monkeypatch):
    engine = _Engine()
    monkeypatch.setattr(updater, "get_engine", lambda: engine)
    updater.get_update_state().available = updater._parse_release(_release("5.2.1", with_update=False), "stable")
    assert updater.start_background_update() is False
    assert engine.full == ["5.2.1"] and engine.started == []


def test_the_scheduler_round_honours_auto_check(updater, monkeypatch):
    checks: list[bool] = []
    starts: list[bool] = []
    monkeypatch.setattr(updater, "check_for_updates", lambda force=False: checks.append(force))
    monkeypatch.setattr(updater, "start_background_update", lambda explicit=False: starts.append(explicit))
    updater.set_preference("auto_check", False)
    updater.scheduler_tick()
    assert checks == [] and starts == [False]
    updater.set_preference("auto_check", True)
    updater.scheduler_tick()
    assert checks == [False]
