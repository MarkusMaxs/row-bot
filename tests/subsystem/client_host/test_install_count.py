"""The installation count (N6) against a loopback fake collector: disclosed first, off in one click.

Each test runs in its own data folder; the clock is passed to ``tick`` so a "day" never waits. The collector
answers each request with the next reply in its script (the last one repeats), as the Worker does: 204 for
an accepted count or deletion.
"""
from __future__ import annotations

import json
import sys
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from row_bot import install_count
from row_bot import profile_restore
from row_bot.application import profile_backup
from tests.helpers.fake_provider_http import HOLD, Reply, fake_provider_http

pytestmark = pytest.mark.subsystem

DAY = 24 * 60 * 60
HOUR = 60 * 60
T0 = 1_790_000_000.0
ACCEPTED = Reply(body=b"", status=204)
REFUSED = Reply(body=b"", status=503)
# A cut request ends at once; the bound only keeps a broken test from hanging.
PROMPTLY = 2.0


@pytest.fixture
def profile(tmp_path, monkeypatch):
    folder = tmp_path / "profile"
    folder.mkdir()
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(folder))
    return folder


@contextmanager
def _collector(monkeypatch, *replies):
    with fake_provider_http(list(replies) or [ACCEPTED]) as server:
        monkeypatch.setattr(install_count, "COLLECTOR_URL", server.url)
        monkeypatch.delenv(install_count.ENV, raising=False)
        try:
            yield server
        finally:
            install_count.stop()


@pytest.fixture
def collector(profile, monkeypatch):
    with _collector(monkeypatch) as server:
        yield server


def _saved(profile) -> dict:
    return json.loads((profile / install_count.STATE_FILE).read_text(encoding="utf-8"))


def _sent(server, path: str) -> list[dict]:
    return [json.loads(item.body) for item in server.seen if item.path == path]


def _pings(server) -> list[dict]:
    return _sent(server, "/v1/ping")


def test_nothing_is_sent_before_the_disclosure(profile, collector):
    view = install_count.view()
    assert view["available"] and view["needs_disclosure"] and view["enabled"]
    for day in range(30):
        install_count.tick(T0 + day * DAY)
    assert collector.seen == []
    # The ID is made only once counting is allowed: a server nobody opened has none.
    assert not (profile / install_count.STATE_FILE).exists()
    assert install_count.view()["can_delete"] is False


def test_a_headless_server_nobody_opened_never_sends(profile, collector, monkeypatch):
    """The real scheduler on a server whose card no device has shown: it runs and sends nothing."""
    rounds = threading.Event()
    real_tick = install_count.tick

    def counted_tick(now=None):
        try:
            return real_tick(now)
        finally:
            rounds.set()

    monkeypatch.setattr(install_count, "tick", counted_tick)
    monkeypatch.setattr(install_count, "_START_DELAY_SEC", 0)
    install_count.start()
    assert rounds.wait(PROMPTLY)
    thread = install_count._thread
    install_count.stop()
    thread.join(PROMPTLY)
    assert not thread.is_alive()
    assert collector.seen == []
    assert not (profile / install_count.STATE_FILE).exists()


