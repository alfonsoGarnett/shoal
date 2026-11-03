"""Quick device actions (lock, turn off the screen) depending on the system.

Shutting down the device is a sensitive action: it only appears if the owner enables it in the
configuration (`allow_shutdown`) and it goes through the network quorum (by default, a single decision).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from ..detect import has, run
from . import Adapter

NIRCMD = "nircmd.exe"
QUORUM = {"shutdown"}


class Impl(Adapter):
    name = "pcactions"
    caps = ["pc_actions"]

    def probe(self):
        fam = self.ctx["platform"]["family"]
        conf = self.ctx["config"]
        acts = {}
        if fam == "windows":
            acts["lock"] = ("Lock this device", ["rundll32.exe", "user32.dll,LockWorkStation"])
            nircmd = conf.get("nircmd") or shutil.which("nircmd") or NIRCMD
            if Path(nircmd).is_file():
                acts["screen_off"] = ("Turn off the screen", [nircmd, "monitor", "off"])
            if conf.get("allow_shutdown"):
                acts["shutdown"] = ("Shut down this device", ["shutdown", "/s", "/t", "0"])
        elif fam == "macos":
            acts["lock"] = ("Lock this device", ["pmset", "displaysleepnow"])
            acts["screen_off"] = ("Turn off the screen", ["pmset", "displaysleepnow"])
            if conf.get("allow_shutdown"):
                acts["shutdown"] = ("Shut down this device", ["osascript", "-e", 'tell application "System Events" to shut down'])
        elif fam == "linux" and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            if has("loginctl"):
                acts["lock"] = ("Lock this device", ["loginctl", "lock-session"])
            if has("xset"):
                acts["screen_off"] = ("Turn off the screen", ["xset", "dpms", "force", "off"])
            if conf.get("allow_shutdown") and has("systemctl"):
                acts["shutdown"] = ("Shut down this device", ["systemctl", "poweroff"])
        self.acts = acts
        ap = self.ctx.get("approvals")
        if ap:
            ap.register("shutdown", self._apply)
        return bool(acts)

    def _apply(self, args):
        name = args.get("action")
        if name not in self.acts:
            return {"ok": False, "error": "unknown action"}
        code, out, err = run(self.acts[name][1], 20)
        return {"ok": code == 0, "output": (out + err).strip()}

    def get(self, path, query):
        if path == "/api/actions":
            return {"pc": {k: v[0] for k, v in self.acts.items()}}

    def post(self, path, body, query, handler):
        if path.startswith("/api/actions/pc/"):
            name = path.rsplit("/", 1)[-1]
            if name not in self.acts:
                return ("error", 404, "unknown action")
            if name in QUORUM:
                ap = self.ctx.get("approvals")
                if ap:
                    res = ap.request("shutdown", {"action": name, "hub": self.ctx["name"]},
                                     self.ctx["name"], by=handler.headers.get("Tailscale-User-Login") or "owner")
                    if not res.get("approved"):
                        return {"ok": True, "pending": res,
                                "message": "Another hub must confirm the shutdown (Control › Approvals)."}
            return self._apply({"action": name})
