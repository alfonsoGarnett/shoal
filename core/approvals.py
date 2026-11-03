"""Quorum approvals for sensitive network actions (never for Funnel).

Sharing a service through Funnel must be able to be turned on and off with a single decision, so
it is left out. The rest of the sensitive actions (enabling the remote terminal, removing a hub from the network, shutting down a
machine) can require N owner machines to confirm it:

- `quorum` per action: 1 (only the requester; the usual behavior), 2 or 3.
- The request is born in the hub that handles it (counts as the first approval) and is broadcast to
  the other hubs; each one shows it in Control › Approvals. The owner approves from *another*
  hub and the approval travels back over the mesh (or by relay if that hub is not nearby).
- If there are fewer hubs online than required, the quorum is lowered automatically (it never leaves you out).
- The quorum requires distinct hubs, but the identity is still that of the tailnet owner.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path

from .crdt import newer

ACTIONS = {"terminal": "enable the remote terminal", "mesh_remove": "remove a hub from the mesh",
           "shutdown": "shut down a device"}
TTL = 600       # the request expires after 10 min
KEEP_RESULT = 300


class Approvals:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "approvals.json"
        self.lock = threading.Lock()
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        self.pendings = d.get("pendings") or {}
        self.quorum = {k: int(v) for k, v in (d.get("quorum") or {}).items() if k in ACTIONS}
        self.clock = d.get("clock") or ""
        self.name = ""
        self.executors = {}
        self.alive_count = lambda: 1  # the mesh connects this: v2 hubs online (including this one)
        self.on_change = None         # the mesh connects this: broadcast a request/result
        self.notify = None            # the mesh connects this: notify by push

    def set_me(self, name: str):
        self.name = name

    # ---------- configuration ----------
    def need(self, action: str) -> int:
        q = max(1, min(3, int(self.quorum.get(action, 1))))
        return max(1, min(q, max(1, int(self.alive_count()))))

    def set_quorum(self, values: dict, clock: str = ""):
        with self.lock:
            for k, v in (values or {}).items():
                if k in ACTIONS:
                    self.quorum[k] = max(1, min(3, int(v)))
            if clock:
                self.clock = clock
            self._save()

    def merge_quorum(self, values: dict, clock: str):
        if not clock or not values:
            return
        if not newer(clock, self.clock) and self.clock:
            return
        with self.lock:
            for k, v in values.items():
                if k in ACTIONS:
                    self.quorum[k] = max(1, min(3, int(v)))
            self.clock = clock
            self._save()

    def view(self):
        return {"quorum": {a: int(self.quorum.get(a, 1)) for a in ACTIONS}, "clock": self.clock,
                "pending": self.public()}

    def register(self, action: str, fn):
        self.executors[action] = fn

    # ---------- requests ----------
    def request(self, action: str, args: dict, origin: str, by: str = "") -> dict:
        """Returns {"approved": True} if no quorum is needed, or {"pending": id, …}."""
        n = self.need(action)
        if n <= 1:
            return {"approved": True, "need": 1}
        p = {"id": secrets.token_hex(6), "action": action, "args": args or {}, "origin": origin,
             "by": by, "ts": time.time(), "need": n, "approvals": {origin: time.time()},
             "denied": {}, "result": None}
        with self.lock:
            self.pendings[p["id"]] = p
            self._save()
        self._changed(p)
        self._notify(p)
        return {"pending": p["id"], "need": n, "approvals": [origin]}

    def approve(self, pid: str, hub: str) -> dict:
        with self.lock:
            p = self.pendings.get(pid)
            if not p:
                return {"error": "that request no longer exists"}
            if p.get("result") is not None:
                return {"result": p["result"], "done": True}
            if hub != p.get("origin"):
                p.setdefault("approvals", {})[hub] = time.time()
            need = max(1, min(int(p.get("need", 1)), max(1, int(self.alive_count()))))
            p["need"] = need
            self._save()
        done = self._ready(p)
        self._changed(p)
        if done and p.get("origin") == self.name:
            self._execute(p)
        return {"approvals": list(p.get("approvals") or []), "need": p.get("need"), "done": done}

    def deny(self, pid: str, hub: str) -> dict:
        with self.lock:
            p = self.pendings.get(pid)
            if not p:
                return {"error": "that request no longer exists"}
            p.setdefault("denied", {})[hub] = time.time()
            p["result"] = {"ok": False, "error": f"rejected by {hub}"}
            self._save()
        self._changed(p)
        return {"ok": True}

    def merge(self, remote: dict) -> bool:
        """Integrates a request or result broadcast by another hub."""
        if not isinstance(remote, dict) or not remote.get("id") or not remote.get("action"):
            return False
        with self.lock:
            p = self.pendings.get(remote["id"])
            if not p:
                self.pendings[remote["id"]] = dict(remote)
                self._save()
                return True
            for k in ("approvals", "denied"):
                for hub, ts in (remote.get(k) or {}).items():
                    cur = p.setdefault(k, {})
                    cur[hub] = min(cur[hub], ts) if hub in cur else ts
            if remote.get("result") is not None:
                p["result"] = remote["result"]
            if remote.get("need"):
                p["need"] = int(remote["need"])
            self._save()
        return True

    def public(self):
        now = time.time()
        with self.lock:
            out = []
            for p in self.pendings.values():
                if now - p.get("ts", 0) > TTL + KEEP_RESULT:
                    continue
                out.append({"id": p.get("id"), "action": p.get("action"), "args": p.get("args"),
                            "origin": p.get("origin"), "by": p.get("by"), "ts": p.get("ts"),
                            "need": p.get("need"), "done": self._ready(p),
                            "approvals": sorted((p.get("approvals") or {}).keys()),
                            "denied": sorted((p.get("denied") or {}).keys()),
                            "result": p.get("result")})
            return sorted(out, key=lambda p: -p.get("ts", 0))

    def pending(self, pid: str):
        with self.lock:
            p = self.pendings.get(pid)
            return dict(p) if p else None

    # ---------- execution ----------
    def _ready(self, p) -> bool:
        others = [h for h in (p.get("approvals") or {}) if h != p.get("origin")]
        need = max(1, min(int(p.get("need", 1)), max(1, int(self.alive_count()))))
        return len(others) >= need - 1

    def tick(self):
        """Executes what already has quorum in its origin hub and expires the old ones."""
        now = time.time()
        with self.lock:
            items = [dict(p) for p in self.pendings.values()]
        for p in items:
            if p.get("result") is None and p.get("origin") == self.name and self._ready(p):
                self._execute(p)
            if now - p.get("ts", 0) > TTL and p.get("result") is None:
                with self.lock:
                    live = self.pendings.get(p["id"])
                    if live and live.get("result") is None:
                        live["result"] = {"ok": False, "error": "expired without approvals"}
                        self._save()
                self._changed(p)
            elif p.get("result") is not None and now - p.get("ts", 0) > TTL + KEEP_RESULT:
                with self.lock:
                    self.pendings.pop(p["id"], None)
                    self._save()

    def _execute(self, p):
        fn = self.executors.get(p.get("action"))
        with self.lock:
            live = self.pendings.get(p["id"])
            if not live or live.get("result") is not None:
                return
            live["result"] = {"ok": False, "error": "no executor on this hub"}
            self._save()
        if fn:
            try:
                r = fn(live.get("args") or {})
                if isinstance(r, dict):
                    if "ok" not in r:
                        r = {**r, "ok": "error" not in r}
                else:
                    r = {"ok": True, "output": r}
            except Exception as e:
                r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            with self.lock:
                live["result"] = r
        with self.lock:
            self._save()
        self._changed(live)

    def _changed(self, p):
        if self.on_change:
            try:
                self.on_change(p)
            except Exception:
                pass

    def _notify(self, p):
        if self.notify:
            try:
                self.notify("approval", "Pending approval",
                            f"{ACTIONS.get(p['action'], p['action'])} on {p['origin']} · confirm on another hub",
                            "/hub/control.html#aprobaciones")
            except Exception:
                pass

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pendings": self.pendings, "quorum": self.quorum, "clock": self.clock},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)
