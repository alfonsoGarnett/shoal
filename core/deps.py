"""Optional native dependencies, installed in the background on hubs that predate them.

Today only `cryptography` (native Ed25519, see core/crypto.py). The installers add it to new
hubs and start them with `--auto-deps`; such a hub, if it still runs the pure-Python fallback,
tries once a day to install it with the platform's own tool, then restarts so the native backend
loads. If that is not possible (no network, no pip, an externally managed Python), the hub keeps
working on the fallback.

A hub started by hand (`python -m core` from a clone) never installs anything: it only says
how to, once. Changing someone's Python environment needs their say-so.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

RETRY_AFTER = 24 * 3600


def _python() -> str:
    """The console interpreter next to sys.executable (pythonw.exe cannot run pip visibly)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return sys.executable


def _install_command():
    if "com.termux" in os.environ.get("PREFIX", ""):
        # Termux ships a prebuilt package; pip would have to compile Rust on the phone.
        return ["pkg", "install", "-y", "python-cryptography"]
    cmd = [_python(), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    if sys.prefix == sys.base_prefix:  # not a venv: user site, never the system Python
        cmd.append("--user")
    return cmd + ["cryptography"]


def _importable() -> bool:
    probe = "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey"
    try:
        return subprocess.run([_python(), "-c", probe], capture_output=True, timeout=60,
                              creationflags=0x08000000 if sys.platform == "win32" else 0).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_crypto(data: Path, log, restart) -> None:
    """Run in a background thread at startup. `restart` relaunches the hub."""
    from . import crypto

    if crypto.BACKEND != "pure-python" or os.environ.get("HUB_NO_DEPS"):
        return
    if not os.environ.get("HUB_AUTO_DEPS"):
        log("ed25519 runs on the pure-Python fallback; for the native backend: "
            + " ".join(_install_command()) + " (or start the hub with --auto-deps)")
        return
    marker = Path(data) / "deps.json"
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    last = state.get("cryptography") or {}
    if time.time() - float(last.get("ts") or 0) < RETRY_AFTER:
        return
    time.sleep(60)  # let the hub settle (and pass its update canary) first
    cmd = _install_command()
    log(f"native Ed25519 unavailable; installing cryptography ({' '.join(cmd[:4])} ...)")
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=1800,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        detail = (r.stdout + r.stderr).decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
    except (OSError, subprocess.TimeoutExpired) as e:
        detail = [repr(e)]
    ok = _importable()
    state["cryptography"] = {"ts": time.time(), "ok": ok, "detail": detail[0][:300]}
    try:
        marker.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass
    if ok:
        log("cryptography installed; restarting to load native Ed25519")
        restart()
    else:
        log(f"cryptography could not be installed ({detail[0][:200]}); keeping pure-Python Ed25519")
