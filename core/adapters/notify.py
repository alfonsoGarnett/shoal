"""Device notifications: Termux (notification, toast, vibration), notify-send, macOS or Windows."""
from __future__ import annotations

import os
import subprocess

from ..detect import has, run
from . import Adapter


class Impl(Adapter):
    name = "notify"

    def probe(self):
        p = self.ctx["platform"]
        if p["termux"] and has("termux-notification"):
            self.mode, self.caps = "termux", ["notify", "toast", "vibrate"]
        elif has("notify-send") and p["family"] == "linux":
            self.mode, self.caps = "notify-send", ["notify"]
        elif p["family"] == "macos" and has("osascript"):
            self.mode, self.caps = "osascript", ["notify"]
        elif p["family"] == "windows" and has("powershell"):
            self.mode, self.caps = "windows", ["notify"]
        else:
            return False
        return True

    def post(self, path, body, query, handler):
        title, text = body.get("title", "Hub"), body.get("body", body.get("text", ""))
        if path == "/notify":
            if self.mode == "termux":
                cmd = ["termux-notification", "--title", title, "--content", text, "--id", body.get("tag", "hub"), "--group", "hub"]
            elif self.mode == "notify-send":
                cmd = ["notify-send", title, text]
            elif self.mode == "windows":
                return self._windows(title, text)
            else:
                cmd = ["osascript", "-e", f'display notification {text!r} with title {title!r}'.replace("'", '"')]
            code, _, err = run(cmd, 15)
            return {"ok": code == 0, "error": err.strip() or None}
        if path == "/toast" and self.mode == "termux":
            return {"ok": run(["termux-toast", body.get("text", "")], 10)[0] == 0}
        if path == "/vibrate" and self.mode == "termux":
            return {"ok": run(["termux-vibrate", "-d", str(int(body.get("ms", 400)))], 10)[0] == 0}

    @staticmethod
    def _windows(title, text):
        """An Action Center notification from the user's session, through WinRT. The text goes in
        through the environment, never into the script: nothing to escape or inject."""
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
            "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$t = $x.GetElementsByTagName('text');"
            "$t.Item(0).AppendChild($x.CreateTextNode($env:HUB_TITLE)) > $null;"
            "$t.Item(1).AppendChild($x.CreateTextNode($env:HUB_TEXT)) > $null;"
            "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
            "[Windows.UI.Notifications.ToastNotification]::new($x))"
        )
        env = {**os.environ, "HUB_TITLE": str(title)[:200], "HUB_TEXT": str(text)[:1000]}
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                               capture_output=True, text=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "error": type(e).__name__}
        return {"ok": r.returncode == 0, "error": r.stderr.strip()[:300] or None}
