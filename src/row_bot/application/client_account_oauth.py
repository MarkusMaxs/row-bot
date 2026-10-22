"""Explicit Google and X account setup over existing OAuth owners.

Any signed-in device may import credentials, check, disconnect and sign in.
The desktop app on this computer signs in through this computer's browser,
which returns to a loopback address here. Another device signs in through
Row-Bot's one approved public HTTPS address: the start returns the provider's
page for that device's browser, and the provider sends it back to
``/api/v1/accounts/{account}/auth/callback``. That route needs no session; a
one-time state (with PKCE, bound to the session that started it, for
``LINK_TTL`` seconds) finds the waiting sign-in. Without exactly one such
address only the desktop app can sign in. Tokens go to the keychain only.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import secrets
from threading import RLock, Thread
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlencode, urlparse
from uuid import UUID

from row_bot import account_tokens
from row_bot.application.client_platform import ClientPlatformError

logger = logging.getLogger(__name__)
_LOCK = RLock()
_JOBS: dict[str, dict[str, Any]] = {}
_ACTIVE: set[str] = set()
_NAMES = {"google": "Google", "x": "X"}
CALLBACK_PATH = "/api/v1/accounts/{account}/auth/callback"
LINK_TTL = 300  # Seconds a sign-in through Row-Bot's public address waits for its callback.
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


@dataclass(frozen=True)
class Device:
    """Where an account action comes from: the desktop app on this computer or
    not, and the approved public origins its request reached Row-Bot through."""

    on_this_computer: bool = True
    public_origins: tuple[str, ...] = ()


def _account(account: str) -> str:
    if account not in _NAMES:
        raise ClientPlatformError("invalid_account_command")
    return account


def public_callback(account: str, public_origins: Iterable[str]) -> str | None:
    """The account's callback at Row-Bot's one approved public HTTPS origin; None without exactly one."""
    from row_bot.access.config import canonical_origin

    try:
        origins = {canonical_origin(value) for value in public_origins if str(value).startswith("https://")}
    except ValueError:
        return None
    return origins.pop() + CALLBACK_PATH.format(account=_account(account)) if len(origins) == 1 else None


def _google_kind(client: dict | None) -> str:
    """A saved Google client's type: ``installed`` (Desktop app), ``web`` (Web application) or ``""``."""
    if isinstance(client, dict):
        for kind in ("web", "installed"):
            if isinstance(client.get(kind), dict):
                return kind
    return ""


def sign_in_route(account: str, device: Device) -> dict[str, str | None]:
    """How a sign-in started from this device works; nothing is contacted.

    ``this_computer``: the desktop app here, through this computer's browser and
    a loopback address (X, or a Google Desktop app client). ``link``: this
    device's browser, returning to Row-Bot's public address (X, or a Google Web
    application client that lists it). ``unavailable`` names its blocker.
    ``callback_url`` is the public address the provider must list, when one applies.
    """
    callback = public_callback(account, device.public_origins)
    client = account_tokens.google_client() if account == "google" else None
    kind = _google_kind(client)
    redirects = client["web"].get("redirect_uris") if kind == "web" else None
    if device.on_this_computer and kind != "web":
        sign_in, blocker = "this_computer", None
    elif callback is None:
        sign_in, blocker = "unavailable", "public_address_required"
    elif kind == "installed":
        sign_in, blocker = "unavailable", "desktop_client"  # Google allows a Desktop app client loopback addresses only.
    elif kind == "web" and not (isinstance(redirects, list) and callback in redirects):
        sign_in, blocker = "unavailable", "redirect_not_listed"
    else:
        sign_in, blocker = "link", None
    return {"sign_in": sign_in, "sign_in_blocker": blocker,
            "callback_url": callback if sign_in != "this_computer" else None}


def sign_in_routes(device: Device) -> dict[str, dict[str, str | None]]:
    """Google's and X's sign-in routes for this device (Settings › Accounts)."""
    return {account: sign_in_route(account, device) for account in _NAMES}


