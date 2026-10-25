"""Outlook through the person's own Microsoft app (N10): a public client (no secret) signs in with PKCE on this
computer or with Microsoft's device code from another device; only the refresh token is kept, in the keychain,
and it never reaches a result, a log or the client. Microsoft's sign-in and Graph are fakes on loopback."""

from __future__ import annotations

import json
import logging
from uuid import uuid4

import pytest

from row_bot import account_tokens, microsoft_account, secret_store
from row_bot.application import client_account_oauth as owner
from row_bot.application.client_account_oauth import Device
from row_bot.application.client_platform import ClientPlatformError
from tests.helpers.fake_microsoft import ADDRESS, CLIENT_ID, browser, fake_microsoft
from tests.subsystem.plugins.conftest import MemoryKeyring

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]
PHONE = Device(on_this_computer=False)


@pytest.fixture
def microsoft(tmp_path, monkeypatch):
    """An isolated profile and keychain, and Microsoft faked on loopback."""
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "profile"))
    secret_store._set_backend_for_tests(MemoryKeyring())
    monkeypatch.setattr(owner, "_pause", lambda job, seconds: None)  # No real waits between asks for a code.
    try:
        with fake_microsoft(monkeypatch) as fake:
            yield fake
    finally:
        with owner._LOCK:
            owner._JOBS.clear()
            owner._ACTIVE.clear()
        secret_store._set_backend_for_tests(None)


