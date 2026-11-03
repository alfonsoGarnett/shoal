"""Battery: Termux, Linux (/sys/class/power_supply), macOS (pmset) or Windows (Win32)."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from ..detect import has, read, run
from . import Adapter


class Impl(Adapter):
    name = "battery"
    caps = ["battery"]

    def probe(self):
        return self.read() is not None

    def read(self):
        p = self.ctx["platform"]
        if p["termux"] and has("termux-battery-status"):
            code, out, _ = run(["termux-battery-status"], 10)
            if code == 0:
                b = json.loads(out)
                return {"percentage": b.get("percentage"), "status": b.get("status"), "temperature": b.get("temperature"),
                        "plugged": b.get("plugged") != "UNPLUGGED"}
        if p["family"] == "linux":
            for d in sorted(Path("/sys/class/power_supply").glob("*")):
                if read(d / "type") == "Battery" and read(d / "capacity"):
                    st = read(d / "status").upper()
                    return {"percentage": int(read(d / "capacity")), "status": st or "UNKNOWN", "temperature": None,
                            "plugged": st in ("CHARGING", "FULL", "NOT CHARGING")}
        if p["family"] == "macos" and has("pmset"):
            out = run(["pmset", "-g", "batt"], 5)[1]
            m = re.search(r"(\d+)%;\s*([\w ]+);", out)
            if m:
                return {"percentage": int(m[1]), "status": m[2].strip().upper(), "temperature": None, "plugged": "AC Power" in out}
        if sys.platform == "win32":
            import ctypes

            class S(ctypes.Structure):
                _fields_ = [("ac", ctypes.c_byte), ("flag", ctypes.c_byte), ("pct", ctypes.c_byte), ("r", ctypes.c_byte),
                            ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]
            s = S()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)) and s.pct != -1 and not (s.flag & 128):
                st = "CHARGING" if s.flag & 8 else ("FULL" if s.ac == 1 and s.pct >= 95 else "PLUGGED") if s.ac == 1 else "DISCHARGING"
                return {"percentage": s.pct, "status": st, "temperature": None, "plugged": s.ac == 1}
        return None

    def info(self):
        return {"battery": self.read()}
