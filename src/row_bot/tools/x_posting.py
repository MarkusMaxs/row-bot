"""Posting to X as a named account, approved by the person, and sent once (N7).

A post, reply, quote or delete goes out only as the account the call names (``x_accounts``). Its
approval card says exactly what will happen: "Post to X as @rowbot_ai?", the full text, the post it
replies to or quotes as a link, and each photo or video by name, size and a short SHA-256. The
approval is bound to the account, the action, the text, the target and the media's SHA-256s: any
change is another question, asked again. Under Auto only the person approves these (a person-only
risk, like Git identity); Hands-off is the person's own choice.

Before sending, an operation is recorded (its ID, account, payload hash and time). When X doesn't
confirm (a timeout or a dropped connection after sending), the outcome is not known: Row-Bot reads
the account's latest posts and reports the post it finds, or that it didn't post. It never sends
again by itself; an operation still unsettled is checked the same way before the same payload goes
out again.
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import logging
import mimetypes
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from row_bot import x_accounts
from row_bot.data_paths import get_row_bot_data_dir
from row_bot.x_accounts import XAccount

logger = logging.getLogger(__name__)

ACTIONS = ("post", "reply", "quote", "delete")
MEDIA_UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
_CHUNK = 5 * 1024 * 1024
_MAX_IMAGES = 4
_MAX_TEXT = 25_000  # X's longest post (with a subscription).
_THUMBNAIL_PIXELS = 160
_THUMBNAIL_BYTES = 24 * 1024
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
_SINGLE_TYPES = {"image/gif", "video/mp4", "video/quicktime"}
_RECENT = 20  # How many of the account's latest posts a check reads.
_MAX_OPERATIONS = 200
_OPS_LOCK = threading.Lock()
_SENDING = threading.Lock()  # One send at a time, so an operation still "sending" was cut off, never in flight.


class XPostError(ValueError):
    """Something stops the post before anything is sent; the message says what, in plain words."""


@dataclass(frozen=True)
class Media:
    path: Path
    name: str
    size: int
    sha256: str
    mime: str
    kind: str  # image, gif or video
    thumbnail: str = ""  # A small JPEG as a data URL, for the card; "" for a video.

    @property
    def short(self) -> str:
        return self.sha256[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _size(value: int) -> str:
    for unit, step in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if value >= step:
            return f"{value / step:.1f} {unit}".replace(".0 ", " ")
    return f"{value} bytes"


# ── What is sent ─────────────────────────────────────────────────────────────


def _thumbnail(path: Path) -> str:
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as source:
            if source.width * source.height > 64_000_000:
                return ""
            source.draft(None, (_THUMBNAIL_PIXELS * 2, _THUMBNAIL_PIXELS * 2))
            image = ImageOps.exif_transpose(source)
            image.thumbnail((_THUMBNAIL_PIXELS, _THUMBNAIL_PIXELS))
            output = io.BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=70)
    except Exception:
        return ""
    data = output.getvalue()
    return f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}" if len(data) <= _THUMBNAIL_BYTES else ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def media_facts(paths: list[str] | None) -> list[Media]:
    """Each file to attach: its name, size, SHA-256 and kind (with a thumbnail for a picture)."""
    found: list[Media] = []
    for raw in paths or []:
        path = Path(str(raw)).expanduser()
        if not path.is_file():
            raise XPostError(f"{path.name or raw} isn't a file Row-Bot can read, so nothing was posted.")
        mime = mimetypes.guess_type(path.name)[0] or ""
        if mime not in _IMAGE_TYPES | _SINGLE_TYPES:
            raise XPostError(f"X takes JPEG, PNG, WebP or GIF pictures and MP4 or MOV videos; {path.name} isn't "
                             "one, so nothing was posted.")
        kind = "video" if mime.startswith("video/") else "gif" if mime == "image/gif" else "image"
        found.append(Media(path, path.name[:256], path.stat().st_size, _sha256(path), mime, kind,
                           _thumbnail(path) if kind != "video" else ""))
    singles = [item for item in found if item.kind != "image"]
    if len(found) > _MAX_IMAGES or (singles and len(found) > 1):
        raise XPostError("A post takes up to four pictures, or one video or GIF, so nothing was posted.")
    return found


def payload_hash(account: XAccount, action: str, text: str, target: str, media: list[Media]) -> str:
    """What the approval is bound to: the account, action, text, target and each file's SHA-256."""
    from row_bot.tools.approval_gate import call_binding

    return call_binding("x_post", {"account": account.id, "action": action, "text": text, "target": target},
                        [item.sha256 for item in media])


