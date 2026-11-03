"""Offer this device as an exit node for the tailnet (switch). Requires the Tailscale CLI."""
from __future__ import annotations

import json

from ..detect import run
from . import Adapter


class Impl(Adapter):
    name = "exitnode"
    caps = ["exitnode"]

    def probe(self):
        self.ts = self.ctx.get("adapters_by_name", {}).get("tailscale")
        return bool(self.ts)

    def state(self):
        code, out, _ = run(["tailscale", "debug", "prefs"], 10)
        prefs = json.loads(out.lstrip("﻿") or "{}") if code == 0 else {}
        me = self.ts.status().get("Self", {})
        return {"advertised": "0.0.0.0/0" in (prefs.get("AdvertiseRoutes") or []),
                "approved": "0.0.0.0/0" in (me.get("AllowedIPs") or []) or "0.0.0.0/0" in (me.get("PrimaryRoutes") or [])}

    def get(self, path, query):
        if path == "/api/exitnode":
            return self.state()

    def post(self, path, body, query, handler):
        if path == "/api/exitnode":
            code, out, err = run(["tailscale", "set", f"--advertise-exit-node={'true' if body.get('on') else 'false'}"], 30)
            return {**self.state(), "output": (out + err).strip(), "ok": code == 0}
