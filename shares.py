"""Publicly share a small group of services with Tailscale Funnel.

The hub is the gatekeeper. A group has one token, one expiry and one login
session; each selected service is mapped to one of Funnel's public ports
(8443, 10000). Port 443 is reserved for the private hub itself.

The old one-route API is kept as a compatibility wrapper for classic callers.
"""
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

NO_WINDOW = 0x08000000
COOKIE = "hubshare"
PUBLIC_PORTS = (8443, 10000)


class Shares:
    def __init__(self, state_dir: Path, restore_serve=None, port=None):
        self.port = int(port or os.environ.get("HUB_PORT") or 8790)  # Funnel forwards to this hub, on its port
        self.path = state_dir / "shares.json"
        self.restore_serve = restore_serve or (lambda: None)
        self.lock = threading.Lock()
        self.active = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else None
        threading.Thread(target=self._expiry, daemon=True).start()

    def _save(self):
        if self.active:
            self.path.write_text(json.dumps(self.active, ensure_ascii=False), encoding="utf-8")
        else:
            self.path.unlink(missing_ok=True)

    @staticmethod
    def _run(*args, timeout=20):
        try:
            kw = {"creationflags": NO_WINDOW} if sys.platform == "win32" else {}
            r = subprocess.run(list(args), capture_output=True, timeout=timeout, **kw)
            if len(args) > 1 and args[1] in ("serve", "funnel") and "status" not in args:
                try:
                    from core.detect import invalidate
                    invalidate("tailscale")
                except ImportError:
                    pass
            return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace").strip()
        except subprocess.TimeoutExpired as e:
            return -1, ((e.stdout or b"") + (e.stderr or b"")).decode("utf-8", "replace").strip()

    def funnel_allowed(self, status: dict) -> bool:
        caps = list((status.get("Self") or {}).get("CapMap") or []) + list((status.get("Self") or {}).get("Capabilities") or [])
        return any("funnel" in c.lower() for c in caps)

    def view(self):
        with self.lock:
            a = dict(self.active) if self.active else None
        if a:
            a["remaining"] = None if a["expires"] is None else max(0, int(a["expires"] - time.time()))
        return a

    def _disable_funnels(self, ports):
        for port in ports:
            self._run("tailscale", "funnel", f"--https={port}", "off", timeout=15)

    def _serve_backup(self, ports):
        """Capture existing Serve handlers for the public ports before Funnel.

        Funnel and Serve share the same per-port configuration. Without this,
        `funnel off` removes a pre-existing route (for example an OctoPrint
        Serve on :10000) permanently. Only the affected ports are captured;
        every other Serve route is left alone.
        """
        ports = set(int(p) for p in ports)
        code, out = self._run("tailscale", "serve", "status", "--json")
        if code != 0:
            raise RuntimeError(out.splitlines()[0] if out else "Could not read Serve state")
        try:
            state = json.loads(out)
        except ValueError as e:
            raise RuntimeError("Could not parse Serve state") from e
        backup = []
        for hostport, cfg in (state.get("Web") or {}).items():
            try:
                port = int(str(hostport).rsplit(":", 1)[-1])
            except ValueError:
                continue
            if port not in ports:
                continue
            for path, handler in (cfg.get("Handlers") or {}).items():
                target = handler.get("Proxy")
                if target:
                    backup.append({"port": port, "path": path, "target": target})
        return backup

    def _restore_serve(self, backup, ports):
        ports = set(int(p) for p in ports)
        for port in ports:
            self._run("tailscale", "serve", f"--https={port}", "off", timeout=15)
        for item in sorted(backup or [], key=lambda x: (x["port"], x["path"] != "/", x["path"])):
            args = ["tailscale", "serve", "--bg", f"--https={item['port']}"]
            if item.get("path") not in (None, "", "/"):
                args.append(f"--set-path={item['path']}")
            args.append(item["target"])
            self._run(*args, timeout=15)

    def start(self, routes, name="", minutes=60, who="", host=""):
        """Start one public group.

        routes: list of route dicts, or one route dict for the old API.
        """
        if isinstance(routes, dict):
            routes = [routes]
        # Classic hub.py called start(route, minutes, who, host).
        if isinstance(name, int):
            name, minutes, who, host = "", name, minutes, who
        routes = list(routes or [])
        if not 1 <= len(routes) <= len(PUBLIC_PORTS):
            raise ValueError("Select one or two services")
        for route in routes:
            if route.get("kind") != "proxy" or str(route.get("path") or "").rstrip("/") == "/hub":
                raise ValueError("Only proxied services can be shared")
            if not route.get("target"):
                raise ValueError("Route has no target")
            u = urllib.parse.urlparse(str(route["target"]))
            if u.port == self.port and (u.hostname or "").lower() in ("127.0.0.1", "localhost", "::1"):
                raise ValueError("The hub itself cannot be shared")

        minutes = int(minutes)
        if minutes:
            minutes = max(5, min(minutes, 24 * 60))
        name = (name or "").strip()[:64]
        if not name:
            name = " + ".join(str(r.get("name") or r.get("id") or "service") for r in routes)[:64]

        # Replace an existing group atomically enough for the UI.
        with self.lock:
            old = self.active
        if old:
            self.stop("replaced")

        backup = self._serve_backup(PUBLIC_PORTS)
        token = secrets.token_urlsafe(18)
        targets = []
        enabled = []
        try:
            for port, route in zip(PUBLIC_PORTS, routes):
                code, out = self._run(
                    "tailscale", "funnel", "--bg", f"--https={port}",
                    f"http://127.0.0.1:{self.port}", timeout=15,
                )
                if code != 0:
                    raise RuntimeError(out.splitlines()[0] if out else "Could not enable Funnel")
                enabled.append(port)
                targets.append({
                    "id": route.get("id"),
                    "route": route.get("key") or route.get("id"),
                    "name": route.get("name") or route.get("id"),
                    "public_port": port,
                    "target": route["target"],
                    "source_port": route.get("port"),
                })
        except Exception:
            self._disable_funnels(enabled)
            self._restore_serve(backup, enabled)
            raise

        urls = [
            {"port": t["public_port"], "name": t["name"],
             "url": f"https://{host}:{t['public_port']}/s/{token}"}
            for t in targets
        ]
        with self.lock:
            self.active = {
                "token": token,
                "name": name,
                "routes": targets,
                "urls": urls,
                "url": urls[0]["url"] if urls else "",
                "created": time.time(),
                "expires": (time.time() + minutes * 60) if minutes else None,
                "by": who,
                "serve_backup": backup,
            }
            self._save()
        return self.view()

    def stop(self, why="stopped"):
        with self.lock:
            active = self.active or {}
            had = self.active is not None
            ports = [r["public_port"] for r in active.get("routes", [])]
            backup = active.get("serve_backup") or []
            self.active = None
            self._save()
        self._disable_funnels(ports)
        self._restore_serve(backup, ports)
        try:
            self.restore_serve()
        except Exception:
            pass
        return {"stopped": had, "why": why}

    def valid(self, token: str, public_port=None):
        with self.lock:
            a = self.active
        if not a or not isinstance(token, str) or not token \
                or not secrets.compare_digest(token.encode("utf-8"), str(a["token"]).encode("utf-8")):
            return None
        if a["expires"] is not None and time.time() >= a["expires"]:
            return None
        if public_port is not None:
            target = next((r for r in a.get("routes", []) if int(r["public_port"]) == int(public_port)), None)
            return a if target else None
        return a

    def target_for(self, share, public_port):
        for route in share.get("routes", []):
            if int(route["public_port"]) == int(public_port):
                return route
        return None

    def _expiry(self):
        while True:
            a = self.active
            if a and a["expires"] is not None and time.time() >= a["expires"]:
                self.stop("expired")
            time.sleep(10)
