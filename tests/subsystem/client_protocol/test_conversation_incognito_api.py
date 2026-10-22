"""Incognito through the client API (N11): New incognito chat, the chat's switch, and the flag on its view."""
from __future__ import annotations

import pytest

from tests.subsystem.client_protocol.test_protocol_application import _client, _command, service  # noqa: F401
from tests.subsystem.client_protocol.test_protocol_security import bootstrap

pytestmark = pytest.mark.subsystem


def _view(client, headers, conversation: str) -> dict:
    items = client.get("/api/v1/conversations", headers=headers).json()["items"]
    return next(row for row in items if row["id"] == conversation)


def test_a_new_incognito_chat_and_its_switch_off_and_on(service):
    from row_bot import threads

    with _client(service) as client:
        _, headers = bootstrap(client)
        everyday = _command(client, headers, "conversation.create", {"title": "Everyday"}).json()["conversation_id"]
        created = _command(client, headers, "conversation.create", {"title": "Private", "incognito": True})
        assert created.status_code == 200, created.text
        private = created.json()["conversation_id"]

        assert _view(client, headers, everyday)["incognito"] is False
        view = _view(client, headers, private)
        assert view["incognito"] is True

        off = _command(client, headers, "conversation.incognito", {"incognito": False}, target=private,
                       revision=view["revision"])
        assert off.status_code == 200, off.text
        view = _view(client, headers, private)
        assert view["incognito"] is False and view["revision"] == off.json()["revision"]
        assert threads.incognito_state(private).boundary is not None

        on = _command(client, headers, "conversation.incognito", {"incognito": True}, target=private,
                      revision=view["revision"])
        assert on.status_code == 200, on.text
        assert _view(client, headers, private)["incognito"] is True


def test_turning_incognito_off_waits_for_the_reply_under_way(service, monkeypatch):
    from row_bot import threads

    with _client(service) as client:
        _, headers = bootstrap(client)
        private = _command(client, headers, "conversation.create",
                           {"title": "Private", "incognito": True}).json()["conversation_id"]
        revision = _view(client, headers, private)["revision"]

        with monkeypatch.context() as replying:
            replying.setattr(service.registry, "active", lambda conversation_id: (object(),))
            refused = _command(client, headers, "conversation.incognito", {"incognito": False}, target=private,
                               revision=revision)

        assert refused.status_code == 409 and refused.json()["code"] == "generation_active"
        assert threads.incognito_state(private).incognito is True
