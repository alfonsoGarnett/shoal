"""Demo mode: a whole Shoal mesh on this machine, with made-up devices and services.

    python tools/demo.py [--base-port 8890] [--seconds 0]

Four hubs (Atlas, Juniper, Kestrel, Pixel) run on 127.0.0.1, each with its own state in a
temporary folder, a fake `tailscale status` (via HUB_DEMO_TAILSCALE) and a few tiny web services,
so the index, the live mesh map, the clipboard and the terminal can be shown or recorded without a
single real name, address or file. Nothing here talks to Tailscale or to your real hubs: the hubs
start with a PATH that has no Tailscale CLI and a fake tailnet domain, a made-up home folder, and
without the adapters that would touch this machine (its clipboard, notifications, lock screen, exit node). The
remote terminal stays off; switching it on opens a real shell of this machine in that fake home.
Ctrl+C stops everything.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
DOMAIN = "demo-tailnet.ts.net"
# name, os label, priority, services (port offset, name, description, Phosphor icon)
MODELS = {"Atlas": "Home server", "Juniper": "MacBook Air", "Kestrel": "Raspberry Pi 5", "Pixel": "Pixel 9"}
# what each hub says it runs on (labels only), and the adapters it does without: no battery on the server
# and the Pi, and the phone cannot coordinate (no Tailscale CLI on Android, so no push either)
DEMO_OS = {"Atlas": "linux:server", "Juniper": "macOS:macos", "Kestrel": "linux:raspberry-pi",
           "Pixel": "android:android"}
OFF = {"Atlas": ",battery", "Juniper": "", "Kestrel": ",battery", "Pixel": ",push"}
HUBS = [
    ("Atlas", "linux", 100, [(11, "Jellyfin", "Movies and shows for the whole house.", "ph-film-strip"),
                              (12, "Grafana", "Power, temperature and network graphs.", "ph-chart-line")]),
    ("Juniper", "macOS", 50, [(13, "Paperless", "Scanned letters, searchable.", "ph-files")]),
    ("Kestrel", "linux", 60, [(14, "Home Assistant", "Lights, sensors and the front door.", "ph-house-line")]),
    ("Pixel", "android", 10, []),
]
OTHER_DEVICES = [("nas", "linux", True), ("printer-cam", "linux", True), ("old-laptop", "windows", False)]
SAMPLE_FILES = {"Documents/notes.md": "# Notes\n\n- renew the domain\n- back up the NAS\n",
                "Documents/recipes/pancakes.txt": "flour, milk, eggs\n", "Pictures/.keep": "",
                "Downloads/shoal-demo.txt": "Hello from the demo.\n"}
CLIPS = ["ssh atlas.demo-tailnet.ts.net", "Wi-Fi for guests: correct-horse-battery", "https://example.com/recipe/42"]


def fake_ip(i):
    return ".".join(str(x) for x in (100, 64 + i, 10, 10 + i))


def status_for(me: int) -> dict:
    """What `tailscale status --json` would say on hub number `me`."""
    devices = [(h[0], h[1], True) for h in HUBS] + OTHER_DEVICES

    def node(i, name, os_, online):
        return {"HostName": name, "DNSName": f"{name.lower()}.{DOMAIN}.", "OS": os_,
                "TailscaleIPs": [fake_ip(i)], "Online": online, "LastSeen": "", "CurAddr": "" if i % 2 else "direct"}
    peers = {f"n{i}": node(i, *d) for i, d in enumerate(devices) if i != me}
    return {"BackendState": "Running", "Self": node(me, *devices[me]), "Peer": peers,
            "CurrentTailnet": {"Name": DOMAIN, "MagicDNSSuffix": DOMAIN}}


class Service(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"<!doctype html><title>{self.server.title}</title><h1>{self.server.title}</h1><p>demo service</p>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET

    def log_message(self, *a):
        pass


def get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-port", type=int, default=8890, help="hubs listen on base, base+1, …; services on base+11…")
    ap.add_argument("--seconds", type=float, default=0, help="stop after this long (0 = until Ctrl+C)")
    a = ap.parse_args()
    # On Windows the temp folder sits inside the user's profile, so its path would show the account
    # name in the terminal and the file browser: use the shared Public folder when possible.
    public = Path(os.environ.get("PUBLIC", "")) if os.name == "nt" else None
    parent = str(public) if public and public.is_dir() and os.access(public, os.W_OK) else None
    root = Path(tempfile.mkdtemp(prefix="shoal-demo-", dir=parent))
    def has_cli(d):
        try:
            return (Path(d) / ("tailscale.exe" if os.name == "nt" else "tailscale")).exists()
        except OSError:
            return False
    no_cli = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if p and not has_cli(p))
    procs, servers = [], []
    try:
        for _, _, _, svcs in HUBS:
            for off, title, _, _ in svcs:
                s = ThreadingHTTPServer(("127.0.0.1", a.base_port + off), Service)
                s.title = title
                threading.Thread(target=s.serve_forever, daemon=True).start()
                servers.append(s)
        urls = []
        for i, (name, _, prio, svcs) in enumerate(HUBS):
            home, port = root / name, a.base_port + i
            home.mkdir()
            fake_home = home / "home"
            for rel, text in SAMPLE_FILES.items():
                (fake_home / rel).parent.mkdir(parents=True, exist_ok=True)
                (fake_home / rel).write_text(text, encoding="utf-8")
            url = f"http://127.0.0.1:{port}/hub/"
            urls.append(url)
            (home / "status.json").write_text(json.dumps(status_for(i)), encoding="utf-8")
            conf = {"name": name, "url": url, "priority": prio,
                    "routes": [{"port": a.base_port + off, "path": "/", "name": t, "about": d, "icon": ic}
                               for off, t, d, ic in svcs]}
            if i == 0:
                conf["domain"] = DOMAIN
            (home / "config.json").write_text(json.dumps(conf), encoding="utf-8")
            env = {**os.environ, "PATH": no_cli, "HUB_DEMO_TAILSCALE": str(home / "status.json"),
                   "HUB_CORE_HOME": str(home), "HUB_PORT": str(port), "HUB_NAME": name, "HUB_BIND": "127.0.0.1",
                   "HUB_NO_RESTART": "1", "HUB_RELEASE_WRITE": "0", "HUB_OK_GRACE": "1", "HUB_UPDATE_GRACE": "99999",
                   "HUB_MESH_EVERY": "4", "HUB_MESH_ALIVE": "20", "HUB_MESH_ANTI": "1", "HUB_PRIORITY": str(prio),
                   "HUB_DISABLE": "clipboard,pcactions,exitnode,docker,notify" + OFF[name], "HUB_MODEL": MODELS[name],
                   "HUB_DEMO_OS": DEMO_OS[name],
                   "HOME": str(fake_home), "USERPROFILE": str(fake_home)}  # files and terminal start there
            if i:  # join the first hub's mesh, as a new device would
                subprocess.run([sys.executable, "-m", "core", "--home", str(home), "--port", str(port),
                                "--bind", "127.0.0.1", "--name", name, "--enroll", urls[0]],
                               cwd=APP, env=env, capture_output=True, timeout=60)
            log = (home / "hub.log").open("w", encoding="utf-8")
            procs.append(subprocess.Popen([sys.executable, "-m", "core"], cwd=APP, env=env, stdout=log, stderr=subprocess.STDOUT))
            for _ in range(60):
                try:
                    get(url + "api/caps")
                    break
                except Exception:
                    time.sleep(0.5)
        for _ in range(60):  # until every hub sees the others
            try:
                if sum(h["online"] for h in get(urls[0] + "api/fleet")["hubs"]) >= len(HUBS):
                    break
            except Exception:
                pass
            time.sleep(1)
        for c in CLIPS:
            try:
                post(urls[0] + "api/clip", {"text": c, "from": "Juniper"})
            except Exception:
                pass
        print("Shoal demo mesh is up (fake devices, nothing real):", flush=True)
        for (name, *_), url in zip(HUBS, urls):
            print(f"  {name:8} {url}", flush=True)
        print(f"state in {root}; Ctrl+C to stop", flush=True)
        end = time.time() + a.seconds if a.seconds else None
        while end is None or time.time() < end:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        for s in servers:
            s.shutdown()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