def read_account_auth(*, account: str, device: Device = Device()) -> dict[str, Any]:
    """Read the keychain's saved sign-in and remembered checks only; no provider or refresh call."""
    _account(account)
    if account == "google":
        configured = account_tokens.google_client() is not None
    else:
        from row_bot.api_keys import get_key
        configured = bool(get_key("X_CLIENT_ID") and get_key("X_CLIENT_SECRET"))
    signed_in = account_tokens.digest(account)
    state = "not_configured" if not configured else account_tokens.state(account)
    revision = hashlib.sha256(json.dumps({
        "account": account, "configured": configured, "token": signed_in,
        "client": account_tokens.digest("google_client") if account == "google" else None,
    }, sort_keys=True).encode()).hexdigest()
    route = sign_in_route(account, device)  # Differs by device, so the revision leaves it out.
    return {
        "schema_version": 1, "account": account, "revision": revision,
        "configured": configured, "state": state,
        "token_files": int(bool(signed_in)),  # Saved sign-ins (in the keychain); the name predates that.
        "sign_in": route["sign_in"], "sign_in_blocker": route["sign_in_blocker"],
    }


def _install_google_credentials(raw: str, device: Device) -> None:
    if not raw or len(raw.encode("utf-8")) > 65536:
        raise ClientPlatformError("account_credentials_invalid")
    try:
        parsed = json.loads(raw)
        kinds = [kind for kind in ("installed", "web") if kind in parsed]
        if len(kinds) != 1:
            raise ValueError
        kind = kinds[0]
        client = parsed[kind]
        if not isinstance(client, dict) or any(not isinstance(client.get(key), str) or not client[key] for key in (
            "client_id", "client_secret", "auth_uri", "token_uri",
        )):
            raise ValueError
        auth = urlparse(client["auth_uri"])
        token = urlparse(client["token_uri"])
        # Google's own hosts only; older client files name the token endpoint on accounts.google.com. No
        # backslash, user or port: parsers disagree about where such an address goes.
        if (auth.scheme != "https" or auth.hostname != "accounts.google.com" or token.scheme != "https"
                or token.hostname not in {"oauth2.googleapis.com", "accounts.google.com"}
                or any(ch in client[key] for key in ("auth_uri", "token_uri") for ch in ("\\", "@"))
                or any(part.port is not None or part.username is not None for part in (auth, token))):
            raise ValueError
        # What is saved names Google's current endpoints, whatever the file spelled.
        client.update(auth_uri=GOOGLE_AUTH_URI, token_uri=GOOGLE_TOKEN_URI)
        redirects = client.get("redirect_uris")
        if kind == "installed" and (not isinstance(redirects, list) or not any(
            isinstance(value, str) and value in {"http://localhost", "http://127.0.0.1"}
            for value in redirects
        )):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ClientPlatformError("account_credentials_invalid") from None
    if kind == "web":
        # A Web application client returns only to the addresses it lists: Row-Bot's public one must be there.
        callback = public_callback("google", device.public_origins)
        if callback is None:
            raise ClientPlatformError("account_callback_unavailable")
        if not isinstance(redirects, list) or callback not in redirects:
            raise ClientPlatformError("account_redirect_mismatch")
    try:
        account_tokens.write("google_client", parsed)  # The client's secret too: keychain only.
    except account_tokens.AccountTokenError:
        raise ClientPlatformError("account_secure_storage_unavailable") from None
    from row_bot.tools import registry
    registry.set_tool_config("gmail", "credentials_path", "")  # Saved by Row-Bot now, not a file of the person's.
    registry.set_tool_config("calendar", "credentials_path", "")


def _google_flow() -> bytes:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from row_bot.tools.gmail_tool import GMAIL_SCOPES
    from row_bot.tools.calendar_tool import CALENDAR_SCOPES

    client = account_tokens.google_client()
    if client is None:
        raise ClientPlatformError("account_credentials_required")
    flow = InstalledAppFlow.from_client_config(client, GMAIL_SCOPES + CALENDAR_SCOPES)
    credentials = flow.run_local_server(host="127.0.0.1", port=0, timeout_seconds=180)
    return credentials.to_json().encode()


def _x_flow() -> bytes:
    from row_bot.api_keys import get_key
    from row_bot.tools.x_tool import _run_oauth_flow

    token = _run_oauth_flow(get_key("X_CLIENT_ID"), get_key("X_CLIENT_SECRET"), persist=False)
    return json.dumps(token, separators=(",", ":")).encode()


def _client(account: str) -> dict[str, str]:
    """The OAuth client a sign-in through the public address uses: Google's Web application client or X's app."""
    if account == "google":
        web = (account_tokens.google_client() or {}).get("web") or {}
        return {"client_id": str(web.get("client_id") or ""), "client_secret": str(web.get("client_secret") or "")}
    from row_bot.api_keys import get_key
    return {"client_id": get_key("X_CLIENT_ID") or "", "client_secret": get_key("X_CLIENT_SECRET") or ""}


