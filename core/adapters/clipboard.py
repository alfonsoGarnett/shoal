"""Device clipboard: Termux, Wayland, X11, macOS or Windows (PowerShell)."""
from __future__ import annotations

import os

from ..detect import has, run
from . import Adapter


class Impl(Adapter):
    name = "clipboard"
    caps = ["clipboard_device"]

    def probe(self):
        p = self.ctx["platform"]
        if p["termux"] and has("termux-clipboard-get"):
            self.get_cmd, self.set_cmd = ["termux-clipboard-get"], ["termux-clipboard-set"]
        elif os.environ.get("WAYLAND_DISPLAY") and has("wl-paste"):
            self.get_cmd, self.set_cmd = ["wl-paste", "-n"], ["wl-copy"]
        elif os.environ.get("DISPLAY") and has("xclip"):
            self.get_cmd, self.set_cmd = ["xclip", "-o", "-selection", "clipboard"], ["xclip", "-selection", "clipboard"]
        elif p["family"] == "macos":
            self.get_cmd, self.set_cmd = ["pbpaste"], ["pbcopy"]
        elif p["family"] == "windows":
            self.get_cmd = ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"]
            self.set_cmd = ["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"]
        else:
            return False
        return True

    def get(self, path, query):
        if path == "/clip":
            code, out, err = run(self.get_cmd, 10)
            return {"text": out, "error": err.strip() or None}

    def post(self, path, body, query, handler):
        if path == "/clip":
            code, _, err = run(self.set_cmd, 10, stdin=body.get("text", "").encode())
            return {"ok": code == 0, "error": err.strip() or None}
