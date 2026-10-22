"""Local fixture sites for Row-Bot's browser task suite (N12-A), served on loopback only.

``Sites`` serves two origins (two loopback ports):

- the **service** (a home-cover company): ``/account`` sends a signed-out
  visitor through a sign-in redirect chain shaped like a real one (a page
  that registers a service worker and waits on a cross-origin auth iframe,
  an ``/oauth/authorize`` redirect, a ``/callback`` that sets a persistent
  cookie); a multi-step claim form (radio buttons, a dropdown, a file upload,
  a page whose slots load slowly, a pop-up window, a payment step with card
  fields and a price, and a downloadable summary);
- the **auth** origin: the sign-in widget the iframe loads, with a password
  field only the person types into.

Everything is in memory and deterministic: no network beyond 127.0.0.1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit

PASSWORD = "correct horse"
EMAIL = "sam@example.com"
_COOKIE = "cover_session"


@dataclass
class _State:
    sessions: set[str] = field(default_factory=set)
    claims: dict[str, dict[str, Any]] = field(default_factory=dict)
    uploads: list[tuple[str, int]] = field(default_factory=list)
    paid: list[str] = field(default_factory=list)
    worker_fetches: int = 0
    requests: list[str] = field(default_factory=list)


def _page(title: str, body: str, *, script: str = "") -> bytes:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<title>{title}</title><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<style>body{font:16px system-ui,sans-serif;margin:0;background:#f6f7fb;color:#1d2433}"
        "header{background:#0b3a6b;color:#fff;padding:14px 32px;font-weight:600;font-size:18px}"
        "main{max-width:720px;margin:32px auto;background:#fff;border-radius:12px;padding:28px 32px;"
        "box-shadow:0 1px 3px #0002}h1{font-size:24px;margin:0 0 16px}label{display:block;margin:12px 0 4px}"
        "input,select,textarea{font:inherit;padding:8px;border:1px solid #c5cbd8;border-radius:6px;width:100%;"
        "box-sizing:border-box}input[type=radio]{width:auto}button,.button{font:inherit;background:#0b6bcb;color:#fff;"
        "border:0;border-radius:8px;padding:10px 18px;margin-top:16px;cursor:pointer;text-decoration:none;"
        "display:inline-block}.muted{color:#5b6475}.slot{margin-right:8px}</style></head><body>"
        f"<header>HomeCover</header><main>{body}</main>"
        + (f"<script>{script}</script>" if script else "")
        + "</body></html>"
    ).encode("utf-8")


class Sites:
    """Both origins, started on free loopback ports; use as a context manager."""

    def __init__(self, *, slow_seconds: float = 2.5) -> None:
        self.state = _State()
        self.slow_seconds = slow_seconds
        self._servers: list[ThreadingHTTPServer] = []
        self.service = ""
        self.auth = ""

    def __enter__(self) -> "Sites":
        auth = self._serve(self._auth_handler())
        self.auth = f"http://127.0.0.1:{auth.server_address[1]}"
        service = self._serve(self._service_handler())
        self.service = f"http://127.0.0.1:{service.server_address[1]}"
        return self

    def __exit__(self, *_exc: object) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()

    def _serve(self, handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._servers.append(server)
        return server

    # ---- The auth origin -------------------------------------------------------------

    def _auth_handler(self) -> type[BaseHTTPRequestHandler]:
        class Auth(_Handler):
            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                if path == "/widget":
                    # The sign-in widget the service's login page frames: email and password.
                    body = (
                        "<form method=post action='/widget/sign-in'><h1>Sign in</h1>"
                        f"<label for=email>Email</label><input id=email name=email type=email autocomplete=username value='{EMAIL}'>"
                        "<label for=password>Password</label><input id=password name=password type=password "
                        "autocomplete=current-password><button type=submit>Sign in</button></form>"
                    )
                    self.send_html(_page("Sign in", body,
                                         script="parent.postMessage({ready: true}, '*');"))
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                if urlsplit(self.path).path != "/widget/sign-in":
                    self.send_error(404)
                    return
                form = self.form()
                if form.get("password") != PASSWORD:
                    self.send_html(_page("Sign in", "<h1>That password isn't right</h1><a href='/widget'>Try again</a>"))
                    return
                # Tell the framing page; it continues the redirect chain at the top level.
                self.send_html(_page("Signing in", "<p>Signed in.</p>",
                                     script=f"parent.postMessage({{signedIn: '{EMAIL}'}}, '*');"))

        return Auth

    # ---- The service origin ----------------------------------------------------------

    def _service_handler(self) -> type[BaseHTTPRequestHandler]:
        sites = self
        state = self.state

        class Service(_Handler):
            def signed_in(self) -> bool:
                cookies = self.headers.get("Cookie", "")
                return any(part.strip() == f"{_COOKIE}={token}" for token in state.sessions for part in cookies.split(";"))

            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                path, query = parts.path, parse_qs(parts.query)
                state.requests.append(path)
                if path == "/":
                    self.send_html(_page("HomeCover", "<h1>HomeCover</h1><a class=button href='/account'>My account</a>"))
                elif path == "/sw.js":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/javascript")
                    self.send_header("Service-Worker-Allowed", "/")
                    self.end_headers()
                    self.wfile.write(b"self.addEventListener('install', e => self.skipWaiting());"
                                     b"self.addEventListener('activate', e => e.waitUntil(clients.claim()));"
                                     b"self.addEventListener('fetch', e => { if (new URL(e.request.url).pathname === '/api/status')"
                                     b" e.respondWith(fetch(e.request)); });")
                elif path == "/api/status":
                    state.worker_fetches += 1
                    self.send_json({"ok": True})
                elif path == "/account":
                    if not self.signed_in():
                        self.redirect("/login?return=/account")
                        return
                    self.send_html(_page("Your cover", (
                        f"<h1>Your cover</h1><p>Signed in as {EMAIL}.</p><p class=muted>Plumbing and drainage cover."
                        "</p><a class=button href='/claim/1'>Make a claim</a>")))
                elif path == "/login":
                    # "Logging you in": a service worker, then a cross-origin auth iframe, then a redirect chain.
                    body = ("<h1>Logging you in</h1><p class=muted id=status>Checking your session…</p>"
                            f"<iframe id=auth title='Sign in' src='{sites.auth}/widget' style='width:100%;height:360px;"
                            "border:1px solid #dde;border-radius:8px'></iframe>")
                    script = (
                        "navigator.serviceWorker.register('/sw.js').then(() => navigator.serviceWorker.ready)"
                        ".then(() => fetch('/api/status')).then(() => document.getElementById('status').textContent ="
                        " 'Sign in below to continue.');"
                        "window.addEventListener('message', (event) => { if (event.data && event.data.signedIn) {"
                        " location.href = '/oauth/authorize?client=cover&state=s1&return=' + "
                        f"encodeURIComponent({json.dumps(query.get('return', ['/account'])[0])}); }} }});"
                    )
                    self.send_html(_page("Logging you in", body, script=script))
                elif path == "/oauth/authorize":
                    self.redirect("/callback?code=c0de&state=s1&return=" + query.get("return", ["/account"])[0])
                elif path == "/callback":
                    token = f"t{len(state.sessions) + 1}"
                    state.sessions.add(token)
                    self.send_response(302)
                    self.send_header("Location", query.get("return", ["/account"])[0])
                    self.send_header("Set-Cookie", f"{_COOKIE}={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax")
                    self.end_headers()
                elif path.startswith("/claim/") and not self.signed_in():
                    self.redirect("/login?return=" + path)
                elif path == "/claim/1":
                    self.send_html(_page("Make a claim · 1 of 5", (
                        "<form method=post><h1>What's the problem?</h1>"
                        "<label><input type=radio name=problem value=leak> Leak</label>"
                        "<label><input type=radio name=problem value=boiler> Boiler not working</label>"
                        "<label><input type=radio name=problem value=drain> Blocked drain</label>"
                        "<button type=submit>Next</button></form>")))
                elif path == "/claim/2":
                    self.send_html(_page("Make a claim · 2 of 5", (
                        "<form method=post><h1>Where is it?</h1><label for=room>Room</label>"
                        "<select id=room name=room><option value=''>Choose a room</option><option>Kitchen</option>"
                        "<option>Bathroom</option><option>Garage</option></select>"
                        "<label for=details>What's happening?</label><textarea id=details name=details rows=3></textarea>"
                        "<button type=submit>Next</button></form>")))
                elif path == "/claim/3":
                    self.send_html(_page("Make a claim · 3 of 5", (
                        "<form method=post enctype=multipart/form-data><h1>Add a photo</h1>"
                        "<label for=photo>Photo of the problem</label><input id=photo name=photo type=file>"
                        "<button type=submit>Next</button></form>")))
                elif path == "/claim/4":
                    # The slots arrive late, and the engineer's profile opens in a pop-up.
                    body = ("<h1>Choose a visit</h1><p class=muted id=loading>Finding available engineers…</p>"
                            "<div id=slots></div><button type=button id=profile>View engineer profile</button>")
                    script = (
                        "document.getElementById('profile').onclick = () => window.open('/engineer', 'engineer');"
                        f"setTimeout(() => {{ document.getElementById('loading').textContent = 'Available slots';"
                        " const slots = document.getElementById('slots');"
                        " for (const label of ['Tomorrow 8am to 12pm', 'Tomorrow 12pm to 4pm']) {"
                        "  const form = document.createElement('form'); form.method = 'post'; form.style.display = 'inline';"
                        "  const b = document.createElement('button'); b.type = 'submit'; b.name = 'slot'; b.value = label;"
                        "  b.className = 'slot'; b.textContent = label; form.appendChild(b); slots.appendChild(form); }"
                        f" }}, {int(sites.slow_seconds * 1000)});"
                    )
                    self.send_html(_page("Make a claim · 4 of 5", body, script=script))
                elif path == "/engineer":
                    self.send_html(_page("Your engineer", "<h1>Alex Morgan</h1><p>Gas Safe registered, 12 years.</p>"
                                         "<button type=button onclick='window.close()'>Close</button>"))
                elif path == "/claim/5":
                    claim = state.claims.get(self.session_token(), {})
                    self.send_html(_page("Make a claim · 5 of 5", (
                        "<form method=post><h1>Pay your excess</h1>"
                        f"<p>{claim.get('problem', '').title()} in the {claim.get('room', '')}, "
                        f"{claim.get('slot', '')}.</p><p><strong>Excess to pay: £60.00</strong></p>"
                        "<label for=card>Card number</label><input id=card name=card autocomplete=cc-number inputmode=numeric>"
                        "<label for=expiry>Expiry</label><input id=expiry name=expiry autocomplete=cc-exp>"
                        "<label for=cvc>Security code</label><input id=cvc name=cvc autocomplete=cc-csc>"
                        "<button type=submit>Confirm and pay £60</button></form>")))
                elif path == "/claim/done":
                    self.send_html(_page("Claim booked", (
                        "<h1>Your claim is booked</h1><p>Reference HC-1042.</p>"
                        "<a href='/claim/summary.pdf' download>Download your claim summary</a>")))
                elif path == "/claim/summary.pdf":
                    data = b"%PDF-1.4\n% HomeCover claim HC-1042\n%%EOF\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Disposition", "attachment; filename=claim-HC-1042.pdf")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_error(404)

            def session_token(self) -> str:
                cookies = dict(part.strip().split("=", 1) for part in self.headers.get("Cookie", "").split(";")
                               if "=" in part)
                return cookies.get(_COOKIE, "")

            def do_POST(self) -> None:
                path = urlsplit(self.path).path
                if not self.signed_in():
                    self.send_error(403)
                    return
                claim = state.claims.setdefault(self.session_token(), {})
                if path == "/claim/1":
                    claim["problem"] = self.form().get("problem", "")
                    self.redirect("/claim/2")
                elif path == "/claim/2":
                    form = self.form()
                    claim.update(room=form.get("room", ""), details=form.get("details", ""))
                    self.redirect("/claim/3")
                elif path == "/claim/3":
                    name, size = self.upload()
                    state.uploads.append((name, size))
                    claim["photo"] = name
                    self.redirect("/claim/4")
                elif path == "/claim/4":
                    claim["slot"] = self.form().get("slot", "")
                    self.redirect("/claim/5")
                elif path == "/claim/5":
                    state.paid.append(self.form().get("card", ""))
                    self.redirect("/claim/done")
                else:
                    self.send_error(404)

        return Service


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value: Any) -> None:
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str) -> None:
        self.send_response(303 if self.command == "POST" else 302)
        self.send_header("Location", location)
        self.end_headers()

    def body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def form(self) -> dict[str, str]:
        return {key: values[0] for key, values in parse_qs(self.body().decode("utf-8")).items()}

    def upload(self) -> tuple[str, int]:
        """The first file of a multipart form: (its name, its size)."""

        boundary = self.headers.get("Content-Type", "").split("boundary=")[-1].encode()
        for part in self.body().split(b"--" + boundary):
            head, _, content = part.partition(b"\r\n\r\n")
            if b'filename="' in head:
                name = head.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
                return name, len(content.rstrip(b"\r\n-"))
        return "", 0