def target_url(target: str) -> str:
    return f"https://x.com/i/status/{target}" if target else ""


def question(account: XAccount, action: str) -> str:
    return {"post": f"Post to X as {account.handle}?", "reply": f"Reply on X as {account.handle}?",
            "quote": f"Quote a post on X as {account.handle}?",
            "delete": f"Delete a post from {account.handle}?"}[action]


def card(account: XAccount, action: str, text: str, target: str, media: list[Media], *,
         target_text: str = "") -> dict[str, Any]:
    """What the person's card shows; never a path or a token."""
    return {
        "account_id": account.id, "username": account.username, "name": account.name, "action": action,
        "text": text, "target_id": target,
        "target_url": account.post_url(target) if action == "delete" and target else target_url(target),
        "target_text": target_text[:_MAX_TEXT],
        "media": [{"name": item.name, "size": item.size, "sha256": item.short, "kind": item.kind,
                   "thumbnail": item.thumbnail} for item in media],
        "limit": _LIMITS.get(account.id, 0),  # How long a post the account may send, when X said (0: unknown).
    }


def _words(account: XAccount, action: str, text: str, target: str, media: list[Media], target_text: str) -> str:
    """The action in one paragraph, for the conversation's activity and a workflow's approval."""
    who = f"{account.handle} ({account.name})" if account.name else account.handle
    lead = {"post": f"Post to X as {who}", "reply": f"Reply as {who} to {target_url(target)}",
            "quote": f"Quote {target_url(target)} as {who}",
            "delete": f"Delete post {target} from {who}"}[action]
    quoted = text if action != "delete" else target_text
    sentence = f"{lead}: “{quoted[:600]}{'…' if len(quoted) > 600 else ''}”" if quoted else lead
    if media:
        sentence += " with " + ", ".join(f"{item.name} ({_size(item.size)}, SHA-256 {item.short})" for item in media)
    return sentence + "."


def approval_payload(account: XAccount, action: str, text: str, target: str, media: list[Media], *,
                     target_text: str = "") -> dict[str, Any]:
    """The approval this post asks for: its card, its words, and the binding (a new question when any part changes)."""
    return {"tool": "x_post", "label": question(account, action).rstrip("?"),
            "description": _words(account, action, text, target, media, target_text),
            "question": question(account, action), "binding": payload_hash(account, action, text, target, media),
            "args": {"account": account.handle, "action": action, "text": text, "tweet_id": target,
                     "media": [item.name for item in media]},
            "x_post": card(account, action, text, target, media, target_text=target_text)}


class PostEdited(Exception):
    """The person edited the post on its card (G2): ask again with this text and the files they kept."""

    def __init__(self, text: str, kept: list[str]) -> None:
        super().__init__("The person edited the post.")
        self.text, self.kept = text, kept


def _edited(answer: Any) -> PostEdited | None:
    edit = answer.get("x_post_edit") if isinstance(answer, dict) else None
    if not isinstance(edit, dict) or not isinstance(edit.get("text"), str):
        return None
    return PostEdited(edit["text"], [str(item) for item in edit.get("media") or []])


def approve(account: XAccount, action: str, text: str, target: str, media: list[Media], *,
            target_text: str = "") -> tuple[str | None, str]:
    """(None, its approval line) when this exact post may go out; else (the refusal, its line).

    Raises PostEdited when the person edited it on the card instead: never an approval of anything.
    """
    from row_bot.tools.approval_gate import X_POST_RISK, current_approval_mode, resolve_approval_line, with_approval

    payload = approval_payload(account, action, text, target, media, target_text=target_text)
    answers: list[Any] = []

    def ask(asked: dict[str, Any]) -> Any:
        answers.append(_ask(asked))
        return answers[-1]
    outcome, line = resolve_approval_line(payload, approval_mode=current_approval_mode(), risks=(X_POST_RISK,),
                                          approval_callback=ask)
    edited = _edited(answers[-1]) if answers and outcome == "deny" else None
    if edited is not None:
        raise edited
    if outcome == "allow":
        return None, line
    if outcome == "block":
        return "BLOCKED: posting on X is unavailable while this chat's approval level is Block.", ""
    return with_approval(line, "Nothing was sent to X: the person didn't approve it. Don't post it another way."), line


