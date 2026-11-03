"""System state: CPU temperature, load, memory and uptime (Linux, Android, macOS)."""
from __future__ import annotations

import os
import time
from pathlib import Path

from ..detect import read
from . import Adapter


class Impl(Adapter):
    name = "system"
    caps = ["system"]

    def probe(self):
        return hasattr(os, "getloadavg") or Path("/proc/meminfo").exists()

    def info(self):
        out = {}
        # CPU/SoC zones only: on Android there are dozens (battery, camera, PMIC…) and some report odd values.
        temps, any_temps = [], []
        for z in Path("/sys/class/thermal").glob("thermal_zone*"):
            t, kind = read(z / "temp"), read(z / "type").lower()
            if t.lstrip("-").isdigit() and 0 < int(t) / 1000 < 120:
                any_temps.append(int(t) / 1000)
                if any(k in kind for k in ("cpu", "soc", "x86_pkg", "tsens", "cpuss", "package", "core")):
                    temps.append(int(t) / 1000)
        if temps:
            out["cpu_temp"] = round(sum(temps) / len(temps), 1)
        elif len(any_temps) == 1:  # Raspberry Pi and similar: a single zone, the SoC one
            out["cpu_temp"] = round(any_temps[0], 1)
        try:
            out["load"] = [round(x, 2) for x in os.getloadavg()]
        except (OSError, AttributeError):
            pass
        mem = {l.split(":")[0]: int(l.split()[1]) for l in read("/proc/meminfo").splitlines() if l.split()[1:2] and l.split()[1].isdigit()}
        if mem.get("MemTotal"):
            out["memory"] = {"total": mem["MemTotal"] * 1024, "available": mem.get("MemAvailable", 0) * 1024}
        up = read("/proc/uptime").split()
        if up:
            out["system_uptime"] = float(up[0])
        out["cpus"] = os.cpu_count()
        return {"system": out}
