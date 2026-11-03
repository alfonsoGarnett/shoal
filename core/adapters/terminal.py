"""Remote terminal: one-shot commands (bash/zsh/sh or PowerShell) and, on POSIX devices, an
interactive shell on a PTY over a WebSocket (GET api/term/pty). Disabled by default.

Enabling it is a sensitive action: if the network quorum is greater than 1, the request stays
pending until another hub confirms it (Control › Approvals). Disabling it never waits, and it
closes every interactive session.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from .. import ptyhost
from ..detect import run
from ..ws import WebSocket
from . import Adapter

MARK = "__HUB_CWD__"


class Impl(Adapter):
    name = "terminal"
    caps = ["terminal"]

    def probe(self):
        p = self.ctx["platform"]
        if p["family"] == "windows":
            self.shell = "powershell"
        else:
            self.shell = next((s for s in ("bash", "zsh", "sh") if shutil.which(s)), None)
        self.pty = ptyhost.Host(self._log, self.ctx["data"] / "shell") if ptyhost.AVAILABLE and self.shell else None  # PTY or ConPTY
        if self.shell:
            ap = self.ctx.get("approvals")
            if ap:
                ap.register("terminal", self._apply)
        return bool(self.shell)

    def _apply(self, args):
        s = self.state()
        s["enabled"] = bool(args.get("on"))
        self.save(s)
        return {"exec": s["enabled"], "on": s["enabled"]}

    @property
    def state_file(self) -> Path:
        return self.ctx["data"] / "terminal.json"

    def state(self):
        try:
            d = json.loads(self.state_file.read_text())
            if not isinstance(d, dict):
                d = {}
        except (OSError, ValueError):
            d = {}
        cwd = d.get("cwd") or ""
        if not (isinstance(cwd, str) and cwd and Path(cwd).is_dir()):  # also repairs a cwd mangled by older versions
            cwd = str(Path.home())
        return {"enabled": bool(d.get("enabled", False)), "cwd": cwd}

    def save(self, s):
        self.state_file.write_text(json.dumps(s))
        if not s["enabled"] and self.pty:
            self.pty.close_all()

    def _log(self, who, text):
        with (self.ctx["data"] / "terminal.log").open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{who}\t-\t[{text}]\n")

    def info(self):
        s = self.state()
        return {"exec": s["enabled"], "shell": self.shell, "pty": bool(self.pty)}

    def get(self, path, query):
        if path == "/api/term":
            s = self.state()
            # "relay": this hub carries the interactive shell of other hubs (api/term/pty?node=)
            return {**s, "host": self.ctx["name"], "shell": self.shell, "pty": bool(self.pty), "relay": True}

    def websocket(self, handler, query):
        """GET api/term/pty: an interactive shell. The server has checked the owner and the Origin."""
        if not self.pty:
            return handler._json({"error": "no interactive terminal on this device"}, 404)
        if not self.state()["enabled"]:
            return handler._json({"error": "The remote terminal is disabled on this device."}, 409)
        ws = WebSocket.accept(handler)
        if ws is None:
            return handler._json({"error": "a WebSocket upgrade is required"}, 400)
        who = handler.headers.get("Tailscale-User-Login") or handler.headers.get("X-Hub-User") or "owner"
        self.pty.serve(ws, query, self.shell, str(Path.home()), who)

    def post(self, path, body, query, handler):
        if path in ("/exec/enable", "/api/term/enable"):
            on = bool(body.get("on"))
            if on:
                ap = self.ctx.get("approvals")
                if ap:
                    res = ap.request("terminal", {"on": True, "hub": self.ctx["name"]},
                                     self.ctx["name"], by=handler.headers.get("Tailscale-User-Login") or "owner")
                    if not res.get("approved"):
                        s = self.state()
                        return {"exec": s["enabled"], "enabled": s["enabled"], "cwd": s["cwd"],
                                "pending": res,
                                "message": "Another hub must confirm the activation (Control › Approvals)."}
            s = self.state()
            s["enabled"] = on
            self.save(s)
            return {"exec": s["enabled"], "enabled": s["enabled"], "cwd": s["cwd"]}
        if path in ("/exec", "/api/term/exec"):
            s = self.state()
            if not s["enabled"]:
                return ("error", 409, "The remote terminal is disabled on this device.")
            cmd, t0 = body.get("cmd", ""), time.time()
            # The command's own stderr is merged into stdout in order; the trailing marker carries
            # the command's exit code and the new working directory.
            if self.shell == "powershell":
                cwd = s["cwd"].replace("'", "''")
                script = (f"Set-Location -LiteralPath '{cwd}'; $global:LASTEXITCODE = 0; $Error.Clear(); "
                          f"& {{ {cmd} }} 2>&1 | Out-String -Width 200; "
                          f"$c = if ($LASTEXITCODE) {{ $LASTEXITCODE }} elseif ($Error.Count) {{ 1 }} else {{ 0 }}; "
                          f"Write-Output ('{MARK}' + $c + ':' + (Get-Location).Path)")
                code, out, err = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], 120)
            else:
                script = (f'cd {json.dumps(s["cwd"])} 2>/dev/null\n{{ {cmd}\n}} 2>&1\n'
                          f'printf "\\n{MARK}%s:%s" "$?" "$PWD"')
                code, out, err = run([self.shell, "-c", script], 120)
            if MARK in out:
                out, _, tail = out.rpartition(MARK)
                status, _, new = tail.strip().partition(":")
                if status.lstrip("-").isdigit():
                    code = int(status)
                if new and Path(new).is_dir():
                    s["cwd"] = new
                    self.save(s)
            out += err  # only the shell's own errors land here (for example a syntax error)
            with (self.ctx["data"] / "terminal.log").open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{handler.headers.get('X-Hub-User') or handler.headers.get('Tailscale-User-Login') or '?'}\t{code}\t{cmd}\n")
            return {"output": out.rstrip() + "\n", "code": code, "cwd": s["cwd"], "secs": round(time.time() - t0, 2)}
