"""Raspberry Pi GPIO (read-only for now): pin state with `pinctrl` or `gpioinfo`."""
from __future__ import annotations

import glob

from ..detect import has, run
from . import Adapter


class Impl(Adapter):
    name = "gpio"
    caps = ["gpio"]

    def probe(self):
        return self.ctx["platform"]["raspberry_pi"] and bool(glob.glob("/dev/gpiochip*"))

    def info(self):
        if has("pinctrl"):
            return {"gpio": {"tool": "pinctrl", "pins": run(["pinctrl", "get"], 10)[1].splitlines()[:40]}}
        if has("gpioinfo"):
            return {"gpio": {"tool": "gpioinfo", "pins": run(["gpioinfo"], 10)[1].splitlines()[:60]}}
        return {"gpio": {"tool": None, "chips": glob.glob("/dev/gpiochip*")}}
