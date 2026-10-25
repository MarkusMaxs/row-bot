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


def _real_save_card(thread: str) -> dict:
    """The card the real save_memory tool asks in *thread*, as a turn's tool call asks it."""
    from typing import TypedDict

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from row_bot.tools import memory_tool

    class State(TypedDict):
        result: str

    builder = StateGraph(State)
    builder.add_node("save", lambda _state: {"result": memory_tool._save_memory(
        "preference", "Coffee", "I take it black, no sugar.")})
    builder.add_edge(START, "save")
    builder.add_edge("save", END)
    [asked] = builder.compile(checkpointer=InMemorySaver()).invoke(
        {"result": ""}, {"configurable": {"thread_id": thread}})["__interrupt__"]
    return dict(asked.value)


def test_the_save_card_reaches_the_app_with_its_question_and_exact_text(service):
    from uuid import uuid4

    answers: list[bool] = []

    def stream(_text, _enabled, config, *, stop_event=None):
        yield "interrupt", [{"__interrupt_id": "save-card",
                             **_real_save_card(str(config["configurable"]["thread_id"]))}]

    def resume(_enabled, _config, approved, *, interrupt_ids=None, stop_event=None):
        answers.append(approved)
        yield "done", "Saved."

    service.stream_factory, service.resume_factory = stream, resume
    with _client(service) as client:
        _, headers = bootstrap(client)
        private = _command(client, headers, "conversation.create",
                           {"title": "Private", "incognito": True}).json()["conversation_id"]
        submitted = _command(client, headers, "conversation.submit", {
            "submission_id": str(uuid4()), "text": "Remember how I take my coffee",
            "model_selection": {"provider_id": "fixture", "model_ref": "fixture::model"}},
            target=private, revision=_view(client, headers, private)["revision"])
        assert submitted.status_code == 202, submitted.text
        handle = service.registry.get(submitted.json()["execution_id"])
        assert handle.producer_done.wait(5)

        view = client.get(f"/api/v1/approvals/{handle.approval_id}", headers=headers)

        assert view.status_code == 200, view.text
        card = view.json()
        assert card["question"] == "Save to memory? This chat is incognito."
        assert card["memory_text"] == "Coffee: I take it black, no sugar."
        assert card["repeatable"] is False and not card.get("reviewer_reason")
        resolved = client.post(f"/api/v1/approvals/{handle.approval_id}/commands",
                               headers={**headers, "Idempotency-Key": str(uuid4())}, json={
                                   "command_id": str(uuid4()), "client_session_id": headers["X-Client-Session"],
                                   "type": "approval.resolve", "expected_revision": card["revision"],
                                   "payload": {"decision": "approve", "nonce": card["nonce"]}})
        assert resolved.status_code == 202, resolved.text
        assert service.registry.get(resolved.json()["execution_id"]).producer_done.wait(5)
    assert answers == [True]
