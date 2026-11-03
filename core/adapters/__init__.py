"""Adapters: each one contributes standard capabilities when the device supports them.

To add support for something new (GPIO, Home Assistant, a camera, ...), create a
module here with a class that inherits from Adapter and add it to the loader. The
adapter runs only where probe() returns True.

Standard capability names (the UI knows how to render them):
  tailscale serve taildrop exitnode battery clipboard_device notify toast vibrate files
  terminal system gpio containers device routes services nodes mesh
"""
from __future__ import annotations

import os
import shutil


def disabled() -> set:
    """Adapters switched off by HUB_DISABLE (comma separated names), e.g. in demo mode."""
    return {x.strip() for x in os.environ.get("HUB_DISABLE", "").split(",") if x.strip()}


def disk_room(path, size: int) -> str | None:
    """None if `size` bytes fit on the disk of `path` and still leave a reserve; otherwise why not.

    The reserve (2 % of the disk, between 256 MB and 2 GB) keeps an upload from filling the SD
    card of a Raspberry Pi or a phone, where a full disk stops the hub and the system with it."""
    try:
        du = shutil.disk_usage(path)
    except OSError:
        return None  # cannot tell: let the write itself fail if it must
    reserve = min(2 << 30, max(256 << 20, du.total // 50))
    if size + reserve <= du.free:
        return None
    return f"not enough disk space: {size >> 20} MB do not fit ({du.free >> 20} MB free, {reserve >> 20} MB kept in reserve)"


class Adapter:
    name = "base"
    caps: list = []

    def __init__(self, ctx):
        self.ctx = ctx  # shared dict: platform, config, data paths

    def probe(self) -> bool:
        return False

    def info(self) -> dict:
        return {}

    # Adapter-specific routes: return None when the path is not yours.
    def get(self, path: str, query: dict):
        return None

    def post(self, path: str, body: dict, query: dict, handler):
        return None


def load_all(ctx) -> list:
    """Order matters: adapters that depend on others (clipshare -> clipboard) load later."""
    from . import (battery, clipboard, clipshare, docker, exitnode, files, funnel, gpio, mesh, monitor, notify,
                   pcactions, push, services, system, taildrop, tailscale, terminal)
    active = []
    ctx["adapters_by_name"] = {}
    # push before mesh: it provides ctx["notify"] for modules and adapters
    off = disabled()
    for mod in (tailscale, battery, clipboard, notify, files, terminal, system, gpio, push, services, docker,
                clipshare, monitor, taildrop, exitnode, pcactions, funnel, mesh):
        a = mod.Impl(ctx)
        if a.name in off:
            continue
        try:
            if a.probe():
                active.append(a)
                ctx["adapters_by_name"][a.name] = a
        except Exception:
            pass
    return active