def _ask(payload: dict[str, Any]) -> Any:
    """The person's answer (the conversation pauses on the card); a test stands in here."""
    from langgraph.types import interrupt

    return interrupt(payload)


# ── The operation record ─────────────────────────────────────────────────────


def _operations_path() -> Path:
    return get_row_bot_data_dir() / "x" / "operations.json"


def _operations() -> list[dict[str, Any]]:
    try:
        value = json.loads(_operations_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _save_operations(items: list[dict[str, Any]]) -> None:
    path = _operations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(items[-_MAX_OPERATIONS:], indent=1), encoding="utf-8")
    os.replace(temporary, path)


def operations() -> list[dict[str, Any]]:
    """The recorded operations, oldest first (what was sent from which account, and how it ended)."""
    with _OPS_LOCK:
        return _operations()


def _begin(account: XAccount, action: str, digest: str, text: str, target: str) -> dict[str, Any]:
    operation = {"id": str(uuid.uuid4()), "account_id": account.id, "action": action, "payload_hash": digest,
                 "text": text, "target": target, "started_at": _stamp(_now()), "status": "sending"}
    with _OPS_LOCK:
        _save_operations([*_operations(), operation])
    return operation


def _settle(operation: dict[str, Any], status: str, **facts: str) -> None:
    operation.update(status=status, settled_at=_stamp(_now()), **facts)
    with _OPS_LOCK:
        items = _operations()
        for index, item in enumerate(items):
            if item.get("id") == operation["id"]:
                items[index] = operation
        _save_operations(items)


def _unsettled(account: XAccount, digest: str) -> dict[str, Any] | None:
    with _OPS_LOCK:
        return next((item for item in reversed(_operations()) if item.get("account_id") == account.id
                     and item.get("payload_hash") == digest and item.get("status") in {"sending", "unknown"}), None)


# ── Sending ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Outcome:
    status: str  # posted, failed or unknown
    post_id: str = ""
    at: str = ""
    detail: str = ""


def _definite(exc: Exception) -> bool:
    """Whether a failed request certainly never reached X (so nothing can have been posted)."""
    import httpx

    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol))


def _refusal(response: Any, account: XAccount) -> str:
    if response.status_code == 401:
        return f"X turned down the sign-in of {account.handle}. The person can reconnect it in Settings › Apps › X."
    if response.status_code == 429:
        return "X is limiting requests right now. Try again later."
    try:
        body = response.json()
    except ValueError:
        body = {}
    detail = str(body.get("detail") or body.get("title") or "").strip()[:300] if isinstance(body, dict) else ""
    return f"X refused it ({response.status_code}){': ' + detail if detail else ''}."


def _upload(media: list[Media], token: dict) -> list[str]:
    """Upload each file in chunks, checking its bytes are the ones approved; raises XPostError (nothing posted)."""
    ids: list[str] = []
    headers = x_accounts.bearer(token)
    for item in media:
        def fail(step: str) -> XPostError:
            return XPostError(f"Uploading {item.name} to X failed ({step}), so nothing was posted. "
                              "Uploaded media may stay on X unused.")
        try:
            started = x_accounts.send("POST", MEDIA_UPLOAD_URL, headers=headers, timeout=30, data={
                "command": "INIT", "total_bytes": str(item.size), "media_type": item.mime})
            if started.status_code not in (200, 201, 202):
                raise fail("start")
            media_id = str(started.json().get("media_id_string") or "")
            if not media_id:
                raise fail("start")
            digest, sent = hashlib.sha256(), 0
            with item.path.open("rb") as handle:
                segment = 0
                while chunk := handle.read(_CHUNK):
                    digest.update(chunk)
                    sent += len(chunk)
                    appended = x_accounts.send("POST", MEDIA_UPLOAD_URL, headers=headers, timeout=60,
                                               data={"command": "APPEND", "media_id": media_id,
                                                     "segment_index": str(segment)},
                                               files={"media_data": chunk})
                    if appended.status_code not in (200, 202, 204):
                        raise fail("sending")
                    segment += 1
            if digest.hexdigest() != item.sha256 or sent != item.size:
                raise XPostError(f"{item.name} changed after it was approved, so nothing was posted.")
            finished = x_accounts.send("POST", MEDIA_UPLOAD_URL, headers=headers, timeout=30,
                                       data={"command": "FINALIZE", "media_id": media_id})
            if finished.status_code not in (200, 201):
                raise fail("finishing")
        except XPostError:
            raise
        except (OSError, ValueError) as exc:
            logger.warning("Uploading media to X failed (%s)", type(exc).__name__)
            raise fail("reading the file") from None
        except Exception as exc:
            logger.warning("Uploading media to X failed (%s)", type(exc).__name__)
            raise fail("connection") from None
        ids.append(media_id)
    return ids


