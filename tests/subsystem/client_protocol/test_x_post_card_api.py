"""An X post's approval through the client API (N7): the card the person reads, then the post it lets out.

The turn pauses on the real x_post card; the approval view carries the account, the whole text and each
picture's name, size and SHA-256 (never a path or a token); approving resumes the turn, and the tool,
run again from the top, posts exactly that to a fake X API. Stores are isolated and the model is a
scripted stream; nothing reaches X.
"""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.helpers.fake_x_api import PERSONAL, ROWBOT, FakeXApi, install
from tests.subsystem.client_protocol.test_protocol_application import service  # noqa: F401
from tests.subsystem.client_protocol.test_protocol_security import bootstrap
from tests.helpers.access import person_window

pytestmark = pytest.mark.subsystem


def _post(client, headers, target, kind, payload, revision="0"):
    identity = str(uuid4())
    return client.post(f"/api/v1/conversations/{target}/commands", headers={**headers, "Idempotency-Key": identity},
                       json={"command_id": identity, "client_session_id": headers["X-Client-Session"], "type": kind,
                             "expected_revision": revision, "payload": payload})


def test_the_approval_view_shows_the_post_and_approving_sends_exactly_it(service, tmp_path, monkeypatch):  # noqa: F811
    from PIL import Image

    from row_bot.api.v1.routes import create_client_platform_app
    from row_bot.tools import approval_gate, x_posting
    from row_bot.tools.x_tool import XTool

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "profile"))
    fake = FakeXApi()
    install(monkeypatch, fake, ROWBOT, PERSONAL)
    for user in fake.users.values():  # Both accounts post long posts (an X subscription).
        user["subscription_type"] = "Premium"
    monkeypatch.setattr(approval_gate, "current_approval_mode", lambda: "ask")
    tool = XTool()
    monkeypatch.setattr(tool, "get_config", lambda key, default=None: default)
    picture = tmp_path / "launch.png"
    Image.new("RGB", (800, 450), "teal").save(picture)
    text = "Row-Bot 5.3: every post names its account.\n\n" + "Details follow. " * 120
    call = {"action": "post", "text": text, "media_paths": [str(picture)], "account": "@rowbot_ai"}
    results: list[str] = []

    class Paused(Exception):
        pass

    def stream(_text, _enabled, _config, *, stop_event):
        card: dict = {}

        def pause(payload):
            card.update(payload)
            raise Paused
        with monkeypatch.context() as patch:
            patch.setattr(x_posting, "_ask", pause)
            with pytest.raises(Paused):
                tool._x_post(**call)
        yield "interrupt", [{"__interrupt_id": "x-card", **card}]

    def resume(_enabled, _config, approved, *, interrupt_ids=None, stop_event=None):
        # The tool runs again from the top, and the person's answer is this card's.
        with monkeypatch.context() as patch:
            patch.setattr(x_posting, "_ask", lambda _payload: approved)
            results.append(tool._x_post(**call))
        yield "done", "Posted."

    service.stream_factory, service.resume_factory = stream, resume
    app = create_client_platform_app(service, choices=lambda: {"models": [], "capabilities": []}, session_authenticator=person_window)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        _, headers = bootstrap(client)
        created = client.post("/api/v1/conversations/commands", headers={**headers, "Idempotency-Key": str(uuid4())},
                              json={"command_id": str(uuid4()), "client_session_id": headers["X-Client-Session"],
                                    "type": "conversation.create", "expected_revision": "0",
                                    "payload": {"title": "Launch post"}})
        conversation = created.json()["conversation_id"]
        submitted = _post(client, headers, conversation, "conversation.submit", {
            "submission_id": str(uuid4()), "text": "Post the launch",
            "model_selection": {"provider_id": "fixture", "model_ref": "fixture::model"}})
        assert submitted.status_code == 202, submitted.text
        handle = service.registry.get(submitted.json()["execution_id"])
        assert handle.producer_done.wait(5)
        view = client.get(f"/api/v1/approvals/{handle.approval_id}", headers=headers)
        assert view.status_code == 200, view.text
        assert fake.posts == []  # Nothing goes out while the card waits.
        resolved = client.post(f"/api/v1/approvals/{handle.approval_id}/commands",
                               headers={**headers, "Idempotency-Key": str(uuid4())}, json={
                                   "command_id": str(uuid4()), "client_session_id": headers["X-Client-Session"],
                                   "type": "approval.resolve", "expected_revision": view.json()["revision"],
                                   "payload": {"decision": "approve", "nonce": view.json()["nonce"]}})
        assert resolved.status_code == 202, resolved.text
        assert service.registry.get(resolved.json()["execution_id"]).producer_done.wait(5)

    card = view.json()
    assert card["question"] == "Post to X as @rowbot_ai?"
    post = card["x_post"]
    assert (post["username"], post["name"], post["account_id"], post["action"]) == ("rowbot_ai", "Row-Bot", "111",
                                                                                    "post")
    assert post["text"] == text  # The whole text: far past the reason's 1,024 characters.
    [media] = post["media"]
    assert (media["name"], media["size"], media["kind"]) == ("launch.png", picture.stat().st_size, "image")
    assert media["sha256"] == hashlib.sha256(picture.read_bytes()).hexdigest()[:12]
    assert media["thumbnail"].startswith("data:image/jpeg;base64,")
    assert str(tmp_path) not in json.dumps(card) and "access-" not in json.dumps(card)
    [result] = results
    [sent] = fake.posted_by("111")
    assert f"Posted as @rowbot_ai: https://x.com/rowbot_ai/status/{sent['id']}" in result
    assert sent["text"].startswith("Row-Bot 5.3: every post names its account.") and fake.posted_by("222") == []
