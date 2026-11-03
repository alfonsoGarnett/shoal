"""Platform detection: what kind of machine it is and what it has at hand."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# Short-lived cache for read-only CLI calls (`tailscale status --json`,
# `docker ps`): the UI, the monitor, the mesh and route probes all ask for
# the same snapshot within seconds of each other. Any command that changes
# Tailscale or Docker state clears that tool's entries.
_READ_ONLY = {
    "tailscale": {"status", "ping", "version", "ip", "whois", "netcheck"},
    "docker": {"ps", "info", "inspect", "version", "images"},
}
_cache: dict = {}
_cache_lock = threading.Lock()


def invalidate(tool=None):
    with _cache_lock:
        for key in [k for k in _cache if tool is None or k[0] == tool]:
            _cache.pop(key, None)


def _mutates(cmd) -> bool:
    if not cmd or cmd[0] not in _READ_ONLY:
        return False
    sub = [a for a in cmd[1:] if not a.startswith("-")]
    if not sub:
        return False
    if sub[0] in _READ_ONLY[cmd[0]] or (len(sub) > 1 and sub[1] == "status"):
        return False
    return True


def run_cached(cmd, timeout=10, ttl=5.0):
    """run() for read-only commands, reusing a successful result for `ttl` seconds."""
    key = tuple(cmd)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    res = run(cmd, timeout)
    if res[0] == 0:
        with _cache_lock:
            _cache[key] = (time.monotonic(), res)
    return res


def run(cmd, timeout=10, stdin=None):
    """(code, stdout, stderr). Never raises: if the command does not exist it returns 127."""
    if not _mutates(cmd):
        return _run(cmd, timeout, stdin)
    invalidate(cmd[0])
    try:
        return _run(cmd, timeout, stdin)
    finally:  # a reader may have refilled the cache while the change ran
        invalidate(cmd[0])


def _run(cmd, timeout, stdin):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, input=stdin,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return -1, "", "timed out"
    except OSError as e:
        return 126, "", str(e)


def has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def read(path, default="") -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip("\x00\n ")
    except OSError:
        return default


def detect() -> dict:
    """Machine class and peculiarities. 'family' decides the installer and the supervisor."""
    termux = "com.termux" in os.environ.get("PREFIX", "") or Path("/data/data/com.termux").exists() and has("termux-info")
    model = ""
    if termux:
        family, os_name = "android", "android"
        model = run(["getprop", "ro.product.model"], 5)[1].strip()
    elif sys.platform == "win32":
        family, os_name = "windows", "windows"
    elif sys.platform == "darwin":
        family, os_name = "macos", "macOS"
    else:
        family, os_name = "linux", "linux"
        model = read("/proc/device-tree/model") or read("/sys/class/dmi/id/product_name")
    wsl = "microsoft" in read("/proc/sys/kernel/osrelease").lower()
    pi = "raspberry pi" in model.lower()
    kind = ("raspberry-pi" if pi else "android" if termux else "wsl" if wsl else
            "server" if family == "linux" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY") else family)
    if ":" in os.environ.get("HUB_DEMO_OS", ""):  # demo mode: the made-up device's "os:kind" (labels only)
        os_name, kind = os.environ["HUB_DEMO_OS"].split(":", 1)
    return {
        "family": family, "os": os_name, "kind": kind, "model": os.environ.get("HUB_MODEL") or model or platform.node(),  # HUB_MODEL: demo mode
        "arch": platform.machine(), "python": platform.python_version(), "hostname": platform.node(),
        "termux": termux, "wsl": wsl, "raspberry_pi": pi,
        "supervisor": "termux-boot" if termux else "systemd" if has("systemctl") and Path("/run/systemd/system").exists()
        else "launchd" if family == "macos" else "task-scheduler" if family == "windows" else "none",
    }