class ImmediateThread:
    def __init__(self, *, target, args, **_kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def _queued(into):
    class Queued:
        def __init__(self, *, target, args, **_kwargs):
            into.append((target, args))

        def start(self):
            return None
    return Queued


def _send(action, device=Device(), **extra):
    snapshot = owner.read_account_auth(account="microsoft", device=device)
    return owner.execute_account_auth(owner_id="local", command_id=str(uuid4()), account="microsoft",
                                      expected_revision=snapshot["revision"], action=action, device=device, **extra)


def _register(tenant=""):
    return _send("import_credentials", credentials_json=json.dumps({"client_id": CLIENT_ID, "tenant": tenant}))


def _tokens_in(text: str, fake) -> list[str]:
    return [token for token in (fake.access_token, fake.refresh_token, "refresh-1", "access-1", "device-code-1")
            if token in text]


@pytest.mark.parametrize(("given", "accepted"), [
    ({"client_id": CLIENT_ID}, True),
    ({"client_id": CLIENT_ID, "tenant": "organizations"}, True),
    ({"client_id": CLIENT_ID, "tenant": "yourdomain.onmicrosoft.com"}, True),
    ({"client_id": CLIENT_ID, "tenant": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}, True),
    ({"client_id": "Row-Bot"}, False),
    ({"client_id": CLIENT_ID, "tenant": "common/../evil"}, False),  # A tenant can't change where sign-in goes.
    ({"client_id": CLIENT_ID, "client_secret": "secret"}, False),  # A public client: no secret is ever kept.
])
def test_the_app_registration_is_checked_and_kept_in_the_keychain(microsoft, given, accepted):
    if not accepted:
        with pytest.raises(ClientPlatformError, match="account_microsoft_app_invalid"):
            _send("import_credentials", credentials_json=json.dumps(given))
        assert owner.read_account_auth(account="microsoft")["configured"] is False
        return
    saved = _send("import_credentials", credentials_json=json.dumps(given))
    assert saved["phase"] == "completed" and saved["snapshot"]["configured"] is True
    assert saved["snapshot"]["state"] == "not_authenticated"
    assert microsoft_account.client() == {"client_id": CLIENT_ID, "tenant": given.get("tenant") or "common"}
    assert microsoft.requests == []  # Saving it contacts nobody.


def test_signing_in_on_this_computer_uses_pkce_and_a_loopback_address_and_keeps_only_the_refresh_token(
        microsoft, monkeypatch, caplog):
    from row_bot.tools import registry

    caplog.set_level(logging.DEBUG)
    _register()
    monkeypatch.setattr(owner, "Thread", ImmediateThread)
    monkeypatch.setattr(owner, "_open_browser", browser(microsoft))
    registry.set_enabled("outlook_mail", False)
    registry.set_enabled("outlook_calendar", False)
    receipt = _send("start")
    assert receipt["phase"] == "completed", receipt
    assert receipt["snapshot"]["state"] == "connected" and receipt["snapshot"]["address"] == ADDRESS
    (authorize,) = microsoft.seen("GET", "/common/oauth2/v2.0/authorize")
    assert authorize.query["code_challenge_method"] == "S256" and authorize.query["redirect_uri"].startswith("http://localhost:")
    assert set(authorize.query["scope"].split()) == set(microsoft_account.SCOPES)
    (exchange,) = microsoft.seen("POST", "/common/oauth2/v2.0/token")
    assert exchange.form["grant_type"] == "authorization_code" and exchange.form["code_verifier"]
    assert "client_secret" not in exchange.form  # A public client.
    saved = account_tokens.read("microsoft")
    assert saved["refresh_token"] == microsoft.refresh_token and "access_token" not in saved
    assert (saved["address"], saved["client_id"]) == (ADDRESS, CLIENT_ID)
    # Its first sign-in turns on what Outlook's setup reviewed (sending and changes still ask first).
    assert registry.is_enabled("outlook_mail") and registry.is_enabled("outlook_calendar")
    assert not _tokens_in(json.dumps(receipt) + caplog.text, microsoft)


def test_the_loopback_accepts_only_this_sign_ins_own_state(microsoft, monkeypatch):
    import httpx

    _register()
    monkeypatch.setattr(owner, "Thread", ImmediateThread)
    refused = []
    real = browser(microsoft)

    def forged_then_real(url):
        from urllib.parse import parse_qs, urlparse
        back = parse_qs(urlparse(url).query)["redirect_uri"][0]
        refused.append(httpx.get(back + "/?code=forged&state=not-this-one", timeout=5).status_code)
        real(url)
    monkeypatch.setattr(owner, "_open_browser", forged_then_real)
    receipt = _send("start")
    assert refused == [400] and receipt["phase"] == "completed"
    (exchange,) = microsoft.seen("POST", "/common/oauth2/v2.0/token")
    assert exchange.form["code"] != "forged"


def test_another_device_signs_in_with_microsofts_code(microsoft, monkeypatch):
    _register()
    queued = []
    monkeypatch.setattr(owner, "Thread", _queued(queued))
    snapshot = owner.read_account_auth(account="microsoft", device=PHONE)
    assert (snapshot["sign_in"], snapshot["sign_in_blocker"]) == ("device", None)  # No public address needed.
    waiting = _send("start", device=PHONE)
    assert waiting["phase"] == "running"
    assert waiting["device_code"] == {"user_code": "ABCD-EFGH", "verification_uri": "https://microsoft.com/devicelogin",
                                      "verification_uri_complete": ""}
    assert 0 < waiting["expires_in"] <= 900 and "device-code-1" not in json.dumps(waiting)
    (asked,) = microsoft.seen("POST", "/common/oauth2/v2.0/devicecode")
    assert set(asked.form["scope"].split()) == set(microsoft_account.SCOPES) and "client_secret" not in asked.form
    target, args = queued[0]
    target(*args)  # Pending once, then the person allowed it.
    done = owner.read_account_auth_receipt(owner_id="local", command_id=waiting["command_id"])
    assert done["phase"] == "completed" and done["snapshot"]["state"] == "connected"
    assert [request.form["grant_type"] for request in microsoft.seen("POST", "/common/oauth2/v2.0/token")] == [
        microsoft_account.DEVICE_GRANT] * 2
    assert account_tokens.read("microsoft")["refresh_token"] == microsoft.refresh_token


@pytest.mark.parametrize(("error", "says"), [
    ("authorization_declined", "Microsoft didn't allow the sign-in."),
    ("expired_token", "The code ran out before it was used."),
])
def test_a_declined_or_expired_code_says_so_and_keeps_the_sign_in_there_was(microsoft, monkeypatch, error, says):
    _register()
    account_tokens.write("microsoft", {"refresh_token": "kept", "client_id": CLIENT_ID, "tenant": "common",
                                       "address": ADDRESS})
    queued = []
    monkeypatch.setattr(owner, "Thread", _queued(queued))
    microsoft.device_error = error
    waiting = _send("start", device=PHONE)
    queued[0][0](*queued[0][1])
    failed = owner.read_account_auth_receipt(owner_id="local", command_id=waiting["command_id"])
    assert failed["phase"] == "failed" and failed["message"].startswith(says)
    assert account_tokens.read("microsoft")["refresh_token"] == "kept"


def test_a_tenant_that_needs_admin_consent_says_who_can_approve_it(microsoft, monkeypatch):
    _register()
    monkeypatch.setattr(owner, "Thread", ImmediateThread)
    monkeypatch.setattr(owner, "_open_browser", browser(microsoft))
    microsoft.token_error = {"error": "invalid_grant", "error_codes": [65001]}
    receipt = _send("start")
    assert receipt["phase"] == "failed"
    assert "admin" in receipt["message"] and "GoDaddy" in receipt["message"]
    assert account_tokens.read("microsoft") is None


def test_stopping_a_code_sign_in_ends_it_and_a_late_answer_is_never_saved(microsoft, monkeypatch):
    _register()
    queued = []
    monkeypatch.setattr(owner, "Thread", _queued(queued))
    waiting = _send("start", device=PHONE)
    stopped = owner.cancel_account_auth(owner_id="local", command_id=waiting["command_id"])
    assert stopped["phase"] == "cancelled"
    microsoft.pending = 0
    queued[0][0](*queued[0][1])  # The poller wakes after the cancel: it asks nothing more and saves nothing.
    assert account_tokens.read("microsoft") is None
    assert microsoft.seen("POST", "/common/oauth2/v2.0/token") == []
    assert owner.read_account_auth(account="microsoft")["state"] == "not_authenticated"


def _signed_in(fake):
    _register()
    account_tokens.write("microsoft", {"refresh_token": fake.refresh_token, "client_id": CLIENT_ID,
                                       "tenant": "common", "address": ADDRESS, "name": "You"})


def test_a_refresh_replaces_the_refresh_token_in_the_keychain_and_a_revoked_one_needs_you(microsoft, caplog):
    from row_bot.integrations import builtin

    caplog.set_level(logging.DEBUG)
    _signed_in(microsoft)
    assert microsoft_account.graph("GET", "/me")["mail"] == ADDRESS
    assert account_tokens.read("microsoft")["refresh_token"] == microsoft.refresh_token == "refresh-2"
    checked = _send("check")
    assert checked["phase"] == "completed" and checked["snapshot"]["state"] == "connected"
    microsoft.revoked.add(microsoft.refresh_token)
    failed = _send("check")
    assert failed["phase"] == "failed" and failed["snapshot"]["state"] == "invalid"
    builtin.changed()
    row = next(row for row in builtin.rows() if row["id"] == "builtin:account:microsoft")
    assert [blocker["code"] for blocker in row["blockers"]] == ["expired"]
    assert row["next_action"]["label"] == "Sign in again"
    assert not _tokens_in(caplog.text, microsoft)


def test_a_sign_in_from_another_app_registration_needs_a_new_one(microsoft):
    _signed_in(microsoft)
    microsoft_account.save_client("99999999-2222-3333-4444-555555555555")
    assert owner.read_account_auth(account="microsoft")["state"] == "invalid"
    with pytest.raises(microsoft_account.SignInError):
        microsoft_account.access_token()


def test_settings_show_who_is_signed_in_without_contacting_microsoft_or_a_token(microsoft):
    from row_bot.application.settings_snapshot import read_settings_snapshot

    _signed_in(microsoft)
    accounts = read_settings_snapshot()["accounts"]
    for key in ("outlook_mail", "outlook_calendar"):
        assert (accounts[key]["configured"], accounts[key]["address"]) == (True, ADDRESS)
        assert accounts[key]["authentication_state"] == "saved_unchecked"
    assert microsoft.requests == []
    assert not _tokens_in(json.dumps(accounts), microsoft)


def test_disconnecting_forgets_the_sign_in_and_its_access(microsoft):
    _signed_in(microsoft)
    microsoft_account.graph("GET", "/me")
    removed = _send("disconnect", confirmed=True)
    assert removed["phase"] == "completed" and removed["snapshot"]["state"] == "not_authenticated"
    assert account_tokens.read("microsoft") is None and microsoft_account._ACCESS == {}
    with pytest.raises(microsoft_account.SignInError):
        microsoft_account.graph("GET", "/me")
