"""Taildrop from the hub (capability "taildrop"): send files to any device on the tailnet.

With the Tailscale CLI: `tailscale file cp` from this device; the destination "host" stores the file
on this same device (~/Downloads/Tailnet). Without the CLI (Android): it is delegated to the mesh
coordinator, which receives this device's identity via tailscale serve and offers the right destinations.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from ..detect import run
from . import Adapter, disk_room

SAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class Impl(Adapter):
    name = "taildrop"
    caps = ["taildrop"]
    stream_paths = ("/api/taildrop",)  # the server delivers the body without reading it

    def probe(self):
        self.ts = self.ctx.get("adapters_by_name", {}).get("tailscale")
        if self.ts:
            return "taildrop" in self.ts.caps
        return self.ctx["mesh"].has_peers()  # via the mesh coordinator

    def get(self, path, query):
        if path != "/api/taildrop/targets":
            return None
        if not self.ts:
            from ..server import master_get
            import json
            with master_get("/api/taildrop/targets", 15) as r:
                return json.load(r)
        code, out, err = run(["tailscale", "file", "cp", "--targets"], 15)
        targets = [{"ip": l.split("\t")[0], "name": l.split("\t")[1].rstrip(".").split(".")[0]}
                   for l in out.splitlines() if "\t" in l]
        # the visitor is not sent to itself; if visiting from another device, this device is a destination
        h = getattr(self.ctx["req"], "handler", None)
        ip = ((h.headers.get("X-Forwarded-For") or h.client_address[0]) if h else "").split(",")[0].strip().removeprefix("::ffff:")
        me = (self.ts.status().get("Self") or {})
        mine = me.get("TailscaleIPs") or []
        targets = [t for t in targets if t["ip"] != ip]
        visitor = {"ip": ip, "isHost": ip in mine or ip in ("127.0.0.1", "::1"), "hostName": me.get("HostName")}
        if not visitor["isHost"]:
            targets.insert(0, {"ip": "host", "name": me.get("HostName") or self.ctx["name"]})
        return {"targets": targets, "visitor": visitor, "error": None if code == 0 else (out + err).strip()}

    def stream(self, path, query, handler, length):
        """Receive the file (body) and send it via Taildrop, or forward it to the coordinator."""
        to, name = query.get("to", ""), SAFE.sub("_", query.get("name", "file")).strip(". ") or "file"
        if not self.ts:
            from ..server import master_stream
            return master_stream("POST", f"/api/taildrop?to={to}&name={name}", handler.rfile, length)
        if to != "host" and not re.fullmatch(r"[0-9a-fA-F.:]+", to):
            return ("error", 400, "invalid destination")
        folder = Path(tempfile.mkdtemp(prefix="taildrop-"))
        dest = (Path.home() / "Downloads" / "Tailnet" / name) if to == "host" else folder / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        full = disk_room(dest.parent, length)
        if full:
            handler.close_connection = True
            return ("error", 507, full)
        try:
            left = length
            with dest.open("wb") as f:
                while left > 0:
                    chunk = handler.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            if to == "host":
                return {"ok": left == 0, "output": f"saved to {dest}", "bytes": length}
            code, out, err = run(["tailscale", "file", "cp", str(dest), f"{to}:"], 600)
            return {"ok": code == 0, "output": (out + err).strip(), "bytes": length}
        finally:
            shutil.rmtree(folder, ignore_errors=True)
