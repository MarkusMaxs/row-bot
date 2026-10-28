"""Sign a test client in the way the launcher signs in Row-Bot's window (A0)."""

from __future__ import annotations

from starlette.testclient import TestClient

from row_bot.access.request_context import SessionIdentity


def person_window(scope, provenance) -> SessionIdentity:
    """The person's own Row-Bot window: every request carries its sign-in (A0).

    The session authenticator for a test app that stands for the person at their desktop.
    Without one, a request from this computer is a bare connection that may only look.
    """
    return SessionIdentity("window-device", "window-session")


def sign_in_this_computer(client: TestClient, service, registration, *, origin: str) -> str:
    """Claim a one-time invitation for ``client``, as the window the launcher opens does.

    On this computer only a signed-in window or browser may change anything. Returns the
    new access session's id.
    """
    created = service.create_invitation(intended_origin=origin, access_route="this_computer")
    claimed = service.claim_invitation(created.token, intended_origin=origin, display_name="Row-Bot window")
    client.cookies.set(registration.cookies.names.http, claimed.session_token)
    return claimed.session.id