def _token_client() -> Any:
    import httpx
    return httpx.Client(timeout=15, follow_redirects=False)


def _link(account: str, callback: str) -> tuple[dict[str, Any], str]:
    """A sign-in for this device's browser: what its callback needs, and the provider's page."""
    client = _client(account)
    if not client["client_id"] or not client["client_secret"]:
        raise ClientPlatformError("account_credentials_required")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)
    if account == "google":
        url = GOOGLE_AUTH_URI + "?" + urlencode({
            "response_type": "code", "client_id": client["client_id"], "redirect_uri": callback,
            "scope": " ".join(account_tokens.GOOGLE_SCOPES), "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent",  # A refresh token every time: the sign-in lasts.
        })
    else:
        from row_bot.tools.x_tool import _authorization_url
        url = _authorization_url(client["client_id"], callback, state, challenge)
    return {"state": state, "verifier": verifier, "redirect_uri": callback, "client": client,
            "created": time.monotonic(), "used": False}, url


def _google_tokens(link: dict[str, Any], code: str, http: Any) -> dict[str, Any]:
    from google.oauth2.credentials import Credentials

    client = link["client"]
    response = http.post(GOOGLE_TOKEN_URI, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": link["redirect_uri"],
        "client_id": client["client_id"], "client_secret": client["client_secret"], "code_verifier": link["verifier"],
    }, headers={"Accept": "application/json"})
    response.raise_for_status()
    payload = response.json()
    granted = str(payload.get("scope") or " ".join(account_tokens.GOOGLE_SCOPES)).split()
    # Gmail's and Calendar's access both, and a refresh token: what one lasting sign-in needs.
    if (not payload.get("access_token") or not payload.get("refresh_token")
            or not set(account_tokens.GOOGLE_SCOPES) <= set(granted)):
        raise ValueError("incomplete Google grant")
    expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=int(payload.get("expires_in") or 3600))
    credentials = Credentials(token=payload["access_token"], refresh_token=payload["refresh_token"],
                              token_uri=GOOGLE_TOKEN_URI, client_id=client["client_id"],
                              client_secret=client["client_secret"], scopes=granted, expiry=expiry)
    return json.loads(credentials.to_json())  # The shape the desktop sign-in saves.


def _x_tokens(link: dict[str, Any], code: str, http: Any) -> dict[str, Any]:
    from row_bot.tools.x_tool import _exchange_code

    client = link["client"]
    return _exchange_code(client["client_id"], client["client_secret"], code, link["redirect_uri"],
                          link["verifier"], http=http)


def _receipt(command_id: str, account: str, action: str, phase: str, message: str, *,
             device: Device = Device(), authorization_url: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1, "command_id": command_id, "account": account,
        "action": action, "phase": phase, "message": message[:256],
        "snapshot": read_account_auth(account=account, device=device),
        "authorization_url": authorization_url,
    }


def _expire() -> None:
    """Settle each sign-in through the public address whose callback never came (holding ``_LOCK``)."""
    now = time.monotonic()
    for job in _JOBS.values():
        link = job.get("link")
        if (link and not link["used"] and job["receipt"] and job["receipt"]["phase"] == "running"
                and now - link["created"] >= LINK_TTL):
            link["used"] = True
            try:
                job["receipt"] = _receipt(job["command_id"], job["account"], "start", "failed",
                                          "Sign-in expired. Existing tokens were kept. Start it again from this account.",
                                          device=job["device"])
            finally:
                _ACTIVE.discard(job["account"])


# What an account's setup reviews (Google's: "Gmail and Calendar. Sending and changes always ask
# first."): its first sign-in turns that access on, so the account reads Connected, not Off (F29).
# A sign-in that replaces one keeps the switches as the person left them.
_REVIEWED_ACCESS = {"google": ("gmail", "calendar")}


def _save_sign_in(account: str, token: dict) -> None:
    first = account_tokens.read(account) is None
    account_tokens.write(account, token)  # Keychain only; a failure keeps the old sign-in.
    account_tokens.record_check(account, "valid")  # The provider has just issued it.
    if not first:
        return
    from row_bot.tools import registry
    for tool in _REVIEWED_ACCESS.get(account, ()):
        try:
            registry.set_enabled(tool, True)
        except Exception:
            logger.warning("Couldn't turn on %s after signing in to %s", tool, account, exc_info=True)