def _create(account: XAccount, token: dict, body: dict[str, Any]) -> Outcome:
    try:
        response = x_accounts.send("POST", f"{x_accounts.API_BASE}/tweets", headers=x_accounts.bearer(token),
                                   json=body)
    except Exception as exc:
        logger.warning("Posting to X as %s got no answer (%s)", account.handle, type(exc).__name__)
        if _definite(exc):
            return Outcome("failed", detail="Row-Bot couldn't reach X.")
        return Outcome("unknown")
    if response.status_code >= 500:
        return Outcome("unknown")
    if response.status_code not in (200, 201):
        return Outcome("failed", detail=_refusal(response, account))
    try:
        post_id = str((response.json().get("data") or {}).get("id") or "")
    except ValueError:
        post_id = ""
    return Outcome("posted", post_id, _stamp(_now())) if post_id.isdigit() else Outcome("unknown")


def _comparable(text: str) -> str:
    """A post's words as X may give them back: links shortened or added (media), entities escaped."""
    return " ".join(re.sub(r"https?://\S+", " ", html.unescape(text or "")).split()).casefold()


def _reconcile(account: XAccount, token: dict, operation: dict[str, Any]) -> Outcome:
    """Look among the account's latest posts for the one this operation sent (no resend)."""
    action, target = operation.get("action"), str(operation.get("target") or "")
    try:
        if action == "delete":
            response = x_accounts.send("GET", f"{x_accounts.API_BASE}/tweets/{target}",
                                       headers=x_accounts.bearer(token))
            if response.status_code == 404:
                return Outcome("posted", target, _stamp(_now()))
            data = response.json() if response.status_code == 200 else None
            if data is None:
                return Outcome("unknown")
            return Outcome("posted", target, _stamp(_now())) if not data.get("data") else Outcome("failed")
        response = x_accounts.send("GET", f"{x_accounts.API_BASE}/users/{account.id}/tweets",
                                   headers=x_accounts.bearer(token),
                                   params={"max_results": _RECENT, "tweet.fields": "created_at,referenced_tweets"})
        if response.status_code != 200:
            return Outcome("unknown")
        posts = response.json().get("data") or []
    except Exception as exc:
        logger.warning("Reading the latest posts of %s failed (%s)", account.handle, type(exc).__name__)
        return Outcome("unknown")
    try:
        since = datetime.fromisoformat(str(operation.get("started_at")).replace("Z", "+00:00")) - timedelta(minutes=5)
    except ValueError:
        since = _now() - timedelta(days=1)
    wanted = _comparable(str(operation.get("text") or ""))
    kind = {"reply": "replied_to", "quote": "quoted"}.get(str(action))
    for post in posts if isinstance(posts, list) else []:
        if not isinstance(post, dict) or _comparable(str(post.get("text") or "")) != wanted:
            continue
        try:
            created = datetime.fromisoformat(str(post.get("created_at")).replace("Z", "+00:00"))
        except ValueError:
            continue
        references = {(str(item.get("type")), str(item.get("id"))) for item in post.get("referenced_tweets") or []
                      if isinstance(item, dict)}
        if created >= since and (kind is None or (kind, target) in references):
            return Outcome("posted", str(post.get("id") or ""), _stamp(created))
    return Outcome("failed")


def _posted(account: XAccount, action: str, target: str, outcome: Outcome) -> str:
    url = account.post_url(outcome.post_id)
    lead = {"post": f"Posted as {account.handle}: {url}",
            "reply": f"Replied as {account.handle} to {target_url(target)}: {url}",
            "quote": f"Quoted {target_url(target)} as {account.handle}: {url}",
            "delete": f"Deleted as {account.handle}: post {outcome.post_id} ({url})"}[action]
    return f"{lead}\nPost ID: {outcome.post_id} · Account: {account.handle} (ID {account.id}) · Time: {outcome.at}"


