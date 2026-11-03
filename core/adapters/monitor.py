"""Device monitor (capability "monitor"): online or not and latency, 24 h, one sample per minute.

With the Tailscale CLI, it measures from this device (`tailscale ping`). Without it (Android, watches),
it shows the monitor of the mesh coordinator.
"""
from __future__ import annotations

import json
import re
import threading
import time

from ..detect import run
from . import Adapter

KEEP = 24 * 60
WINDOW = 24 * 3600  # s: what the monitor shows; older samples are dropped
PING_EVERY = 300  # s; online state is still sampled every minute from `tailscale status`
PONG = re.compile(r"pong from .* via (?P<via>\S+) in (?P<ms>[\d.]+)ms")


def prune(data: dict, now: float) -> dict:
    """Only the last 24 h, and only devices seen in them: a device that left the tailnet (or was
    renamed) gets no new samples, and its old ones made it look online at a stale percentage."""
    out = {}
    for host, h in data.items():
        samples = [s for s in h.get("samples") or [] if now - s[0] < WINDOW]
        if samples:
            out[host] = {**h, "samples": samples}
    return out


class Impl(Adapter):
    name = "monitor"
    caps = ["monitor"]

    def probe(self):
        self.ts = self.ctx.get("adapters_by_name", {}).get("tailscale")
        self.via_master = not self.ts and self.ctx["mesh"].has_peers()
        if not (self.ts or self.via_master):
            return False
        if self.ts:
            self.path = self.ctx["data"] / "monitor.json"
            self.lock = threading.Lock()
            try:
                self.data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self.data = {}
            threading.Thread(target=self.loop, daemon=True).start()
        return True

    @staticmethod
    def ping(ip):
        out = run(["tailscale", "ping", "--c", "1", "--timeout", "3s", ip], 8)[1]
        m = PONG.search(out)
        if not m:
            return None, None
        via = m["via"]
        return float(m["ms"]), ("relay " + via[5:].strip("()")) if via.startswith("DERP") else "direct"

    def loop(self):
        last = {}  # host -> (ts, ms, via, path) of the last real ping
        while True:
            try:
                st = self.ts.status()
                now = int(time.time())
                # The measuring device itself: it is never one of its own peers, so without this the
                # coordinator (whose monitor phones and watches show) was missing from every list.
                me = st.get("Self") or {}
                if me.get("HostName"):
                    with self.lock:
                        h = self.data.setdefault(me["HostName"], {"os": me.get("OS"), "samples": []})
                        h["samples"] = (h["samples"] + [[now, 1, None, "self"]])[-KEEP:]
                for p in (st.get("Peer") or {}).values():
                    if p.get("ShareeNode") or "tag:ingress" in (p.get("Tags") or []):
                        continue
                    ips = p.get("TailscaleIPs") or []
                    host = p.get("HostName")
                    # One `tailscale ping` per peer per minute was most of this loop's cost. The
                    # path (direct address or DERP relay) comes free with the status; ping again
                    # only every PING_EVERY or when that path changes.
                    path = p.get("CurAddr") or ("relay " + p["Relay"] if p.get("Relay") else "")
                    prev = last.get(host)
                    if not (p.get("Online") and ips):
                        ms, via = None, None
                    elif prev and now - prev[0] < PING_EVERY and prev[3] == path and prev[1] is not None:
                        ms, via = prev[1], prev[2]
                    else:
                        ms, via = self.ping(ips[0])
                        last[host] = (now, ms, via, path)
                    with self.lock:
                        h = self.data.setdefault(p.get("HostName"), {"os": p.get("OS"), "samples": []})
                        h["samples"] = (h["samples"] + [[now, 1 if p.get("Online") else 0, ms, via]])[-KEEP:]
                with self.lock:
                    self.data = prune(self.data, now)
                    self.path.write_text(json.dumps(self.data, separators=(",", ":")))
            except Exception:
                pass
            time.sleep(60)

    def get(self, path, query):
        if path != "/api/monitor":
            return None
        if self.via_master:
            from ..server import master_get
            with master_get("/api/monitor", 10) as r:
                return json.load(r)
        with self.lock:  # without Tailscale infrastructure nodes (funnel-ingress-node…)
            data = prune(json.loads(json.dumps(self.data)), time.time())
        return {k: v for k, v in data.items() if not k.startswith("funnel-ingress")}

    def summary(self):
        """Latest sample of each device, so the mesh can compare what each hub sees."""
        if not self.ts or not hasattr(self, "data"):
            return {}
        with self.lock:
            data = prune(json.loads(json.dumps(self.data)), time.time())
        out = {}
        for name, h in data.items():
            if name.startswith("funnel-ingress"):
                continue
            samples = h.get("samples") or []
            if not samples:
                continue
            ts, up, ms, via = samples[-1]
            out[name] = [ts, bool(up), ms, via]
        return out