def test_a_count_says_exactly_the_allowlist(profile, collector):
    from row_bot.version import __version__

    install_count.choose(True)
    install_count.tick(T0)
    assert [(item.method, item.path) for item in collector.seen] == [("POST", "/v1/ping")]
    ping = json.loads(collector.seen[0].body)
    assert list(ping) == ["schema", "install", "version", "os"]
    assert ping["schema"] == 1
    assert len(ping["install"]) == 32 and int(ping["install"], 16) >= 0
    assert ping["version"] == __version__
    assert ping["os"] == {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    # Nothing rides along in the headers either: no cookie, credential or version.
    headers = collector.seen[0].headers
    assert headers["content-type"] == "application/json"
    assert headers["user-agent"] == "Row-Bot"
    assert not {"cookie", "authorization", "x-forwarded-for"} & set(headers)
    view = install_count.view()
    assert view["last_sent_on"] is not None and view["can_delete"] is True


def test_docker_says_docker(profile, collector, monkeypatch):
    monkeypatch.setenv("ROW_BOT_CONTAINERIZED", "1")
    install_count.choose(True)
    install_count.tick(T0)
    assert _pings(collector)[0]["os"] == "docker"
    assert install_count.view()["os"] == "docker"


def test_at_most_once_a_day_up_to_an_hour_late(profile, collector):
    install_count.choose(True)
    install_count.tick(T0)
    for moment in (T0 + 60, T0 + 12 * HOUR, T0 + DAY - 1):
        install_count.tick(moment)
    assert len(_pings(collector)) == 1
    # The next slot is 24 hours later plus up to an hour; a restart keeps it (it is saved).
    due = _saved(profile)["next_due"]
    assert T0 + DAY <= due <= T0 + DAY + HOUR
    install_count.tick(due + 1)
    install_count.tick(due + 2)
    assert len(_pings(collector)) == 2
    assert _pings(collector)[0]["install"] == _pings(collector)[1]["install"]


def test_off_sends_nothing(profile, collector):
    install_count.choose(False)
    for day in range(10):
        install_count.tick(T0 + day * DAY)
    assert collector.seen == []
    view = install_count.view()
    assert view["enabled"] is False and view["needs_disclosure"] is False


def test_off_cuts_a_count_on_its_way(profile, monkeypatch):
    with _collector(monkeypatch, HOLD, ACCEPTED) as collector:
        install_count.choose(True)
        sending = threading.Thread(target=install_count.tick, args=(T0,), daemon=True)
        sending.start()
        held = collector.held(0)
        install_count.choose(False)
        assert held.hung_up.wait(PROMPTLY)
        sending.join(PROMPTLY)
        assert not sending.is_alive()
        # Cut before the answer: not recorded as sent, and nothing pending afterwards.
        assert install_count.view()["last_sent_on"] is None
        for day in range(1, 10):
            install_count.tick(T0 + day * DAY)
        assert len(_pings(collector)) == 1


def test_on_again_reuses_the_id_and_backfills_nothing(profile, collector):
    install_count.choose(True)
    install_count.tick(T0)
    first = _pings(collector)[0]["install"]
    install_count.choose(False)
    for day in range(1, 11):
        install_count.tick(T0 + day * DAY)
    install_count.choose(True)
    install_count.tick(T0 + 11 * DAY)
    install_count.tick(T0 + 11 * DAY + 60)
    assert [ping["install"] for ping in _pings(collector)] == [first, first]


def test_a_failure_waits_for_the_next_daily_slot(profile, monkeypatch):
    with _collector(monkeypatch, REFUSED, ACCEPTED) as collector:
        install_count.choose(True)
        install_count.tick(T0)
        install_count.tick(T0 + 60)
        install_count.tick(T0 + 6 * HOUR)
        assert len(_pings(collector)) == 1
        assert install_count.view()["last_sent_on"] is None
        install_count.tick(_saved(profile)["next_due"] + 1)
        assert len(_pings(collector)) == 2
        assert install_count.view()["last_sent_on"] is not None


def test_start_up_never_waits_for_the_count(profile, monkeypatch):
    """The scheduler is its own thread; Row-Bot stopping cuts a count on its way."""
    with _collector(monkeypatch, HOLD) as collector:
        install_count.choose(True)
        monkeypatch.setattr(install_count, "_START_DELAY_SEC", 0)
        install_count.start()
        # start() came back while the count waits for its answer.
        held = collector.held(0)
        thread = install_count._thread
        assert thread is not None and thread.daemon
        install_count.stop()
        assert held.hung_up.wait(PROMPTLY)
        thread.join(PROMPTLY)
        assert not thread.is_alive()


def test_an_existing_off_survives_an_upgrade_and_a_restore(profile, collector, monkeypatch, tmp_path):
    install_count.choose(True)
    install_count.tick(T0)
    install_count.choose(False)
    counted = _pings(collector)[0]["install"]

    # The next version starts on the same profile.
    monkeypatch.setattr("row_bot.version.__version__", "9.9.0")
    view = install_count.view()
    assert view["version"] == "9.9.0"
    assert view["enabled"] is False and view["needs_disclosure"] is False and view["can_delete"] is True
    install_count.tick(T0 + 5 * DAY)
    assert len(_pings(collector)) == 1

    # A backup never holds the ID or the secret, and restoring one leaves them, and the off, in place.
    (profile / "user_config.json").write_text("{}", encoding="utf-8")
    archive = Path(profile_backup.create_backup(profile, tmp_path / "backups")["path"])
    assert install_count.STATE_FILE not in zipfile.ZipFile(archive).namelist()
    profile_backup.stage_restore(archive, profile, source_name=archive.name)
    profile_restore.apply_pending(profile)
    assert _saved(profile)["install"] == counted
    assert install_count.view()["enabled"] is False
    install_count.tick(T0 + 9 * DAY)
    assert len(_pings(collector)) == 1


def test_delete_counted_data_turns_counting_off_and_forgets_the_id(profile, collector):
    install_count.choose(True)
    install_count.tick(T0)
    counted = _pings(collector)[0]["install"]

    result = install_count.delete_counted()
    assert result["outcome"] == "deleted"
    deletion = _sent(collector, "/v1/delete")[0]
    assert list(deletion) == ["schema", "install", "secret"]
    # The collector checks the secret against the ID it holds a keyed hash of.
    assert deletion["install"] == counted == install_count.install_id(deletion["secret"])
    view = result["snapshot"]
    assert view["enabled"] is False and view["can_delete"] is False
    saved = _saved(profile)
    assert saved["install"] == "" and saved["secret"] == ""
    install_count.tick(T0 + 2 * DAY)
    assert len(_pings(collector)) == 1

    # Counting again later starts with a new ID.
    install_count.choose(True)
    install_count.tick(T0 + 3 * DAY)
    assert _pings(collector)[-1]["install"] != counted


def test_a_deletion_the_service_missed_keeps_counting_off_and_can_be_tried_again(profile, monkeypatch):
    with _collector(monkeypatch, ACCEPTED, REFUSED, ACCEPTED):
        install_count.choose(True)
        install_count.tick(T0)
        result = install_count.delete_counted()
        assert result["outcome"] == "failed"
        assert result["snapshot"]["enabled"] is False and result["snapshot"]["can_delete"] is True
        assert install_count.delete_counted()["outcome"] == "deleted"


def test_the_environment_switch_turns_it_off(profile, collector, monkeypatch):
    monkeypatch.setenv(install_count.ENV, "0")
    view = install_count.view()
    assert view["available"] is False and view["turned_off_here"] is True
    assert view["needs_disclosure"] is False
    install_count.choose(True)
    install_count.tick(T0)
    install_count.start()
    assert install_count._thread is None or not install_count._thread.is_alive()
    assert collector.seen == []
    assert not (profile / install_count.STATE_FILE).exists()


def test_dormant_while_no_collector_is_set(profile, monkeypatch):
    monkeypatch.setattr(install_count, "COLLECTOR_URL", "")
    view = install_count.view()
    assert view["available"] is False and view["needs_disclosure"] is False
    assert view["turned_off_here"] is False and view["service"] == ""
    install_count.choose(True)
    install_count.tick(T0)
    assert not (profile / install_count.STATE_FILE).exists()
    # Only HTTPS leaves this computer.
    monkeypatch.setattr(install_count, "COLLECTOR_URL", "http://row-bot-count.example.workers.dev")
    assert install_count.view()["available"] is False


def test_the_id_and_the_secret_never_reach_the_log(profile, monkeypatch, caplog):
    caplog.set_level("DEBUG")
    with _collector(monkeypatch, REFUSED, ACCEPTED) as collector:
        install_count.choose(True)
        install_count.tick(T0)
        install_count.tick(T0 + 2 * DAY)
        install_count.delete_counted()
        deletion = _sent(collector, "/v1/delete")[0]
    assert deletion["install"] not in caplog.text and deletion["secret"] not in caplog.text


def test_the_id_is_derived_from_the_secret_as_the_collector_checks_it():
    # The same vectors are checked in services/install-count/test/worker.test.js.
    assert install_count.install_id("0" * 63 + "1") == "2271477d6128edbe27a9239e54f22a90"
    assert install_count.install_id("ab" * 32) == "1144938174f096fa7ad56132be06b552"


def test_a_damaged_file_asks_again_before_counting(profile, collector):
    install_count.choose(False)
    (profile / install_count.STATE_FILE).write_text("{not json", encoding="utf-8")
    view = install_count.view()
    assert view["needs_disclosure"] is True
    install_count.tick(T0)
    assert collector.seen == []