_NOT_SURE = "Not sure it went through: X didn't confirm it."


def _settled_text(account: XAccount, action: str, target: str, outcome: Outcome, *, checked: bool) -> str:
    deleting = action == "delete"
    if outcome.status == "posted":
        found = ("" if not checked else f"{_NOT_SURE} Row-Bot looked on X and the post is gone.\n" if deleting
                 else f"{_NOT_SURE} Row-Bot read {account.handle}'s latest posts and found it.\n")
        return found + _posted(account, action, target, outcome)
    if outcome.status == "failed" and checked:
        return ((f"{_NOT_SURE} Row-Bot looked on X and the post is still there: it wasn't deleted." if deleting
                 else f"{_NOT_SURE} Row-Bot read {account.handle}'s latest posts and it isn't there: it didn't post.")
                + " Nothing was sent again; trying again needs a new approval.")
    if outcome.status == "failed":
        return f"Nothing was {'deleted' if action == 'delete' else 'posted'} as {account.handle}. {outcome.detail}".strip()
    return (f"{_NOT_SURE} Row-Bot couldn't read {account.handle}'s latest posts to check, so it may or may not "
            f"have gone out. Look at {account.handle} on X before trying again; Row-Bot won't send it again by "
            "itself.")


@dataclass(frozen=True)
class Sent:
    """How one send ended: ``posted`` (deleted, for a delete), ``failed`` or ``unknown``, and in words."""
    status: str
    text: str
    url: str = ""


def post_as(account: XAccount, action: str, text: str, target: str, media: list[Media]) -> Sent:
    """Send one approved post, reply, quote or delete as ``account``, at most once."""
    if action not in ACTIONS:
        raise XPostError(f"Unknown action {action}. Use post, reply, quote or delete.")
    with _SENDING:
        return _send_once(account, action, text, target, media)


def _sent(account: XAccount, action: str, target: str, outcome: Outcome, *, checked: bool, lead: str = "") -> Sent:
    url = account.post_url(outcome.post_id) if outcome.status == "posted" else ""
    return Sent(outcome.status, lead + _settled_text(account, action, target, outcome, checked=checked), url)


def _send_once(account: XAccount, action: str, text: str, target: str, media: list[Media]) -> Sent:
    token = x_accounts.valid_token(account)
    if token is None:
        return Sent("failed", f"Nothing was sent: the sign-in of {account.handle} has run out or was removed. The "
                              "person can reconnect it in Settings › Apps › X.")
    digest = payload_hash(account, action, text, target, media)
    earlier = _unsettled(account, digest)
    if earlier is not None:
        # The same payload went out before and X never confirmed it: check before sending it again.
        outcome = _reconcile(account, token, earlier)
        if outcome.status == "posted":
            _settle(earlier, "posted", post_id=outcome.post_id, url=account.post_url(outcome.post_id))
            return _sent(account, action, target, outcome, checked=False,
                         lead="An earlier attempt that X didn't confirm went through, so it wasn't sent again.\n")
        if outcome.status == "unknown":
            return Sent("unknown", f"An earlier attempt to send this as {account.handle} may have gone out, and "
                                   f"Row-Bot couldn't check. Look at {account.handle} on X first; nothing was sent now.")
        _settle(earlier, "not_posted")
    operation = _begin(account, action, digest, text, target)
    try:
        if action == "delete":
            outcome = _delete(account, token, target)
        else:
            body: dict[str, Any] = {"text": text}
            if action == "reply":
                body["reply"] = {"in_reply_to_tweet_id": target}
            elif action == "quote":
                body["quote_tweet_id"] = target
            if media:
                body["media"] = {"media_ids": _upload(media, token)}
            outcome = _create(account, token, body)
    except XPostError as exc:
        _settle(operation, "failed")
        return Sent("failed", str(exc))
    checked = outcome.status == "unknown"
    if checked:
        _settle(operation, "unknown")
        outcome = _reconcile(account, token, operation)
    facts = {"post_id": outcome.post_id, "url": account.post_url(outcome.post_id)} if outcome.post_id else {}
    _settle(operation, {"posted": "posted", "failed": "not_posted" if checked else "failed"}.get(outcome.status,
                                                                                                "unknown"), **facts)
    return _sent(account, action, target, outcome, checked=checked)