def _finish_auth(job_key: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_key)
    if not job:
        return
    account = job["account"]
    command_id = job["command_id"]
    try:
        content = _google_flow() if account == "google" else _x_flow()
        with _LOCK:
            cancelled = job["cancelled"]
            if not cancelled:
                job["validate"]()
                _save_sign_in(account, json.loads(content))
        phase = "cancelled" if cancelled else "completed"
        message = "Authentication cancelled." if cancelled else "Account authorization saved."
    except Exception:
        with _LOCK:
            cancelled = job["cancelled"]
        phase = "cancelled" if cancelled else "failed"
        message = "Authentication cancelled." if cancelled else "Authentication failed. Existing tokens were kept. Retry from this account."
    with _LOCK:
        try:
            job["receipt"] = _receipt(command_id, account, "start", phase, message, device=job["device"])
        finally:
            _ACTIVE.discard(account)


def execute_account_auth(
    *, owner_id: str, command_id: str, account: str, action: str,
    expected_revision: str, confirmed: bool = False, credentials_json: str = "",
    validate: Callable[[], None] = lambda: None, device: Device = Device(),
) -> dict[str, Any]:
    """Accept an exact one-shot account action; OAuth runs in a background job or waits for its callback."""
    try:
        if str(UUID(command_id)) != command_id:
            raise ValueError
    except ValueError:
        raise ClientPlatformError("invalid_account_command") from None
    if action not in {"import_credentials", "check", "start", "disconnect"} or account not in _NAMES:
        raise ClientPlatformError("invalid_account_command")
    key = f"{owner_id}:{command_id}"
    with _LOCK:
        _expire()
        prior = _JOBS.get(key)
        if prior:
            if prior["account"] != account or prior["action"] != action or prior["expected_revision"] != expected_revision:
                raise ClientPlatformError("account_command_conflict")
            return prior["receipt"]
        if account in _ACTIVE:
            raise ClientPlatformError("account_busy")
        current = read_account_auth(account=account, device=device)
        if current["revision"] != expected_revision:
            raise ClientPlatformError("account_changed")
        if action == "start" and not current["configured"]:
            raise ClientPlatformError("account_credentials_required")
        if action == "start" and current["sign_in"] == "unavailable":
            raise ClientPlatformError("account_callback_unavailable" if current["sign_in_blocker"] == "public_address_required"
                                      else "account_redirect_mismatch")
        if action == "disconnect" and not confirmed:
            raise ClientPlatformError("account_confirmation_required")
        if action == "import_credentials" and account != "google":
            raise ClientPlatformError("invalid_account_command")
        _ACTIVE.add(account)
        if len(_JOBS) >= 64:
            finished = next((item for item, value in _JOBS.items() if value["receipt"] and value["receipt"]["phase"] not in {"running", "cancel_requested"}), None)
            if finished:
                _JOBS.pop(finished)
        job = {"owner_id": owner_id, "command_id": command_id, "account": account, "action": action,
               "expected_revision": expected_revision, "cancelled": False, "receipt": None,
               "validate": validate, "device": device}
        _JOBS[key] = job
    try:
        if action == "start" and current["sign_in"] == "link":
            link, url = _link(account, public_callback(account, device.public_origins))
            with _LOCK:
                job["link"] = link
                job["receipt"] = _receipt(command_id, account, action, "running",
                                          f"Waiting for {_NAMES[account]} to send the browser back to Row-Bot.",
                                          device=device, authorization_url=url)
            return job["receipt"]
        if action == "start":
            job["receipt"] = _receipt(command_id, account, action, "running", "Complete sign-in in the browser on this computer.",
                                      device=device)
            Thread(target=_finish_auth, args=(key,), daemon=True, name=f"row-bot-{account}-auth").start()
            return job["receipt"]
        if action == "import_credentials":
            validate()
            _install_google_credentials(credentials_json, device)
            phase, message = "completed", "Google credentials saved locally."
        elif action == "disconnect":
            validate()
            account_tokens.delete(account)
            phase, message = "completed", "Local account tokens removed. Provider authorization may still exist."
        else:
            validate()
            # Each check remembers its verdict for Settings › Accounts.
            if account == "google":
                statuses = [account_tokens.check_google()[0]]  # Gmail and Calendar share one sign-in.
            else:
                from row_bot.tools.x_tool import XTool
                statuses = [XTool().check_token_health()[0]]
            if all(status in {"valid", "refreshed"} for status in statuses):
                phase, message = "completed", "Account token is healthy."
            elif any(status in {"expired", "missing"} for status in statuses):
                phase, message = "failed", "Account token needs authentication. Reconnect this account."
            else:
                phase, message = "failed", "The account token could not be checked. Try again later."
        job["receipt"] = _receipt(command_id, account, action, phase, message, device=device)
        return job["receipt"]
    except Exception:
        with _LOCK:
            _JOBS.pop(key, None)
            _ACTIVE.discard(account)
        raise
    finally:
        if action != "start":
            with _LOCK:
                _ACTIVE.discard(account)


