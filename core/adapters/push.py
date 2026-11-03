"""Push notifications (capability "push") and the common notification channel of all adapters.

- With HTTPS (tailscale serve) and pywebpush installed: own Web Push (per-device subscriptions
  with a per-event switch; reuses the app's push.py). The VAPID key and the subscriptions are
  replicated among the capable hubs of the mesh, so any of them notifies the same
  devices if the coordinator goes down.
- Without that (Android over HTTP, a Pi without pywebpush): events are forwarded to the coordinator
  (`api/notify/relay`). If no capable hub responds, they stay queued (outbox.json) and go out as
  soon as one comes back.
The other adapters notify with ctx["notify"](event, title, body, url).
"""
from __future__ import annotations

import json
import threading
import time

from . import Adapter

OUTBOX_MAX, OUTBOX_TTL = 50, 24 * 3600


class Impl(Adapter):
    name = "push"
    caps = ["push"]

    def _claim_sub(self):
        """Stable https contact URL for the VAPID `sub` claim, derived from config."""
        conf = self.ctx.get("config") or {}
        for value in (conf.get("url"), conf.get("self_url")):
            if isinstance(value, str) and value.startswith("https://"):
                return value
        domain = conf.get("domain")
        if domain:
            return f"https://{str(self.ctx['name']).lower()}.{domain}/hub/"
        return "https://example.invalid/hub/"

    def probe(self):
        ts = self.ctx.get("adapters_by_name", {}).get("tailscale")
        self.local = None
        self.lock = threading.Lock()
        self.outbox = self.ctx["data"] / "outbox.json"
        if ts and "serve" in ts.caps:
            try:
                from push import Push  # app module (requires pywebpush)
                self.local = Push(self.ctx["data"], node=self.ctx["name"], claim_sub=self._claim_sub())
            except Exception:
                self.local = None
        self.ctx["notify"] = self.notify  # common channel, whether or not there is local push
        self.ctx["mesh"].hooks.append(self.on_mesh)
        return self.local is not None  # the capability (Notifications page) only if there is own push

    def on_mesh(self, event):
        if event == "replica" and self.local:  # new key and subscriptions from the coordinator
            from push import Push
            self.local = Push(self.ctx["data"], node=self.ctx["name"], claim_sub=self._claim_sub())
        self.flush()

    # ---------- delivery ----------
    def deliver(self, msg) -> bool:
        if self.local:
            self.local.send(msg["event"], msg["title"], msg["body"], url=msg.get("url", "/hub/"))
            return True
        try:
            from ..server import master_post
            master_post("/api/notify/relay", {**msg, "from": self.ctx["name"]})
            return True
        except Exception:
            pass
        # relay: any capable hub of the mesh can notify, even if the coordinator is down
        try:
            mesh = self.ctx["mesh"]
            peers = mesh.capable_peers()
            for name in peers[:3]:
                if mesh.try_message(name, "notify", {**msg, "from": self.ctx["name"]}):
                    return True
            if peers:  # nobody responds right now: it stays in the relay queue (TTL 24 h)
                mesh.send_message(peers[0], "notify", {**msg, "from": self.ctx["name"]})
                return True
        except Exception:
            pass
        return False

    def queue(self, msg):
        with self.lock:
            try:
                q = json.loads(self.outbox.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                q = []
            q = (q + [{**msg, "ts": time.time()}])[-OUTBOX_MAX:]
            self.outbox.write_text(json.dumps(q, ensure_ascii=False), encoding="utf-8")

    def flush(self):
        with self.lock:
            try:
                q = json.loads(self.outbox.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            left = []
            for m in q:
                if time.time() - m.get("ts", 0) > OUTBOX_TTL:
                    continue
                if left or not self.deliver({**m, "body": m["body"] + " (delayed notification)"}):
                    left.append(m)
            if left:
                self.outbox.write_text(json.dumps(left, ensure_ascii=False), encoding="utf-8")
            else:
                self.outbox.unlink(missing_ok=True)

    def notify(self, event, title, body, url="/hub/"):
        msg = {"event": event, "title": title, "body": body, "url": url}

        def send():
            try:
                if not self.deliver(msg):
                    self.queue(msg)
            except Exception:
                pass
        threading.Thread(target=send, daemon=True).start()

    # ---------- contract (same as the PC hub) ----------
    def get(self, path, query):
        p = self.local
        if p is None:
            return None
        if path == "/api/notify":
            h = getattr(self.ctx["req"], "handler", None)
            return {"publicKey": p.public_key, "events": p.catalog(), "devices": p.devices(), "log": p.log[:10],
                    "owner": True, "user": h.headers.get("Tailscale-User-Login", "") if h else ""}
        if path.startswith("/api/notify/sub/"):
            return p.view(path.rsplit("/", 1)[-1]) or {}

    def post(self, path, body, query, handler):
        p = self.local
        if p is None:
            return None
        if path == "/api/notify/subscribe":
            return p.subscribe(body["subscription"], body.get("label", ""), handler.headers.get("Tailscale-User-Login", ""))
        if path == "/api/notify/unsubscribe":
            p.unsubscribe(body["id"])
            return {"ok": True}
        if path == "/api/notify/event":
            return p.set_event(body["id"], body["event"], body["on"])
        if path == "/api/notify/test":
            return {"sent": p.send("test", "Hub test", "If you see this, notifications work.", url="/hub/avisos.html",
                                   only=body.get("id"))}
        if path == "/api/notify/relay":  # another hub on the network asks to notify through this one
            n = p.send(body.get("event", "relay"), body.get("title", "Notification"),
                       f"{body.get('from', '?')}: {body.get('body', '')}", url=body.get("url") or "/hub/")
            return {"sent": n}
