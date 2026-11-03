"""OctoPrint client used by the optional OctoPrint module.

The API key is read from the module configuration or the environment and never
leaves the hub process: the browser only receives the compact public state.
"""
import json
import threading
import time
import urllib.request
from collections import deque

from ... import tailnet  # no redirects: they would carry X-Api-Key elsewhere

POLL = 5  # seconds
MAX_SNAPSHOT = 10 << 20  # 10 MB; refuses infinite MJPEG streams by mistake


class Printer:
    def __init__(self, url, key, snapshot_url="", on_event=None, port="", baudrate=0):
        self.serial_port, self.baudrate = port, baudrate  # empty: OctoPrint keeps its saved connection
        self.url, self.key, self.snapshot_url = url.rstrip("/"), key, snapshot_url
        self.on_event = on_event or (lambda kind, title, body: None)
        self.history = deque(maxlen=360)  # 30 minutes at 5 seconds
        self.state = {"reachable": False}
        self._prev = None
        self._fails = 0
        self._down_sent = False
        self._milestones = set()
        self._stopped = False
        self.lock = threading.Lock()

    def stop(self):
        self._stopped = True

    # ---------- OctoPrint ----------
    def _get(self, path, timeout=5):
        req = urllib.request.Request(self.url + path, headers={"X-Api-Key": self.key})
        with tailnet.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    def _post(self, path, body, timeout=10):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"X-Api-Key": self.key, "Content-Type": "application/json"})
        with tailnet.urlopen(req, timeout=timeout) as r:
            return r.status

    # Actions exposed by the hub. Heating is intentionally absent: heating must
    # be done next to the printer, never from a remote dashboard.
    ACTIONS = {
        "connect": ("Connect", lambda s: s._post("/api/connection", {"command": "connect", **({"port": s.serial_port} if s.serial_port else {}), **({"baudrate": s.baudrate} if s.baudrate else {})})),
        "disconnect": ("Disconnect", lambda s: s._post("/api/connection", {"command": "disconnect"})),
        "pause": ("Pause / resume", lambda s: s._post("/api/job", {"command": "pause", "action": "toggle"})),
        "cancel": ("Cancel print", lambda s: s._post("/api/job", {"command": "cancel"})),
        "cooldown": ("Cool down", lambda s: s._post("/api/printer/command", {"commands": ["M104 S0", "M106 S0"]})),
        "motors_off": ("Release motors", lambda s: s._post("/api/printer/command", {"commands": ["M84"]})),
    }

    def action(self, name):
        if name not in self.ACTIONS:
            raise KeyError(name)
        busy = (self.public().get("state") or "") in ("Printing", "Paused", "Pausing", "Resuming")
        if name in ("motors_off", "disconnect") and busy:
            raise RuntimeError("Not while a print is running.")
        return self.ACTIONS[name][1](self)

    def snapshot(self):
        if not self.snapshot_url:
            return None
        try:
            with tailnet.urlopen(self.snapshot_url, timeout=6) as r:
                data = r.read(MAX_SNAPSHOT + 1)
                if not data or len(data) > MAX_SNAPSHOT:
                    return None
                return data, r.headers.get("Content-Type", "image/jpeg")
        except Exception:
            return None

    def poll(self):
        try:
            job = self._get("/api/job")
            conn = self._get("/api/connection").get("current", {})
            try:
                temps = self._get("/api/printer?exclude=sd").get("temperature", {}).get("tool0", {})
            except Exception:
                temps = {}  # 409 while the printer is disconnected
        except Exception as e:
            self._fails += 1
            with self.lock:
                self.state = {**self.state, "reachable": False, "error": type(e).__name__, "at": time.time()}
            if self._fails == 3 and not self._down_sent:
                self._down_sent = True
                self.on_event("octoprint_down", "OctoPrint is not responding", "OctoPrint has not responded for 15 seconds.")
            return
        if self._down_sent:
            self.on_event("octoprint_down", "OctoPrint is responding again", "OctoPrint is back online.")
        self._fails, self._down_sent = 0, False

        prog = job.get("progress") or {}
        jf = (job.get("job") or {}).get("file") or {}
        st = {
            "reachable": True,
            "at": time.time(),
            "state": job.get("state") or conn.get("state"),
            "error": job.get("error"),
            "port": conn.get("port"),
            "file": jf.get("display") or jf.get("name"),
            "completion": prog.get("completion"),
            "printTime": prog.get("printTime"),
            "printTimeLeft": prog.get("printTimeLeft"),
            "estimated": (job.get("job") or {}).get("estimatedPrintTime"),
            "filament_mm": (((job.get("job") or {}).get("filament") or {}).get("tool0") or {}).get("length"),
            "tool": {"actual": temps.get("actual"), "target": temps.get("target")},
        }
        if temps.get("actual") is not None:
            self.history.append((round(time.time()), temps.get("actual"), temps.get("target")))
        self._events(st)
        with self.lock:
            self.state = st

    def _events(self, st):
        prev, cur = (self._prev or {}).get("state") or "", st["state"] or ""
        name = st.get("file") or "the file"
        if prev and cur != prev:
            if cur == "Printing" and prev not in ("Paused", "Pausing", "Resuming"):
                self._milestones.clear()
                self.on_event("print_started", "Print started", f"{name}")
            elif cur.startswith("Error") or cur.startswith("Offline after error"):
                self.on_event("print_error", "Printer error", st.get("error") or cur)
            elif prev in ("Printing", "Finishing") and cur == "Operational":
                pc = (self._prev or {}).get("completion") or 0
                if pc >= 99.5:
                    self.on_event("print_done", "Print finished", f"{name} is ready.")
                else:
                    self.on_event("print_cancelled", "Print stopped", f"{name} stopped at {pc:.0f}%.")
            elif prev == "Cancelling" and cur == "Operational":
                self.on_event("print_cancelled", "Print cancelled", f"{name}")
        if cur == "Printing" and st.get("completion") is not None:
            for m in (25, 50, 75):
                if st["completion"] >= m and m not in self._milestones:
                    self._milestones.add(m)
                    if prev:  # do not report milestones that already passed at startup
                        left = st.get("printTimeLeft")
                        extra = f" About {round(left / 60)} min left." if left else ""
                        self.on_event("print_progress", f"Print at {m}%", f"{name}.{extra}")
        self._prev = st

    def run(self):
        while not self._stopped:
            try:
                self.poll()
            except Exception as e:  # the watcher must never die
                with self.lock:
                    self.state = {**self.state, "watch_error": repr(e)}
            time.sleep(POLL)

    def public(self):
        with self.lock:
            return dict(self.state)
