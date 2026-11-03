"""Tailscale CLI: tailnet status, `tailscale serve` routes, Taildrop and exit node."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..detect import has, run, run_cached
from . import Adapter


# Demo mode (tools/demo.py): a JSON file stands in for `tailscale status --json`, with made-up
# devices, so a whole mesh can run on one machine without touching a real tailnet.
DEMO = os.environ.get("HUB_DEMO_TAILSCALE")


class Impl(Adapter):
    name = "tailscale"

    def probe(self):
        if DEMO:  # "serve" too, so the demo has push, a coordinator and the shared clipboard
            self.caps = ["tailscale", "serve"]
            return True
        if not has("tailscale"):
            return False
        code, out, _ = run(["tailscale", "status", "--json"], 10)
        if code != 0:
            return False
        st = json.loads(out.lstrip("﻿") or "{}")
        self.caps = ["tailscale", "taildrop", "exitnode"]
        # serve: permission is required (root or --operator); it is checked when reading the configuration
        c2, _, _ = run(["tailscale", "serve", "status", "--json"], 10)
        if c2 == 0:
            self.caps.append("serve")
        return st.get("BackendState") == "Running"

    def status(self) -> dict:
        if DEMO:
            return json.loads(Path(DEMO).read_text(encoding="utf-8"))
        code, out, _ = run_cached(["tailscale", "status", "--json"], 10)
        return json.loads(out.lstrip("﻿") or "{}") if code == 0 else {}

    def serve_status(self) -> dict:
        if DEMO:
            return {}
        code, out, _ = run_cached(["tailscale", "serve", "status", "--json"], 10)
        return json.loads(out.lstrip("﻿") or "{}") if code == 0 else {}