def accept_callback(*, account: str, state: str, code: str = "", error: str = "") -> str:
    """Finish a sign-in through Row-Bot's public address: the provider sent a browser back here, with no session.

    Only the one-time state of a waiting sign-in is accepted, once, before it
    expires and while the session that started it is still signed in. Returns
    the sign-in's outcome (``completed``, ``failed`` or ``cancelled``).
    """
    if account not in _NAMES or not 20 <= len(state) <= 512 or len(code) > 4096 or len(error) > 256:
        raise ClientPlatformError("account_callback_invalid")
    with _LOCK:
        _expire()
        job = next((job for job in _JOBS.values() if job.get("link") and job["account"] == account
                    and hmac.compare_digest(job["link"]["state"].encode(), state.encode())), None)
        if job is None or job["link"]["used"] or job["receipt"]["phase"] != "running":
            raise ClientPlatformError("account_callback_invalid")
        job["link"]["used"] = True  # One callback per state, whatever it brings.
        link = job["link"]
    name = _NAMES[account]
    phase, message = "failed", f"Signing in to {name} didn't finish. Existing tokens were kept. Retry from this account."
    try:
        job["validate"]()  # The session that started it is still signed in.
        if error or not code:
            message = f"{name} didn't allow the sign-in. Existing tokens were kept."
            raise PermissionError(error or "no code")
        with _token_client() as http:
            token = _google_tokens(link, code, http) if account == "google" else _x_tokens(link, code, http)
        with _LOCK:
            if job["cancelled"]:
                raise InterruptedError
            if _client(account) != link["client"]:
                raise ValueError("the client changed during the sign-in")
            job["validate"]()
            _save_sign_in(account, token)
        phase, message = "completed", "Account authorization saved."
    except Exception as exc:
        logger.info("A %s sign-in through Row-Bot's public address did not finish (%s)", name, type(exc).__name__)
    with _LOCK:
        if job["cancelled"] and phase != "completed":
            phase, message = "cancelled", "Authentication cancelled."
        job["link"] = {"state": link["state"], "used": True}  # Its verifier and client are no longer needed.
        try:
            job["receipt"] = _receipt(job["command_id"], account, "start", phase, message, device=job["device"])
        finally:
            _ACTIVE.discard(account)
    return phase


def read_account_auth_receipt(*, owner_id: str, command_id: str) -> dict[str, Any]:
    with _LOCK:
        _expire()
        job = _JOBS.get(f"{owner_id}:{command_id}")
        if job and job["receipt"]:
            return job["receipt"]
    raise ClientPlatformError("account_receipt_missing")


def cancel_account_auth(*, owner_id: str, command_id: str) -> dict[str, Any]:
    with _LOCK:
        _expire()
        job = _JOBS.get(f"{owner_id}:{command_id}")
        if not job or job["action"] != "start" or not job["receipt"]:
            raise ClientPlatformError("account_receipt_missing")
        if job["receipt"]["phase"] == "running":
            job["cancelled"] = True
            link = job.get("link")
            if link and not link["used"]:
                # Nothing runs until the callback, which can no longer use the state.
                link["used"] = True
                job["receipt"] = _receipt(command_id, job["account"], "start", "cancelled", "Authentication cancelled.",
                                          device=job["device"])
                _ACTIVE.discard(job["account"])
            else:
                job["receipt"] = _receipt(command_id, job["account"], "start", "cancel_requested", "Sign-in cancellation requested; the callback window may remain until it times out.",
                                          device=job["device"])
        return job["receipt"]
