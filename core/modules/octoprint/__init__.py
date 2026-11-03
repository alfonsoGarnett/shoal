"""Optional OctoPrint module: printer state, temperature history, camera and safe actions."""
from __future__ import annotations

import os
import threading

from .. import Module
from .printer import Printer


class Impl(Module):
    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.p = None
        self._thread = None

    @staticmethod
    def legacy_config(ctx):
        """Adopts the printer block of an old config.json (or the old environment variables)."""
        legacy = (ctx.get("config") or {}).get("printer") or {}
        key = legacy.get("api_key") or os.environ.get("OCTOPRINT_KEY", "")
        if not key:
            return None
        return {
            "enabled": True,
            "url": legacy.get("octoprint_url") or os.environ.get("OCTOPRINT_URL", "http://127.0.0.1:5000"),
            "api_key": key,
            "snapshot_url": legacy.get("snapshot_url") or os.environ.get("SNAPSHOT_URL", ""),
            "port": legacy.get("port") or os.environ.get("PRINTER_PORT", ""),
            "baudrate": int(legacy.get("baudrate") or os.environ.get("PRINTER_BAUD", 0) or 0),
        }

    def blocks_stop(self, control):
        """Stopping OctoPrint's own service mid-print would ruin the print."""
        if not self.p or not ("OctoPrint" in str(control.get("windows_service", "")) or control.get("printer")):
            return None
        if (self.p.public().get("state") or "") in ("Printing", "Paused", "Pausing", "Resuming", "Cancelling"):
            return "A print is in progress: stopping OctoPrint would interrupt it."
        return None

    def probe(self):
        if not self.config.get("enabled"):
            return False
        key = self.config.get("api_key") or os.environ.get("OCTOPRINT_KEY")
        if not key:
            self.last_error = "API key is not configured"
            return False
        url = self.config.get("url") or os.environ.get("OCTOPRINT_URL", "http://127.0.0.1:5000")
        notify = self.ctx.get("notify") or (lambda *a, **k: None)
        self.p = Printer(
            url,
            key,
            self.config.get("snapshot_url") or os.environ.get("SNAPSHOT_URL", ""),
            on_event=lambda kind, title, body: notify(kind, title, body, "/hub/modules/octoprint/panel.html"),
            port=self.config.get("port") or os.environ.get("PRINTER_PORT", ""),
            baudrate=int(self.config.get("baudrate") or os.environ.get("PRINTER_BAUD", 0) or 0),
        )
        return True

    def start(self):
        self._thread = threading.Thread(target=self.p.run, daemon=True, name="octoprint")
        self._thread.start()
        self.active = True

    def stop(self):
        if self.p:
            self.p.stop()
        self.active = False

    def status(self):
        state = self.p.public() if self.p else {}
        if not self.config.get("enabled"):
            return {"ok": False, "state": "disabled", "detail": "Module disabled"}
        if not self.p:
            return {"ok": False, "state": "needs_config", "detail": self.last_error or "Not configured"}
        if state.get("reachable"):
            return {"ok": True, "state": "online", "detail": state.get("state") or ""}
        return {"ok": False, "state": "offline", "detail": state.get("error") or "OctoPrint is offline"}

    def get(self, path, query):
        if path == "/api/modules/octoprint/state":
            return self.p.public() if self.p else {"reachable": False}
        if path == "/api/modules/octoprint/history":
            return {"points": list(self.p.history) if self.p else []}
        if path == "/api/modules/octoprint/snapshot":
            snap = self.p.snapshot() if self.p else None
            return ("bytes", snap[0], snap[1]) if snap else ("empty",)
        if path == "/api/modules/octoprint/actions":
            return {"actions": [{"id": k, "label": v[0]} for k, v in Printer.ACTIONS.items()]}
        return None

    def post(self, path, body, query, handler):
        prefix = "/api/modules/octoprint/actions/"
        name = path[len(prefix):] if path.startswith(prefix) else ""
        if not name:
            return None
        if not self.p:
            return ("error", 409, "OctoPrint is not configured")
        try:
            self.p.action(name)
            return {"ok": True}
        except KeyError:
            return ("error", 404, "Unknown action")
        except RuntimeError as e:
            return ("error", 409, str(e))
        except Exception as e:
            return ("error", 502, f"{type(e).__name__}: {e}")
