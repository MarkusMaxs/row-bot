"""The installation count's API (N6): the card before the first chat and Settings › System › Privacy."""
from __future__ import annotations

import json

import pytest

from row_bot import install_count
from tests.helpers.fake_provider_http import Reply, fake_provider_http
from tests.subsystem.client_protocol.test_protocol_security import bootstrap, client_app

pytestmark = pytest.mark.subsystem

ACCEPTED = Reply(body=b"", status=204)


@pytest.fixture
def collector(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.delenv(install_count.ENV, raising=False)
    with fake_provider_http([ACCEPTED]) as server:
        monkeypatch.setattr(install_count, "COLLECTOR_URL", server.url)
        yield server
        install_count.stop()


def test_any_signed_in_device_sees_the_card_once_and_its_choice_is_kept(collector):
    local, _, _ = client_app()
    remote, _, _ = client_app(remote=True)
    with local, remote:
        _, local_headers = bootstrap(local)
        _, remote_headers = bootstrap(remote)
        first = remote.get("/api/v1/system/install-count", headers=remote_headers)
        assert first.status_code == 200, first.text
        card = first.json()
        assert card["available"] is True and card["needs_disclosure"] is True and card["enabled"] is True
        assert card["service"] == collector.url.removeprefix("http://")
        # The person leaves it on from a phone: no other device shows the card again.
        chosen = remote.put("/api/v1/system/install-count", headers=remote_headers, json={"enabled": True})
        assert chosen.status_code == 200, chosen.text
        assert chosen.json()["needs_disclosure"] is False
        assert local.get("/api/v1/system/install-count", headers=local_headers).json()["needs_disclosure"] is False
        # Settings › System › Privacy turns it off.
        off = local.put("/api/v1/system/install-count", headers=local_headers, json={"enabled": False})
        assert off.json()["enabled"] is False
        invalid = local.put("/api/v1/system/install-count", headers=local_headers, json={"enabled": "no"})
        assert invalid.status_code == 422
        extra = local.put("/api/v1/system/install-count", headers=local_headers,
                          json={"enabled": True, "install": "0" * 32})
        assert extra.status_code == 422
    assert collector.seen == []


def test_the_api_never_carries_the_id_or_the_secret(collector):
    client, _, _ = client_app()
    with client:
        _, headers = bootstrap(client)
        client.put("/api/v1/system/install-count", headers=headers, json={"enabled": True})
        install_count.tick(1_790_000_000.0)
        read = client.get("/api/v1/system/install-count", headers=headers)
        deleted = client.post("/api/v1/system/install-count/delete", headers=headers)
    counted = json.loads(collector.seen[0].body)["install"]
    secret = json.loads(collector.seen[1].body)["secret"]
    assert read.json()["can_delete"] is True and read.json()["last_sent_on"]
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["outcome"] == "deleted"
    assert deleted.json()["snapshot"]["enabled"] is False and deleted.json()["snapshot"]["can_delete"] is False
    for response in (read, deleted):
        assert counted not in response.text and secret not in response.text


def test_dormant_reads_as_unavailable_and_saves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(install_count, "COLLECTOR_URL", "")
    client, _, _ = client_app()
    with client:
        _, headers = bootstrap(client)
        read = client.get("/api/v1/system/install-count", headers=headers).json()
        chosen = client.put("/api/v1/system/install-count", headers=headers, json={"enabled": True}).json()
    assert read["available"] is False and read["needs_disclosure"] is False
    assert chosen == read
    assert not (tmp_path / "profile" / install_count.STATE_FILE).exists()
