"""Computer use step routes: the desktop app on this computer only, a no-store picture, an idempotent Stop."""

# ruff: noqa: F401, F811 -- imported pytest fixtures are requested by name.

import base64
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from row_bot.access.config import AccessConfig, DeploymentMode
from row_bot.access.request_context import SessionIdentity
from row_bot.api.v1.routes import create_client_platform_app
from row_bot.computer_use.client import CuaClient
from row_bot.computer_use.readiness import CuaReadiness, ReadinessCode, acknowledge_disclosure
from row_bot.computer_use.service import ComputerUseService
from tests.fixtures.fake_desktop import FakeBar, FakeDesktop
from tests.subsystem.client_protocol.test_protocol_application import (
    _client,
    _command,
    service,
)
from tests.subsystem.client_protocol.test_protocol_security import bootstrap
from tests.subsystem.computer_use.conftest import desktop_apps, desktop_windows

pytestmark = pytest.mark.subsystem


@pytest.fixture
def computer(service, monkeypatch):  # After the app's data folder is set.
    from row_bot.computer_use import service as module

    acknowledge_disclosure()
    monkeypatch.setattr("row_bot.computer_use.readiness.readiness",
                        lambda **_kw: CuaReadiness(ReadinessCode.READY, "ready", executable="cua-driver"))
    desktop = FakeDesktop(windows=desktop_windows(), apps=desktop_apps())
    made = ComputerUseService(
        client_factory=lambda: CuaClient("cua-driver", transport_factory=lambda *_args: desktop),
        bar_factory=FakeBar, stop_turn=lambda _thread: None,
        window_display=desktop.window_display, cursor_position=desktop.cursor_position,
    )
    monkeypatch.setattr(module, "_SERVICE", made)
    yield made
    made.stop()


def _working(computer: ComputerUseService, conversation: str) -> None:
    computer.begin(conversation, "turn-api")
    computer.allow(conversation, "Notepad")
    computer.use("Notepad")


def _conversation(client, headers) -> str:
    created = _command(client, headers, "conversation.create", {"title": "Computer step"})
    assert created.status_code == 200, created.text
    return created.json()["conversation_id"]


def _stop(headers, command_id: str | None = None) -> dict:
    return {"command_id": command_id or str(uuid4()), "client_session_id": headers["X-Client-Session"],
            "type": "computer_use.stop"}


def test_the_local_owner_reads_the_step_and_its_picture_without_caching(service, computer):
    with _client(service) as client:
        _, headers = bootstrap(client)
        conversation = _conversation(client, headers)
        root = f"/api/v1/conversations/{conversation}/computer"

        idle = client.get(root, headers=headers)
        assert idle.status_code == 200, idle.text
        assert (idle.json()["state"], idle.json()["active"]) == ("stopped", False)

        _working(computer, conversation)
        snapshot = client.get(root, headers=headers).json()
        assert (snapshot["state"], snapshot["app"], snapshot["has_picture"]) == ("working", "Notepad", True)

        picture = client.get(root + "/preview", params={"revision": snapshot["revision"]}, headers=headers)
        assert picture.status_code == 200, picture.text
        assert picture.headers["cache-control"] == "no-store"
        assert base64.b64decode(picture.json()["image_base64"]).startswith(b"\xff\xd8\xff")
        assert picture.json()["mime_type"] == "image/jpeg"


def test_stop_is_bound_to_its_session_and_idempotency_key(service, computer):
    with _client(service) as client:
        _, headers = bootstrap(client)
        conversation = _conversation(client, headers)
        root = f"/api/v1/conversations/{conversation}/computer/commands"
        _working(computer, conversation)

        stop = _stop(headers)
        mismatched = client.post(root, headers={**headers, "Idempotency-Key": str(uuid4())}, json=stop)
        assert (mismatched.status_code, mismatched.json()["code"]) == (409, "idempotency_mismatch")
        foreign = {**stop, "client_session_id": str(uuid4())}
        refused = client.post(root, headers={**headers, "Idempotency-Key": stop["command_id"]}, json=foreign)
        assert (refused.status_code, refused.json()["code"]) == (422, "invalid_computer_use_command")
        for retired in ("computer_use.pause", "computer_use.resume", "computer_use.click"):
            unknown = client.post(root, headers={**headers, "Idempotency-Key": stop["command_id"]},
                                  json={**stop, "type": retired})
            assert unknown.status_code == 422
        assert computer.status_snapshot()["active"] is True

        stopped = client.post(root, headers={**headers, "Idempotency-Key": stop["command_id"]}, json=stop)
        assert stopped.status_code == 200, stopped.text
        assert (stopped.json()["status"], stopped.json()["computer_use"]["state"]) == ("completed", "stopped")
        assert computer.status_snapshot()["active"] is False
        replay = client.post(root, headers={**headers, "Idempotency-Key": stop["command_id"]}, json=stop)
        assert replay.json() == stopped.json()


def test_other_devices_can_neither_see_nor_control_this_computer(service, computer):
    app = create_client_platform_app(
        service,
        access_config=AccessConfig(deployment_mode=DeploymentMode.SERVER),
        session_authenticator=lambda _scope, _provenance: SessionIdentity("fixture-device", "fixture-session"),
        choices=lambda: {"models": [], "capabilities": []},
    )
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        _, headers = bootstrap(client)
        conversation = _conversation(client, headers)
        root = f"/api/v1/conversations/{conversation}/computer"
        _working(computer, conversation)

        stop = _stop(headers)
        responses = [
            client.get(root, headers=headers),
            client.get(root + "/preview", params={"revision": "0" * 64}, headers=headers),
            client.post(root + "/commands", headers={**headers, "Idempotency-Key": stop["command_id"]}, json=stop),
        ]
        assert [(response.status_code, response.json()["code"]) for response in responses] == [
            (403, "computer_use_local_only")] * 3
        assert "image_base64" not in "".join(response.text for response in responses)
        assert computer.status_snapshot()["active"] is True
