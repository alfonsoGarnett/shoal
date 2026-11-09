"""Publicly share small groups of services with Tailscale Funnel.

The hub is the gatekeeper. Funnel only offers three ports and 443 is reserved
for the private hub, so a port cannot identify a service: the link does. Each
group has its own token and expiry and maps up to two services to the public
ports (8443, 10000); several groups can use the same ports side by side, and
the visitor's token decides which service answers. Stopping one group never
touches the others.

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
        self.lock = threading.Lock()   # the list of groups
        self.ops = threading.Lock()    # one start/stop at a time: they change Tailscale's per-port state
        self.groups = self._load()
        threading.Thread(target=self._expiry, daemon=True).start()

    def _load(self):
        if not self.path.exists():
            return []
        first = json.loads(self.path.read_text(encoding="utf-8"))
        return [first] + first.pop("more", [])

    def _save(self):
        # The first group stays at the top level: a hub rolled back to a one-group
        # release still reads it and keeps that link working.
        if self.groups:
            data = {**self.groups[0], "more": self.groups[1:]}
            self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        else:
            self.path.unlink(missing_ok=True)

    @staticmethod
    def _run(*args, timeout=20, stdout_only=False):
        try:
            kw = {"creationflags": NO_WINDOW} if sys.platform == "win32" else {}
            r = subprocess.run(list(args), capture_output=True, timeout=timeout, **kw)
            if len(args) > 1 and args[1] in ("serve", "funnel") and "status" not in args:
                try:
                    from core.detect import invalidate
                    invalidate("tailscale")
                except ImportError:
                    pass
            out = r.stdout if stdout_only and r.returncode == 0 else r.stdout + r.stderr
            return r.returncode, out.decode("utf-8", "replace").strip()
        except subprocess.TimeoutExpired as e:
            return -1, ((e.stdout or b"") + (e.stderr or b"")).decode("utf-8", "replace").strip()

    def funnel_allowed(self, status: dict) -> bool:
        caps = list((status.get("Self") or {}).get("CapMap") or []) + list((status.get("Self") or {}).get("Capabilities") or [])
        return any("funnel" in c.lower() for c in caps)

    @staticmethod
    def _ports(group):
        return [int(r["public_port"]) for r in group.get("routes", [])]

    @staticmethod
    def _public(group):
        a = dict(group)
        a["remaining"] = None if a["expires"] is None else max(0, int(a["expires"] - time.time()))
        return a

    def view(self):
        with self.lock:
            return [self._public(g) for g in self.groups]

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
        code, out = self._run("tailscale", "serve", "status", "--json", stdout_only=True)  # stderr may carry a version warning
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
        for item in sorted((b for b in backup or [] if int(b["port"]) in ports), key=lambda x: (x["port"], x["path"] != "/", x["path"])):
            args = ["tailscale", "serve", "--bg", f"--https={item['port']}"]
            if item.get("path") not in (None, "", "/"):
                args.append(f"--set-path={item['path']}")
            args.append(item["target"])
            self._run(*args, timeout=15)

    def start(self, routes, name="", minutes=60, who="", host=""):
        """Start one more public group; the groups already public are left alone.

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

        with self.ops:
            with self.lock:
                busy = {p for g in self.groups for p in self._ports(g)}
            fresh = [p for p in PUBLIC_PORTS[:len(routes)] if p not in busy]
            backup = self._serve_backup(fresh) if fresh else []
            enabled = []
            try:
                for port in fresh:
                    code, out = self._run(
                        "tailscale", "funnel", "--bg", f"--https={port}",
                        f"http://127.0.0.1:{self.port}", timeout=15,
                    )
                    if code != 0:
                        raise RuntimeError(out.splitlines()[0] if out else "Could not enable Funnel")
                    enabled.append(port)
            except Exception:
                self._disable_funnels(enabled)
                self._restore_serve(backup, enabled)
                raise

            token = secrets.token_urlsafe(18)
            targets = [{
                "id": route.get("id"),
                "route": route.get("key") or route.get("id"),
                "name": route.get("name") or route.get("id"),
                "public_port": port,
                "target": route["target"],
                "source_port": route.get("port"),
            } for port, route in zip(PUBLIC_PORTS, routes)]
            urls = [
                {"port": t["public_port"], "name": t["name"],
                 "url": f"https://{host}:{t['public_port']}/s/{token}"}
                for t in targets
            ]
            group = {
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
            with self.lock:
                self.groups.append(group)
                self._save()
            return self._public(group)

    def stop(self, token, why="stopped"):
        """Stop one group. A public port stays open while another group still uses it."""
        with self.ops:
            with self.lock:
                gone = [g for g in self.groups if g["token"] == token]
                self.groups = [g for g in self.groups if g["token"] != token]
                in_use = {p for g in self.groups for p in self._ports(g)}
                freed, restore = set(), []
                for group in gone:
                    freed.update(p for p in self._ports(group) if p not in in_use)
                    for item in group.get("serve_backup") or []:
                        # What was on a port before Funnel goes back when the port closes,
                        # so a group still using the port inherits the duty.
                        heir = next((g for g in self.groups if int(item["port"]) in self._ports(g)), None)
                        if heir:
                            heir.setdefault("serve_backup", []).append(item)
                        else:
                            restore.append(item)
                self._save()
            self._disable_funnels(sorted(freed))
            self._restore_serve(restore, freed)
            try:
                self.restore_serve()
            except Exception:
                pass
        return {"stopped": bool(gone), "why": why}

    def valid(self, token: str, public_port=None):
        if not isinstance(token, str) or not token:
            return None
        with self.lock:
            groups = list(self.groups)
        for g in groups:
            if not secrets.compare_digest(token.encode("utf-8"), str(g["token"]).encode("utf-8")):
                continue
            if g["expires"] is not None and time.time() >= g["expires"]:
                return None
            if public_port is not None and int(public_port) not in self._ports(g):
                return None
            return g
        return None

    def target_for(self, share, public_port):
        for route in share.get("routes", []):
            if int(route["public_port"]) == int(public_port):
                return route
        return None

    def _expiry(self):
        while True:
            with self.lock:
                due = [g["token"] for g in self.groups if g["expires"] is not None and time.time() >= g["expires"]]
            for token in due:
                self.stop(token, "expired")
            time.sleep(10)
