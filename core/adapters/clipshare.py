"""Shared clipboard for the whole tailnet (capability "clipboard").

The network history is stored by the mesh coordinator; the other capable hubs keep a
replica (clipboard.json) and serve it if the coordinator goes down. This adapter:
- exposes api/clip* on this hub (on the coordinator, its own history; on the others, forwarded
  to the coordinator);
- with "sync" enabled, keeps the device clipboard up to date: new history entries are placed
  on the device and whatever is copied on the device is published (if the system allows reading
  it in the background: Windows, Linux and macOS do, Android does not).
"""
from __future__ import annotations

import json
import threading
import time

from ..detect import run
from . import Adapter, disabled


class Impl(Adapter):
    name = "clipshare"
    caps = ["clipboard", "control"]

    def probe(self):
        self.mesh = self.ctx["mesh"]
        by = self.ctx.get("adapters_by_name", {})
        self.local = by.get("clipboard")
        self.store = None
        # "clipboard" disabled (demo mode): the network history never touches this device's clipboard
        self.windows = self.ctx["platform"]["family"] == "windows" and "clipboard" not in disabled()
        if "tailscale" in by and "push" in by:  # capable hub: it can store the network history
            import clipboard as appclip  # app module (on Windows it syncs with its clipboard)
            self.appclip = appclip
            self.store = appclip.Clipboard(self.ctx["data"], self.ctx["name"])
            self.store.os_sync = False  # until we know whether it coordinates
            threading.Thread(target=self.store.run, daemon=True).start()
            self.mesh.hooks.append(self.on_mesh)
        elif not (self.local and self.mesh.has_peers()):
            return False
        self.state_file = self.ctx["data"] / "clipshare.json"
        self.can_read = not self.ctx["platform"]["termux"]  # Android blocks background reads
        threading.Thread(target=self.loop, daemon=True).start()
        return True

    def on_mesh(self, event):
        if event == "replica":
            self.store.reload()

    def leading(self):
        """True if this hub stores the network history right now."""
        return bool(self.store) and self.mesh.is_coordinator()

    # ---------- coordinator ----------
    def master(self, method, path, body=None):
        from ..server import master_get, master_post  # avoids a circular import
        if method == "GET":
            with master_get(path, 10) as r:
                return json.load(r)
        return master_post(path, body or {}, 10)

    def state(self):
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {"sync": True, "last_id": None, "last_local": None}

    def save(self, s):
        self.state_file.write_text(json.dumps(s))

    # ---------- device clipboard ----------
    def dev_get(self):
        if self.windows and self.store:
            return self.appclip.win_get()
        code, out, _ = run(self.local.get_cmd, 10)
        return out if code == 0 else None

    def dev_set(self, text):
        if self.windows and self.store:
            return self.appclip.win_set(text)
        run(self.local.set_cmd, 10, stdin=text.encode())

    # ---------- synchronization (hubs that do not coordinate) ----------
    def loop(self):
        time.sleep(10)
        while True:
            try:
                lead = self.leading()
                if self.store:  # on the coordinator, Windows syncs directly with the history
                    self.store.os_sync = self.windows and lead
                s = self.state()
                if not lead and s.get("sync") and (self.local or (self.windows and self.store)):
                    v = self.master("GET", "/api/clip")
                    top = (v.get("items") or [None])[0]
                    if top and top["id"] != s.get("last_id"):  # something new on the network → to the device
                        if top["text"] != s.get("last_local"):
                            self.dev_set(top["text"])
                        s.update(last_id=top["id"], last_local=top["text"])
                    if self.can_read:  # what is copied on the device → to the network
                        out = self.dev_get()
                        if out and out.strip() and out != s.get("last_local"):
                            item = self.master("POST", "/api/clip", {"text": out, "from": self.ctx["name"]})
                            s.update(last_id=item.get("id"), last_local=out)
                    self.save(s)
            except Exception:
                pass
            time.sleep(3)

    # ---------- contract (the same on every hub) ----------
    def own(self, handler):
        """Handle it here: either if it coordinates, or if the request already comes forwarded by another hub (no loops)."""
        return self.store and (self.leading() or (handler and handler.headers.get("X-Hub-From")))

    def get(self, path, query):
        if path != "/api/clip":
            return None
        h = getattr(self.ctx["req"], "handler", None)
        if self.own(h):
            return {**self.store.view(), "sync_label": "Sync with this device", "where": self.ctx["name"]}
        try:
            v = self.master("GET", "/api/clip")
        except Exception:
            if not self.store:
                raise
            return {**self.store.view(), "sync": False, "sync_label": "Local copy (coordinator offline)",
                    "where": self.ctx["name"], "degraded": True}
        v["sync"] = self.state().get("sync", True)  # the switch belongs to this device
        v["sync_label"] = "Sync with this device"
        return v

    def post(self, path, body, query, handler):
        if not path.startswith("/api/clip"):
            return None
        if self.own(handler) or (self.store and not self._reachable()):
            s = self.store
            if path == "/api/clip":
                return s.add(body["text"], body.get("from") or handler.headers.get("Tailscale-User-Login") or "local",
                             push_to_windows=s.os_sync)
            if path == "/api/clip/delete":
                s.delete(body["id"])
                return s.view()
            if path == "/api/clip/clear":
                s.clear()
                return s.view()
            if path == "/api/clip/policy":  # retention for the whole network (replicated with the history)
                s.set_policy(body.get("max_items"), body.get("max_age"))
                return s.view()
            if path == "/api/clip/sync":
                if self.leading():
                    s.set_sync(body["on"])
                    return s.view()
            else:
                return None
        if path == "/api/clip/sync":
            s = self.state()
            s["sync"] = bool(body.get("on"))
            self.save(s)
            return self.get("/api/clip", {})
        if path == "/api/clip":
            return self.master("POST", "/api/clip", {"text": body.get("text", ""), "from": body.get("from") or self.ctx["name"]})
        if path in ("/api/clip/delete", "/api/clip/clear", "/api/clip/policy"):
            return self.master("POST", path, body)

    def _reachable(self):
        c = self.mesh.coordinator()
        return bool(c) and c != self.ctx["name"] and self.mesh.is_alive(c)
