"""Tailscale Funnel gate for one public group (one or two services).

Public ports are 8443 and 10000. Port 443 stays as the private hub. The hub
proxies HTTP, long-lived streams and WebSocket upgrades, and puts a shared
login page/cookie in front of both ports.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import ipaddress
import json
import secrets
import select
import socket
import threading
import time
import urllib.parse
from pathlib import Path

from . import Adapter

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
AUTH_COOKIE = "hubauth"
SESSION_TTL = 7 * 86400
LOGIN_MAX_BODY = 4096          # a user name and a password; anything bigger is not a login
LOGIN_MAX_TRIES = 5            # failures per client IP before it waits
LOGIN_LOCK = 300               # seconds
LOGIN_TRACKED = 4096           # client IPs remembered at most (a scan from many IPs cannot grow it)
PROXY_MAX_BODY = 1 << 30       # request bodies sent to a shared service (streamed, never held in memory)
LOGIN_LANGS = ("en", "es")
_LOGIN_TEXT: dict = {}


def login_text(lang: str) -> dict:
    """The login page's strings (keys funnel.* of the interface catalogs, site/i18n/<lang>.json),
    with English underneath anything a catalog lacks."""
    for code in {"en", lang}:
        if code not in _LOGIN_TEXT:
            path = Path(__file__).resolve().parents[2] / "site" / "i18n" / f"{code}.json"
            try:
                cat = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cat = {}
            _LOGIN_TEXT[code] = {k[len("funnel."):]: v for k, v in cat.items() if k.startswith("funnel.")}
    return {**_LOGIN_TEXT["en"], **_LOGIN_TEXT[lang]}


class Impl(Adapter):
    name = "funnel"
    caps = ["funnel"]

    def probe(self):
        self.ts = self.ctx.get("adapters_by_name", {}).get("tailscale")
        if not (self.ts and "serve" in self.ts.caps):
            return False
        from shares import Shares, COOKIE, PUBLIC_PORTS
        self.PUBLIC_PORTS = PUBLIC_PORTS
        self.shares = Shares(self.ctx["data"])
        self.cookie = COOKIE
        self.public_auth = self.ctx["config"].get("public_auth") or {}
        self.sessions = {}
        self.sessions_lock = threading.Lock()
        self.login_failures = {}
        self.login_checks = threading.BoundedSemaphore(2)  # PBKDF2 is slow on purpose: cap the CPU it can take
        return True

    def allowed(self):
        st = self.ts.status()
        return self.shares.funnel_allowed(st), str((st.get("Self") or {}).get("ID", ""))

    def set_public_auth(self, auth):
        """Apply a per-hub login change without restarting the hub."""
        with self.sessions_lock:
            self.public_auth = dict(auth or {})
            self.sessions.clear()

    # ---------- helpers ----------
    @staticmethod
    def _cookies(h):
        return {k.strip(): v for k, _, v in
                (c.partition("=") for c in (h.headers.get("Cookie") or "").split(";"))}

    @staticmethod
    def _public_port(h):
        host = (h.headers.get("Host") or "").strip()
        if host.startswith("["):  # IPv6 literal, not expected for Funnel
            return 443
        tail = host.rsplit(":", 1)[-1]
        try:
            port = int(tail)
        except ValueError:
            port = 443
        return port

    @staticmethod
    def _body_length(h, limit):
        """Content-Length as an int, or None when it is missing a number, negative or over the limit."""
        try:
            n = int(h.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        return n if 0 <= n <= limit else None

    @staticmethod
    def _refuse(h, code, text):
        """Answer without reading the body, and close: the unread body must not become the next request."""
        body = text.encode()
        h.close_connection = True
        h.send_response(code)
        h.send_header("Content-Type", "text/plain")
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Connection", "close")
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(body)

    @staticmethod
    def _visitor_ip(h):
        """The Internet client's IP. Funnel reaches the hub from tailscaled on loopback and names the
        client in X-Forwarded-For. The last entry is the one tailscaled wrote; earlier ones would come
        from the client itself and could be anything."""
        peer = h.client_address[0].removeprefix("::ffff:")
        try:
            local = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            local = False
        fwd = (h.headers.get("X-Forwarded-For") or "").split(",")[-1].strip().removeprefix("::ffff:")
        if local and fwd:
            try:
                return str(ipaddress.ip_address(fwd))
            except ValueError:
                pass
        return peer

    def _note_failure(self, ip, now):
        rec = self.login_failures.get(ip) or {"n": 0, "until": 0}
        n = rec["n"] + 1
        self.login_failures[ip] = {"n": n, "until": now + LOGIN_LOCK if n >= LOGIN_MAX_TRIES else 0, "seen": now}
        if len(self.login_failures) > LOGIN_TRACKED:
            # forget the stale first, then the oldest, so the table stays bounded
            for k in [k for k, r in self.login_failures.items() if r["until"] < now and now - r["seen"] > LOGIN_LOCK]:
                self.login_failures.pop(k, None)
            for k in sorted(self.login_failures, key=lambda k: self.login_failures[k]["seen"])[
                    :max(0, len(self.login_failures) - LOGIN_TRACKED)]:
                self.login_failures.pop(k, None)

    def _auth_ok(self, h):
        cookie = self._cookies(h).get(AUTH_COOKIE)
        if not cookie:
            return False
        with self.sessions_lock:
            until = self.sessions.get(cookie)
            if not until or until < time.time():
                self.sessions.pop(cookie, None)
                return False
        return True

    def _password_ok(self, user, password):
        auth = self.public_auth or {}
        want_user = str(auth.get("user") or "")
        salt = str(auth.get("salt") or "")
        digest = str(auth.get("hash") or "")
        if not (want_user and salt and digest):
            return False
        try:
            iterations = int(auth.get("iterations") or 210000)
        except ValueError:
            iterations = 210000
        got = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt),
            iterations,
        ).hex()
        return hmac.compare_digest(user, want_user) and hmac.compare_digest(got, digest)

    @staticmethod
    def _visitor_lang(h):
        """The visitor's language, from Accept-Language: they come from the Internet, so the hub's own
        language (the owner's) does not apply. English when none of theirs is available."""
        for part in (h.headers.get("Accept-Language") or "").split(","):
            tag = part.split(";", 1)[0].strip().lower().split("-", 1)[0]
            if tag in LOGIN_LANGS:
                return tag
        return "en"

    @staticmethod
    def _safe_next(value):
        """The page to return to after the login, or "/".

        Only a path on this same hub qualifies: it starts with one "/", has no
        scheme or host, no backslash and no control characters. Anything else
        ("//evil.example", "https://…", "/\\…") would turn the login into an
        open redirect, so it falls back to "/" as before."""
        if not isinstance(value, str) or not value.startswith("/") or len(value) > 2048:
            return "/"
        if value.startswith("//") or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            return "/"
        parts = urllib.parse.urlsplit(value)
        if parts.scheme or parts.netloc or parts.path.startswith("/__login"):
            return "/"
        return value

    def _login_page(self, h, error="", next_path=None):
        """`error`: the key of a message in the catalogs (wrong, busy, invalid, unconfigured).
        `next_path`: where to go after a good login; by default the page that was asked for
        (a deep link such as a notification's clip keeps working through the login)."""
        if next_path is None:
            next_path = getattr(h, "path", "/") if getattr(h, "command", "GET") in ("GET", "HEAD") else "/"
        next_path = self._safe_next(next_path)
        lang = self._visitor_lang(h)
        t = login_text(lang)
        msg = ('<p class="err">%s</p>' % html.escape(t.get(error, error))) if error else ""
        body = """<!doctype html><html lang="__LANG__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0d0c;color:#e8ebe4;
font:16px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}
main{width:min(360px,calc(100vw - 32px));padding:28px;border:1px solid #29302b;border-radius:14px;background:#111512}
h1{margin:0 0 6px;font-size:24px;letter-spacing:-.03em}p{color:#9aa59d}
label{display:block;margin:14px 0 6px;font-size:13px;color:#b8c1ba}
input{width:100%;padding:11px 12px;border-radius:9px;border:1px solid #374139;background:#0b0d0c;color:#e8ebe4}
button{width:100%;margin-top:18px;padding:11px;border:0;border-radius:9px;background:#c4ea6c;color:#0b0d0c;font-weight:650}
.err{color:#ff9d8a}.note{font-size:12px;color:#7f8a82;margin-top:14px}
</style></head><body><main><h1>__TITLE__</h1>
<p>__LEDE__</p>__ERROR__
<form method="post" action="/__login">
<label for="u">__USER__</label><input id="u" name="user" autocomplete="username" required>
<label for="p">__PASSWORD__</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<input type="hidden" name="next" value="__NEXT__">
<button type="submit">__SUBMIT__</button></form>
</main></body></html>"""
        for key, value in (("__LANG__", lang), ("__TITLE__", t.get("title", "Log in")), ("__LEDE__", t.get("lede", "")),
                           ("__USER__", t.get("user", "User")), ("__PASSWORD__", t.get("password", "Password")),
                           ("__SUBMIT__", t.get("submit", "Log in")), ("__NEXT__", next_path)):
            body = body.replace(key, html.escape(value))
        body = body.replace("__ERROR__", msg)  # last: msg is already HTML
        raw = body.encode("utf-8")
        h.send_response(401 if error else 200)
        h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Content-Length", str(len(raw)))
        h.send_header("Content-Language", lang)
        h.send_header("Vary", "Accept-Language")
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(raw)

    def _handle_login(self, h):
        if not (self.public_auth or {}).get("user"):
            self._login_page(h, "unconfigured")
            return
        n = self._body_length(h, LOGIN_MAX_BODY)
        if n is None:
            return self._refuse(h, 413, "Payload too large")
        try:
            raw = h.rfile.read(n) if n else b""
            form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
            user = (form.get("user") or [""])[0]
            password = (form.get("password") or [""])[0]
            next_path = self._safe_next((form.get("next") or ["/"])[0])
        except Exception:
            self._login_page(h, "invalid", "/")
            return
        # Failure brake per Internet client, so a bot elsewhere cannot lock the owner out.
        ip = self._visitor_ip(h)
        now = time.time()
        rec = self.login_failures.get(ip) or {"until": 0}
        if rec["until"] > now:
            self._login_page(h, "busy", next_path)
            return
        if not self.login_checks.acquire(timeout=5):  # many clients at once: shed load, do not queue
            self._login_page(h, "busy", next_path)
            return
        try:
            good = self._password_ok(user, password)
        finally:
            self.login_checks.release()
        if not good:
            self._note_failure(ip, now)
            self._login_page(h, "wrong", next_path)
            return
        self.login_failures.pop(ip, None)
        token = secrets.token_urlsafe(24)
        with self.sessions_lock:
            self.sessions[token] = now + SESSION_TTL
        h.send_response(303)
        h.send_header("Set-Cookie", f"{AUTH_COOKIE}={token}; Path=/; Max-Age={SESSION_TTL}; Secure; HttpOnly; SameSite=Lax")
        h.send_header("Location", next_path)
        h.send_header("Content-Length", "0")
        h.end_headers()

    # ---------- gatekeeper ----------
    def gate(self, h):
        port = self._public_port(h)
        path = urllib.parse.urlparse(h.path).path
        if path.startswith("/s/"):
            share = self.shares.valid(path[3:].strip("/"))
            if share:
                age = 30 * 86400 if share["expires"] is None else max(5, int(share["expires"] - time.time()))
                h.send_response(302)
                h.send_header("Set-Cookie", f"{self.cookie}={share['token']}; Path=/; Max-Age={age}; Secure; HttpOnly; SameSite=Lax")
                h.send_header("Location", "/")
                h.send_header("Content-Length", "0")
                h.end_headers()
                return
        if path == "/__login":
            if h.command == "POST":
                return self._handle_login(h)
            return self._login_page(h)
        share = self.shares.valid(self._cookies(h).get(self.cookie, ""), port)
        if not share:
            return self._not_found(h)
        if not self._auth_ok(h):
            return self._login_page(h)
        route = self.shares.target_for(share, port)
        if not route:
            return self._not_found(h)
        self.proxy(h, route["target"])

    @staticmethod
    def _not_found(h):
        body = b"Not found"
        h.send_response(404)
        h.send_header("Content-Type", "text/plain")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(body)

    # ---------- HTTP / WebSocket proxy ----------
    def proxy(self, h, target):
        if (h.headers.get("Upgrade") or "").lower() == "websocket":
            return self.proxy_ws(h, target)
        import http.client
        u = urllib.parse.urlparse(target)
        n = self._body_length(h, PROXY_MAX_BODY)
        if n is None:
            return self._refuse(h, 413, "Payload too large")
        if h.headers.get("Transfer-Encoding"):  # chunked uploads are not forwarded (no length to stream)
            return self._refuse(h, 411, "Length required")
        hdrs = {k: v for k, v in h.headers.items()
                if k.lower() not in HOP and not k.lower().startswith("tailscale-")}
        if "Cookie" in hdrs:
            rest = "; ".join(c.strip() for c in hdrs["Cookie"].split(";")
                             if not c.strip().startswith((self.cookie + "=", AUTH_COOKIE + "=")))
            hdrs.pop("Cookie")
            if rest:
                hdrs["Cookie"] = rest
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=None)
        try:
            # X-Hub-Proxy: the backend sees 127.0.0.1, so tell it a public client
            # is behind this request (a camera panel refuses its camera
            # root terminal to proxied requests).
            conn.putrequest(h.command, h.path, skip_host=True, skip_accept_encoding=True)
            for k, v in {**hdrs, "Host": f"{u.hostname}:{u.port}", "X-Hub-Proxy": "1"}.items():
                conn.putheader(k, v)
            if n or h.command in ("POST", "PUT", "PATCH"):
                conn.putheader("Content-Length", str(n))
            conn.endheaders()
            left = n  # the body goes through in 1 MB pieces, never whole in memory
            while left > 0:
                chunk = h.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                conn.send(chunk)
                left -= len(chunk)
            r = conn.getresponse()
            h.send_response(r.status, r.reason)
            for k, v in r.getheaders():
                if k.lower() in HOP:
                    continue
                if k.lower() == "location":
                    v = v.replace(f"{u.scheme}://{u.hostname}:{u.port}", "")
                h.send_header(k, v)
            h.send_header("Connection", "close")
            # Let pages on this hub's other public ports frame the service (see
            # H.end_headers). The host is this node's own Tailscale name, never the
            # request's Host header.
            own = ((self.ts.status().get("Self") or {}).get("DNSName") or "").rstrip(".")
            if own and all(c.isalnum() or c in ".-" for c in own):
                h._frame_ancestors = f"'self' https://{own}:*"
            h.end_headers()
            if h.command != "HEAD":
                # read1: forward whatever has arrived. read(64 KB) waited for a full
                # 64 KB, holding back MJPEG frames (~50 KB each) until the next one.
                while True:
                    chunk = r.read1(1 << 16)
                    if not chunk:
                        break
                    h.wfile.write(chunk)
                    h.wfile.flush()
        except Exception as e:
            try:
                h.send_error(502, f"service unavailable ({type(e).__name__})")
            except Exception:
                pass
        finally:
            conn.close()

    def proxy_ws(self, h, target):
        u = urllib.parse.urlparse(target)
        upstream = socket.create_connection((u.hostname, u.port or 80), timeout=10)
        upstream.settimeout(None)
        client = h.connection
        try:
            key = h.headers.get("Sec-WebSocket-Key")
            if not key:
                return h.send_error(400, "missing websocket key")
            # Build the handshake instead of forwarding Funnel/identity headers.
            # Some upstreams (Frigate/nginx) reject the extra proxy headers or
            # answer 500; this is exactly what a direct tailnet WebSocket sends.
            headers = [
                f"Host: {u.hostname}:{u.port}",
                "Connection: Upgrade",
                "Upgrade: websocket",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
                "X-Hub-Proxy: 1",
            ]
            for name in ("Origin", "Sec-WebSocket-Protocol", "User-Agent"):
                value = h.headers.get(name)
                if value:
                    headers.append(f"{name}: {value}")
            request = (f"{h.command} {h.path} {h.request_version}\r\n"
                       + "\r\n".join(headers) + "\r\n\r\n").encode("latin-1", "replace")
            upstream.sendall(request)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = upstream.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head, rest = buf.split(b"\r\n\r\n", 1)
            client.sendall(head + b"\r\n\r\n")
            if rest:
                client.sendall(rest)
            client.settimeout(None)
            while True:
                readable, _, _ = select.select([client, upstream], [], [], 120)
                if not readable:
                    continue
                for src in readable:
                    data = src.recv(65536)
                    if not data:
                        return
                    (upstream if src is client else client).sendall(data)
        except Exception:
            pass
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    # ---------- API (tailnet) ----------
    def get(self, path, query):
        if path == "/api/funnel":
            ok, node = self.allowed()
            return {"allowed": ok, "active": self.shares.view(),
                    "ports": list(self.PUBLIC_PORTS),
                    "enable_url": "https://login.tailscale.com/f/funnel?node=" + node}

    def post(self, path, body, query, handler):
        if path == "/api/funnel/stop":
            return self.shares.stop()
        if path == "/api/funnel/share":
            from ..server import CORE
            d = CORE.data(handler.headers.get("Host", ""))
            ids = body.get("ids")
            if not ids and body.get("id"):
                ids = [body["id"]]
            ids = list(ids or [])[:2]
            routes = []
            for rid in ids:
                route = next((r for r in d["routes"] if r["id"] == rid), None)
                if not route:
                    return ("error", 404, "route not found: %s" % rid)
                routes.append(route)
            if not routes:
                return ("error", 400, "select one or two routes")
            if not self.allowed()[0]:
                return ("error", 409, "Funnel is not enabled for this tailnet")
            try:
                return self.shares.start(
                    routes, body.get("name", ""), int(body.get("minutes", 60)),
                    handler.headers.get("Tailscale-User-Login", ""),
                    d["self"].get("dns", ""),
                )
            except (ValueError, RuntimeError) as e:
                return ("error", 409, str(e))