def _delete(account: XAccount, token: dict, target: str) -> Outcome:
    try:
        response = x_accounts.send("DELETE", f"{x_accounts.API_BASE}/tweets/{target}",
                                   headers=x_accounts.bearer(token))
    except Exception as exc:
        logger.warning("Deleting a post of %s got no answer (%s)", account.handle, type(exc).__name__)
        return Outcome("failed", detail="Row-Bot couldn't reach X.") if _definite(exc) else Outcome("unknown")
    if response.status_code >= 500:
        return Outcome("unknown")
    if response.status_code != 200:
        return Outcome("failed", detail=_refusal(response, account))
    try:
        deleted = bool((response.json().get("data") or {}).get("deleted"))
    except ValueError:
        deleted = False
    return Outcome("posted", target, _stamp(_now())) if deleted else Outcome("failed", detail="X didn't delete it.")


def read_text(account: XAccount, target: str) -> str:
    """A post's text, for the card that deletes it ("" when X doesn't say)."""
    token = x_accounts.valid_token(account)
    if token is None or not target:
        return ""
    try:
        response = x_accounts.send("GET", f"{x_accounts.API_BASE}/tweets/{target}", headers=x_accounts.bearer(token),
                                   timeout=10)
        return str((response.json().get("data") or {}).get("text") or "")[:_MAX_TEXT] if response.status_code == 200 else ""
    except Exception:
        return ""


# X counts a post's length by weight (5.3.1 G1): each link is 23, and characters outside the Latin and common
# punctuation ranges count 2. Without a subscription a post is 280; with one, 25,000.
_LINK = re.compile(r"https?://\S+", re.IGNORECASE)
LINK_LENGTH = 23
SHORT_POST, LONG_POST = 280, 25_000
_LIMITS: dict[str, int] = {}
MEDIA_LIMITS = "up to 4 pictures (5 MB each) or one GIF (15 MB) or one video (512 MB)"


def _weigh(text: str) -> int:
    import unicodedata

    return sum(1 if (code <= 4351 or 8192 <= code <= 8205 or 8208 <= code <= 8223 or 8242 <= code <= 8247) else 2
               for code in map(ord, unicodedata.normalize("NFC", text)))


def weighted_length(text: str) -> int:
    """The post's length as X counts it."""
    total, position = 0, 0
    for match in _LINK.finditer(text):
        total += _weigh(text[position:match.start()]) + LINK_LENGTH
        position = match.end()
    return total + _weigh(text[position:])


def text_limit(account: XAccount) -> int | None:
    """How long a post the account may send: 280, or 25,000 with an X subscription; None when X doesn't say."""
    if account.id in _LIMITS:
        return _LIMITS[account.id]
    token = x_accounts.valid_token(account)
    if token is None:
        return None
    try:
        response = x_accounts.send("GET", f"{x_accounts.API_BASE}/users/me", headers=x_accounts.bearer(token),
                                   params={"user.fields": "subscription_type"}, timeout=10)
        if response.status_code != 200:
            return None
        kind = str((response.json().get("data") or {}).get("subscription_type") or "None")
    except Exception:
        return None
    _LIMITS[account.id] = LONG_POST if kind in {"Basic", "Premium", "PremiumPlus"} else SHORT_POST
    return _LIMITS[account.id]


def check_length(account: XAccount, action: str, text: str) -> None:
    """Refuse, before the card, a post longer than the account may send (X would refuse it after approval)."""
    if action == "delete" or not text:
        return
    limit = text_limit(account)
    length = weighted_length(text)
    if limit is not None and length > limit:
        what = "with its X subscription" if limit == LONG_POST else "without an X subscription"
        raise XPostError(f"That post is {length:,} characters as X counts them (each link counts {LINK_LENGTH}); "
                         f"@{account.username} can post up to {limit:,} {what}. Shorten it by {length - limit:,}, "
                         "or post the rest as a reply to it.")


def check_text(action: str, text: str | None, target: str | None) -> tuple[str, str]:
    """The text and target an action needs, cleaned; raises XPostError when one is missing."""
    words = str(text or "")
    post = str(target or "").strip()
    if action != "delete" and not words.strip():
        raise XPostError("Give the text to post.")
    if action != "post" and not post.isdigit():
        raise XPostError(f"Give the numeric ID of the post to {action} (tweet_id).")
    if len(words) > _MAX_TEXT:
        raise XPostError("That text is longer than X allows.")
    return (words if action != "delete" else ""), (post if action != "post" else "")
