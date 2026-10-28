"""A0: the launcher's one-time sign-in for the window and browser it opens.

Only the launcher holds the launch secret, so only the window or browser it opens can
become a person on this computer; a program Row-Bot starts stays view-only.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from row_bot.access.config import AccessConfig
from row_bot.access.middleware import AccessMiddleware
from row_bot.access.routes import register_access_routes
from row_bot.access.service import AccessService
from row_bot.access.store import AccessStore

ORIGIN = "http://127.0.0.1:8080"
INVITE = {"layout": "desktop", "session_lifetime": "trusted", "origin": ORIGIN}


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    app_module = importlib.import_module("row_bot.app")
    config = AccessConfig.build(deployment_mode="desktop", allowed_hosts=("127.0.0.1",))
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    app = FastAPI()
    registration = register_access_routes(app, service=service, config=config)
    app.add_route("/api/launcher/sign-in", app_module._launcher_sign_in_handler, methods=["POST"])
    app.add_middleware(AccessMiddleware, config=config, session_authenticator=registration.authenticator)
    monkeypatch.setattr(app_module, "_access_registration", registration)
    secret = "s" * 43
    monkeypatch.setenv(app_module.LAUNCH_SECRET_ENV, secret)
    return app, secret


def _client(app, port: int = 51000) -> TestClient:
    return TestClient(app, base_url=ORIGIN, client=("127.0.0.1", port), follow_redirects=False)


def test_only_the_launchers_secret_gets_a_sign_in(desktop) -> None:
    app, secret = desktop
    client = _client(app)

    assert client.post("/api/launcher/sign-in").status_code == 403
    assert client.post("/api/launcher/sign-in", headers={"Authorization": "Bearer wrong"}).status_code == 403
    answer = client.post("/api/launcher/sign-in", headers={"Authorization": f"Bearer {secret}"})

    assert answer.status_code == 200
    assert answer.json()["invitation"].startswith("rbi_")


def test_the_window_that_claims_it_may_act_and_another_program_may_not(desktop) -> None:
    app, secret = desktop
    invitation = _client(app).post(
        "/api/launcher/sign-in", headers={"Authorization": f"Bearer {secret}"}
    ).json()["invitation"]
    window, program = _client(app, 51001), _client(app, 51002)

    claimed = window.post(
        "/api/access/invitations/claim",
        json={"invitation": invitation, "display_name": "Row-Bot window"},
        headers={"origin": ORIGIN, "accept": "application/json"},
    )

    assert claimed.status_code == 200
    assert window.post("/api/access/invitations", json=INVITE, headers={"origin": ORIGIN}).status_code == 201
    assert program.post("/api/access/invitations", json=INVITE, headers={"origin": ORIGIN}).status_code == 403
    # The window is this computer, not a device to manage in Settings › Devices.
    assert window.get("/api/access/devices").json()["devices"] == []
