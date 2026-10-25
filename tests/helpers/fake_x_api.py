"""A fake X API (v2, the v1.1 media upload and OAuth's token endpoint) for deterministic tests.

It runs in-process as an httpx transport: ``x_accounts.transport = FakeXApi().transport()`` sends
every X request here, so nothing reaches X. Users are known by their access tokens; posts, media
and every request are kept in memory for the test to read. ``after_send`` makes the next create or
delete go through on "X's side" and then lose its answer (a timeout or a dropped connection), and
``before_send`` fails it before it reaches X.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs

import httpx

API = "https://api.x.com"
UPLOAD = "https://upload.twitter.com/1.1/media/upload.json"
ROWBOT = ("111", "rowbot_ai", "Row-Bot")
PERSONAL = ("222", "sidd", "Sidd")


def install(monkeypatch, fake: FakeXApi, *users: tuple[str, str, str]) -> list:
    """Send every X request to ``fake``, with an in-memory keychain and the person's own X app; each user
    in ``users`` (id, handle, name) is a connected account, in that order."""
    from row_bot import secret_store, x_accounts
    from tests.subsystem.plugins.conftest import MemoryKeyring

    monkeypatch.setattr(secret_store, "_backend_override", MemoryKeyring())
    monkeypatch.setattr(x_accounts, "transport", fake.transport())
    monkeypatch.setenv("X_CLIENT_ID", "fixture-x-client")
    monkeypatch.setenv("X_CLIENT_SECRET", "fixture-x-secret")
    return [x_accounts.add(fake.sign_in(*user)) for user in users]


@dataclass
class FakeXApi:
    users: dict[str, dict[str, str]] = field(default_factory=dict)  # access token -> {id, username, name}
    refreshes: dict[str, dict[str, Any]] = field(default_factory=dict)  # refresh token -> the new token
    posts: list[dict[str, Any]] = field(default_factory=list)
    requests: list[tuple[str, str]] = field(default_factory=list)  # (method, path), in order
    uploaded: dict[str, int] = field(default_factory=dict)  # media id -> bytes appended
    after_send: str = ""  # "timeout" or "disconnect": the next create or delete happens, its answer is lost
    before_send: str = ""  # "connect": the next create or delete never reaches X
    fail_reads: bool = False  # Reading posts fails (X down)
    now: str = "2026-10-22T10:00:00.000Z"  # When X says each post was made; "" is the real time (browser fixture).
    _next: int = 1_900_000_000_000_000_000

    def sign_in(self, user_id: str, username: str, name: str = "", *, access: str = "",
                expires_at: float = 4_070_908_800.0) -> dict[str, Any]:
        """A sign-in (token) for this user, as X's token endpoint would give it."""
        access = access or f"access-{username}"
        self.users[access] = {"id": user_id, "username": username, "name": name}
        return {"access_token": access, "refresh_token": f"refresh-{username}", "expires_at": expires_at,
                "token_type": "bearer", "scope": "tweet.read tweet.write users.read offline.access"}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def post(self, author: str, text: str, *, created_at: str = "", references: list | None = None) -> dict:
        """A post already on X (written by someone, or sent earlier)."""
        self._next += 1
        moment = created_at or self.now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        item = {"id": str(self._next), "author_id": author, "text": text, "created_at": moment,
                "edit_history_tweet_ids": [str(self._next)]}
        if references:
            item["referenced_tweets"] = references
        self.posts.append(item)
        return item

    def posted_by(self, user_id: str) -> list[dict[str, Any]]:
        return [item for item in self.posts if item["author_id"] == user_id]

    # ── The API ─────────────────────────────────────────────────────────────

    def _user(self, request: httpx.Request) -> dict[str, str] | None:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        return self.users.get(token)

    def _lost(self) -> None:
        kind, self.after_send = self.after_send, ""
        if kind == "timeout":
            raise httpx.ReadTimeout("timed out after sending")
        if kind == "disconnect":
            raise httpx.RemoteProtocolError("server disconnected without sending a response")

    def _unreached(self) -> None:
        kind, self.before_send = self.before_send, ""
        if kind == "connect":
            raise httpx.ConnectError("connection refused")

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?", 1)[0]
        path = url.removeprefix(API)
        self.requests.append((request.method, path if url.startswith(API) else url))
        if url == f"{API}/2/oauth2/token":
            return self._token(request)
        if url == UPLOAD:
            return self._upload(request)
        user = self._user(request)
        if user is None:
            return httpx.Response(401, json={"title": "Unauthorized", "detail": "Unauthorized"})
        parts = path.strip("/").split("/")  # ["2", ...]
        if request.method == "GET" and parts[1:] == ["users", "me"]:
            return httpx.Response(200, json={"data": dict(user)})
        if request.method == "POST" and parts[1:] == ["tweets"]:
            self._unreached()
            body = json.loads(request.content or b"{}")
            references = []
            if "reply" in body:
                references.append({"type": "replied_to", "id": body["reply"]["in_reply_to_tweet_id"]})
            if "quote_tweet_id" in body:
                references.append({"type": "quoted", "id": body["quote_tweet_id"]})
            text = body.get("text", "")
            for media_id in (body.get("media") or {}).get("media_ids", []):
                if media_id not in self.uploaded:
                    return httpx.Response(400, json={"title": "Invalid Request", "detail": "unknown media"})
                text += f" https://t.co/m{media_id[-4:]}"  # X adds the media's link to the text it returns.
            item = self.post(user["id"], text.replace("&", "&amp;"), references=references)
            item["media_ids"] = list((body.get("media") or {}).get("media_ids", []))
            self._lost()
            return httpx.Response(201, json={"data": {"id": item["id"], "text": item["text"],
                                                      "edit_history_tweet_ids": [item["id"]]}})
        if parts[1] == "tweets" and len(parts) == 3:
            found = next((item for item in self.posts if item["id"] == parts[2]), None)
            if request.method == "DELETE":
                self._unreached()
                if found is None or found["author_id"] != user["id"]:
                    return httpx.Response(403, json={"title": "Forbidden", "detail": "not yours"})
                self.posts.remove(found)
                self._lost()
                return httpx.Response(200, json={"data": {"deleted": True}})
            if self.fail_reads:
                return httpx.Response(503, json={"title": "Service Unavailable"})
            if found is None:
                return httpx.Response(200, json={"errors": [{"title": "Not Found Error", "value": parts[2]}]})
            return httpx.Response(200, json={"data": {key: found[key] for key in ("id", "text", "author_id",
                                                                                   "created_at")}})
        if request.method == "GET" and parts[1] == "users" and len(parts) == 4 and parts[3] in {"tweets", "mentions"}:
            if self.fail_reads:
                return httpx.Response(503, json={"title": "Service Unavailable"})
            mine = [item for item in reversed(self.posts) if item["author_id"] == parts[2]]
            shown = [{key: value for key, value in item.items() if key != "media_ids"} for item in mine]
            return httpx.Response(200, json={"data": shown[:int(request.url.params.get("max_results", "10"))],
                                             "meta": {"result_count": len(shown)}} if shown else {"meta": {}})
        if parts[1] == "users" and len(parts) >= 4 and parts[3] in {"likes", "retweets", "bookmarks"}:
            if parts[2] != user["id"]:
                return httpx.Response(403, json={"title": "Forbidden", "detail": "not this user"})
            return httpx.Response(200, json={"data": {"done": True}})
        return httpx.Response(404, json={"title": "Not Found"})

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        fresh = self.refreshes.pop(form.get("refresh_token", ""), None) if form.get(
            "grant_type") == "refresh_token" else None
        if fresh is None:
            return httpx.Response(400, json={"error": "invalid_request"})
        return httpx.Response(200, json=fresh)

    def _upload(self, request: httpx.Request) -> httpx.Response:
        if self._user(request) is None:
            return httpx.Response(401, json={"errors": [{"message": "Unauthorized"}]})
        body = request.content
        if b'name="command"\r\n\r\nAPPEND' in body:
            media_id = body.split(b'name="media_id"\r\n\r\n', 1)[1].split(b"\r\n", 1)[0].decode()
            self.uploaded[media_id] = self.uploaded.get(media_id, 0) + len(body)
            return httpx.Response(204)
        form = {key: values[0] for key, values in parse_qs(body.decode("utf-8", "replace")).items()}
        if form.get("command") == "INIT":
            self._next += 1
            media_id = str(self._next)
            self.uploaded[media_id] = 0
            return httpx.Response(202, json={"media_id": int(media_id), "media_id_string": media_id})
        if form.get("command") == "FINALIZE":
            return httpx.Response(201, json={"media_id_string": form.get("media_id", "")})
        return httpx.Response(400, json={"errors": [{"message": "bad command"}]})
