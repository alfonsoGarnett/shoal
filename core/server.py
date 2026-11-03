"""Universal core server: common hub contract plus detected adapters.

Data lives in ~/.hub-core (or HUB_CORE_HOME): config.json (name, seeds, extra routes),
directory.json (the mesh), terminal.json/log. The code (core/, site/, ...) lives in the app
folder and updates itself from the mesh hub with the newest signed release (core/mesh.py).
After an update the process exits with code 3 and its supervisor (systemd, launchd,
Termux:Boot, scheduled task) starts it again.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import adapters as adapters_pkg
from .adapters import disk_room
from .modules import static_file
from . import crypto, deps, tailnet, ws
from .approvals import Approvals
from .blobs import Blobs
from .detect import detect, run
from .identity import AUTH_HEADER, Identity, Pins, ub64
from .modules import ModuleRegistry
from .peering import Peering

APP = (
    Path(__file__).resolve().parents[1]
)  # app folder (core/, site/, federation.py, ...)
SITE = APP / "site"
DATA = Path(os.environ.get("HUB_CORE_HOME") or Path.home() / ".hub-core")
CONF = DATA / "config.json"
PORT = int(os.environ.get("HUB_PORT", "8790"))
MAX_JSON = 64 << 20  # API bodies; files, blobs and proxied uploads are streamed instead
BIND = os.environ.get("HUB_BIND", "0.0.0.0")
TAILNET = [
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
]
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript",
    ".css": "text/css",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".ps1": "text/plain; charset=utf-8",
    ".sh": "text/plain; charset=utf-8",
    ".py": "text/plain; charset=utf-8",
}
sys.path.insert(0, str(APP))
import federation  # noqa: E402  (fingerprint, RELEASE and bundle shared by the whole mesh)
from .mesh import PROTOCOL, Mesh  # noqa: E402

START = time.time()
CORE = None  # running Core (set by Core.__init__)


def load_conf() -> dict:
    try:
        return json.loads(CONF.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def load_release_key():
    """(seed, pub) of the author if this hub can sign releases; otherwise None."""
    try:
        d = json.loads((DATA / "release_key.json").read_text(encoding="utf-8"))
        seed, pub = ub64(d["seed"]), ub64(d["pub"])
        from . import crypto

        if crypto.publickey(seed) == pub:
            return seed, pub
    except Exception:
        pass
    return None


def save_conf(c: dict):
    DATA.mkdir(parents=True, exist_ok=True)
    CONF.write_text(json.dumps(c, indent=1), encoding="utf-8")
    try:
        os.chmod(CONF, 0o600)
    except OSError:
        pass


def resolve_master(c: dict):
    """Compatibility shim: the mesh already resolves directory *.ts.net names (mesh.install_resolver)."""


def master_get(path, timeout=15, token=True):
    """GET to the mesh coordinator (or another capable hub if it does not answer). Legacy name."""
    return CORE.mesh.call(path, timeout=timeout)


def master_post(path, body: dict, timeout=15):
    with CORE.mesh.call(
        path,
        "POST",
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
        timeout,
    ) as r:
        return json.load(r)


def master_stream(method, path, rfile, length, timeout=600):
    """Forward a large body (for example a file) to the coordinator without loading it all in memory."""
    import http.client
    import ssl

    m = CORE.mesh
    name = m.coordinator()
    if not name or name == m.me:
        raise ConnectionError("no coordinator online")
    try:
        u = urllib.parse.urlparse(m.base(name) + "/")
    except KeyError:
        raise ConnectionError("the coordinator has no reachable URL") from None
    conn = (
        http.client.HTTPSConnection(
            u.hostname,
            u.port or 443,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        if u.scheme == "https"
        else http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    )
    try:
        conn.putrequest(method, u.path.rstrip("/") + path)
        conn.putheader("Content-Length", str(length))
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("X-Hub-From", m.me)
        from .identity import AUTH_HEADER

        conn.putheader(
            AUTH_HEADER, CORE.identity.token(method, path.split("?", 1)[0], "stream")
        )
        conn.endheaders()
        left = length
        while left > 0:
            chunk = rfile.read(min(left, 1 << 20))
            if not chunk:
                break
            conn.send(chunk)
            left -= len(chunk)
        r = conn.getresponse()
        data = json.loads(r.read() or b"{}")
        return (
            data
            if r.status < 400
            else (
                "error",
                r.status,
                data.get("error") or data.get("output") or f"HTTP {r.status}",
            )
        )
    finally:
        conn.close()


NO_WINDOW = 0x08000000


class HubServer(ThreadingHTTPServer):
    """A client that leaves mid-response (a closed tab, a phone switching networks) is routine;
    socketserver would print a full traceback for each one."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                                          TimeoutError)):
            return
        super().handle_error(request, client_address)


def _run_ts(*args, timeout=30):
    """Run the tailscale CLI and return (code, combined output)."""
    kw = {"creationflags": NO_WINDOW} if sys.platform == "win32" else {}
    from .detect import _mutates, invalidate
    changes = _mutates(["tailscale", *args])
    try:
        r = subprocess.run(
            ["tailscale", *args], capture_output=True, timeout=timeout, **kw
        )
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)
    finally:
        if changes:
            invalidate("tailscale")


def _ts_self():
    code, out = _run_ts("status", "--json")
    if code != 0:
        raise RuntimeError(out.splitlines()[0] if out else "tailscale status failed")
    status = json.loads(out)
    own = status.get("Self") or {}
    dns = (own.get("DNSName") or "").rstrip(".")
    host = (own.get("HostName") or "").strip()
    if not dns:
        raise RuntimeError("Tailscale did not report a DNS name")
    return status, own, dns, host


def _slugify(name):
    raw = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    raw = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    return raw[:63].strip("-")


def _serve_snapshot():
    """Capture every HTTPS Serve handler so a hostname change can recreate it."""
    code, out = _run_ts("serve", "status", "--json")
    if code != 0:
        raise RuntimeError(out.splitlines()[0] if out else "Serve status failed")
    data = json.loads(out)
    routes = []
    for hostport, cfg in (data.get("Web") or {}).items():
        try:
            port = int(str(hostport).rsplit(":", 1)[-1])
        except ValueError:
            continue
        for path, handler in (cfg.get("Handlers") or {}).items():
            target = handler.get("Proxy")
            if target:
                routes.append({"port": port, "path": path, "target": target})
    return routes


def _serve_restore(routes):
    """Recreate Serve routes after the machine hostname (and certificate) changed."""
    code, out = _run_ts("serve", "reset")
    if code != 0:
        raise RuntimeError(out.splitlines()[0] if out else "Serve reset failed")
    for item in sorted(routes, key=lambda r: (r["port"], r["path"] != "/", r["path"])):
        args = ["serve", "--bg", f"--https={item['port']}"]
        if item.get("path") not in (None, "", "/"):
            args.append(f"--set-path={item['path']}")
        args.append(item["target"])
        code, out = _run_ts(*args)
        if code != 0:
            raise RuntimeError(out.splitlines()[0] if out else "Serve restore failed")


def service_info(family: str) -> dict:
    """How this hub starts at boot, for the Help page's copy-paste commands."""
    if family == "windows":
        for name in ("Shoal", "TailscaleHub"):
            code, out, _ = run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue).Actions.WorkingDirectory"], 15)
            if code == 0 and out.strip() and Path(out.strip()).resolve() == APP.resolve():
                return {"kind": "task", "name": name}
        return {"kind": "none", "name": ""}
    if family == "linux":
        for name in ("shoal", "tailnet-hub"):
            if Path(f"/etc/systemd/system/{name}.service").exists():
                return {"kind": "systemd", "name": name}
        return {"kind": "none", "name": ""}
    if family == "macos":
        for name in ("dev.shoal.hub", "net.tailnet.hub"):
            if (Path.home() / "Library" / "LaunchAgents" / f"{name}.plist").exists():
                return {"kind": "launchd", "name": name}
        return {"kind": "none", "name": ""}
    if family == "android" and (Path.home() / ".termux" / "boot" / "hub.sh").exists():
        return {"kind": "termux", "name": "hub.sh"}
    return {"kind": "none", "name": ""}


def _wait_hub(url, seconds=25):
    target = url.rstrip("/") + "/api/hub/version"
    ctx = ssl._create_unverified_context()
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(target, timeout=2, context=ctx) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


class Core:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        self.platform = detect()
        self.conf = load_conf()
        self.req = (
            threading.local()
        )  # request in progress per thread (adapters can see who is visiting)
        self.ctx = {
            "platform": self.platform,
            "config": self.conf,
            "data": DATA,
            "req": self.req,
            "port": PORT,
            "name": os.environ.get("HUB_NAME")
            or self.conf.get("name")
            or self.platform["hostname"],
        }
        global CORE
        CORE = self  # adapters (mesh, Funnel) read hub data
        self.owners = set()
        self.identity = Identity(DATA)
        self.identity.name = self.ctx["name"]
        self.pins = Pins(DATA)
        self.release_key = load_release_key()  # only the author device signs releases
        self.blobs = Blobs(DATA)
        self.peering = Peering(DATA)
        self.approvals = Approvals(DATA)
        self.ctx.update(
            identity=self.identity,
            pins=self.pins,
            blobs=self.blobs,
            peering=self.peering,
            approvals=self.approvals,
        )
        self.mesh = self.ctx["mesh"] = Mesh(self)
        self.mesh.check_update_health()  # if the new version did not boot, restore and exit
        self.ctx["core"] = self
        self.modules = self.ctx["modules"] = ModuleRegistry(self.ctx)
        self.modules.load()
        self.adapters = adapters_pkg.load_all(self.ctx)
        self.by_name = {a.name: a for a in self.adapters}
        # tailnet owner (to verify the identity added by tailscale serve)
        if "tailscale" in self.by_name:
            st = self.by_name["tailscale"].status()
            login = (
                (st.get("User") or {}).get(str((st.get("Self") or {}).get("UserID")))
                or {}
            ).get("LoginName")
            if login:
                self.owners.add(login)
        self._pc_cache = {"t": 0, "data": None}
        self.mesh.learn_owners()
        self.mesh.refresh_self()

    # ---------- capabilities ----------
    def caps(self):
        caps = ["routes"]
        for a in self.adapters:
            caps += [c for c in a.caps if c not in caps]
        if self.modules.items or self.modules.failures:
            caps.append("modules")
        caps += [c for c in self.modules.caps() if c not in caps]
        if any(c in caps for c in ("battery", "clipboard_device", "notify", "files")):
            caps.append("device")
        if "containers" in caps:
            caps.append("services")
        return caps

    def lang(self):
        value = str(self.conf.get("lang") or "en").lower()
        return value if value in ("en", "es") else "en"

    def node_info(self):
        out = {
            "agent": "core",
            "name": self.ctx["name"],
            "platform": self.platform,
            "caps": self.caps(),
            "uptime": time.time() - START,
            "model": self.platform["model"],
            "brand": "",
            "os": self.platform["os"],
            "kind": self.platform["kind"],
            "protocol": PROTOCOL,
            "version": self.mesh.running_version,
            "domain": self.mesh.domain(),
            "coordinator": self.mesh.is_coordinator(),
            "android": self.platform["os"] if self.platform["termux"] else None,
        }
        for a in self.adapters:
            try:
                out.update(a.info())
            except Exception as e:
                out.setdefault("errors", {})[a.name] = repr(e)
        out.setdefault("shared", "files" in self.by_name)
        out.setdefault(
            "storage",
            {
                "free": shutil.disk_usage(Path.home()).free,
                "total": shutil.disk_usage(Path.home()).total,
            },
        )
        out.setdefault("exec", False)
        return out

    # ---------- data in the PC hub data.json format ----------
    def master_data(self):
        if not self.mesh.has_peers():
            return None
        if time.time() - self._pc_cache["t"] > 30:
            try:
                with master_get("/data.json", 8) as r:
                    self._pc_cache.update(t=time.time(), data=json.load(r))
            except Exception:
                self._pc_cache["t"] = time.time()
        return self._pc_cache["data"]

    _probe_cache: dict = {}
    _probe_lock = threading.Lock()

    @classmethod
    def probe(cls, url, ttl=5.0):
        """Is the service answering? A HEAD request (no body: a camera panel
        page alone is 400 KB), reused for `ttl` s because every UI refresh and
        the route watcher probe the same targets."""
        now = time.time()
        with cls._probe_lock:
            hit = cls._probe_cache.get(url)
            if hit and now - hit[0] < ttl:
                return dict(hit[1])
        t0 = time.time()
        res = None
        try:
            req = urllib.request.Request(url, method="HEAD")
            with tailnet.urlopen(req, timeout=2) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code  # any HTTP answer means the service is up
        except Exception as e:
            res = {"up": False, "error": type(e).__name__}
        if res is None:
            res = {"up": True, "code": code, "ms": round((time.time() - t0) * 1000)}
        with cls._probe_lock:
            cls._probe_cache[url] = (time.time(), res)
        return dict(res)

    def data(self, host_header=""):
        name = self.ctx["name"]
        ts = self.by_name.get("tailscale")
        if ts:
            st = ts.status()
            me_raw, peers_raw = st.get("Self", {}), [
                p
                for p in (st.get("Peer") or {}).values()
                if not p.get("ShareeNode")
                and "tag:ingress" not in (p.get("Tags") or [])
            ]
            node = lambda n, s=False: {
                "name": n.get("HostName"),
                "dns": (n.get("DNSName") or "").rstrip("."),
                "os": n.get("OS"),  # noqa: E731
                "ips": n.get("TailscaleIPs") or [],
                "online": bool(n.get("Online")) or s,
                "lastSeen": (
                    None
                    if (n.get("LastSeen") or "").startswith("0001")
                    else n.get("LastSeen")
                ),
                "direct": bool(n.get("CurAddr")),
                "relay": n.get("Relay") or "",
                "self": s,
            }
            me, peers, tailnet = (
                node(me_raw, True),
                [node(p) for p in peers_raw],
                (st.get("CurrentTailnet") or {}).get("Name", ""),
            )
        else:
            pc = self.master_data() or {}
            everyone = ([pc["self"]] if pc.get("self") else []) + (
                pc.get("peers") or []
            )
            if (
                not everyone
            ):  # no hub with the CLI online: use what the mesh directory knows
                everyone = [
                    {
                        "name": e["name"],
                        "dns": e.get("dns"),
                        "os": e.get("os"),
                        "ips": [e["ip"]] if e.get("ip") else [],
                        "online": self.mesh.is_alive(e["name"]),
                    }
                    for e in self.mesh.entries()
                ]
            me = next((n for n in everyone if n.get("name") == name), None) or {
                "name": name,
                "dns": host_header.split(":")[0],
                "os": self.platform["os"],
                "ips": [],
                "online": True,
            }
            me = {**me, "self": True, "online": True}
            peers = [{**n, "self": False} for n in everyone if n.get("name") != name]
            tailnet = pc.get("tailnet", "")
        # The mesh name is the user-visible name. The Tailscale DNS/HostName
        # stays separate so links and certificates still point at the real
        # node; only the display identity follows a hub rename.
        me["name"] = name
        base = me.get("dns") or host_header.split(":")[0]
        routes = []
        seen_routes = set()

        def add(port, path, url, target, label, kind="proxy", **extra):
            st = extra.pop("st", None)
            needs_probe = st is None and kind == "proxy"
            if st is None:
                st = (
                    {"up": False, "code": None, "ms": None}
                    if kind == "proxy"
                    else {"up": True, "code": 200, "ms": 0}
                )
            key = f"{port}{path}"
            if key in seen_routes:
                return
            seen_routes.add(key)
            route = {
                "key": key,
                "id": extra.pop("id", None) or key.strip("/").replace("/", "-"),
                "port": port,
                "path": path,
                "url": url,
                "kind": kind,
                "target": target,
                "name": label.get("name") or key,
                "about": label.get("about", ""),
                "icon": label.get("icon", "ph-globe-hemisphere-west"),
                "featured": bool(label.get("featured")),
                "panel": label.get("panel", ""),
                "hidden": bool(label.get("hidden")),
                "control": extra.pop("control", bool(label.get("control"))),
                "funnel": False,
                "project": label.get("project", ""),
                **st,
                **extra,
            }
            if needs_probe:
                route["_probe"] = target
            # custom title and description, replicated through the mesh (they win over config labels)
            lab = self.mesh.label_for(route["id"]) if getattr(self, "mesh", None) else {}
            if lab.get("name"):
                route["name"] = lab["name"]
            if lab.get("about") is not None:
                route["about"] = lab["about"]
            if lab.get("hidden") is not None:  # hidden from the index, set from any hub
                route["hidden"] = lab["hidden"]
            route["custom"] = bool(lab.get("custom"))
            routes.append(route)

        # route labels: config.json "labels" and, on a migrated PC, the app services.json
        labels = dict(self.conf.get("labels") or {})
        try:
            labels.update(
                json.loads((APP / "services.json").read_text(encoding="utf-8-sig")).get(
                    "routes", {}
                )
            )
        except (OSError, ValueError):
            pass
        if ts and "serve" in ts.caps:  # tailscale serve routes, as on the PC
            for hostport, cfg in (ts.serve_status().get("Web") or {}).items():
                host, _, port = hostport.rpartition(":")
                for path, h in (cfg.get("Handlers") or {}).items():
                    tgt = h.get("Proxy") or h.get("Path") or ""
                    add(
                        int(port),
                        path,
                        f"https://{host}"
                        + ("" if port == "443" else f":{port}")
                        + path,
                        tgt,
                        labels.get(f"{port}{path}", {}),
                        "proxy" if "Proxy" in h else "static",
                    )
        if not any(
            r["target"].endswith(f":{PORT}") for r in routes
        ):  # this hub itself, if it is not behind serve
            add(
                PORT,
                "/hub/",
                f"http://{base}:{PORT}/hub/",
                f"http://127.0.0.1:{PORT}",
                {
                    "name": "This hub",
                    "about": "Core hub on this device.",
                    "icon": "ph-compass",
                },
                st={"up": True, "code": 200, "ms": 0},
            )
        for r in (
            self.conf.get("routes") or []
        ):  # servers declared by the user in config.json
            add(
                r["port"],
                r.get("path", "/"),
                f"http://{base}:{r['port']}{r.get('path', '/')}",
                f"http://127.0.0.1:{r['port']}",
                r,
            )
        sv = self.by_name.get("services")
        if sv:  # services declared in config.json, with a switch
            for r in sv.routes():
                add(
                    r["port"],
                    r["path"],
                    f"http://{base}:{r['port']}{r['path']}",
                    f"http://127.0.0.1:{r['port']}",
                    r,
                    control=True,
                    id=r["id"],
                )
        dk = self.by_name.get("docker")
        if dk:
            for r in dk.routes(base):
                add(
                    r["port"],
                    "/",
                    f"http://{base}:{r['port']}/",
                    f"http://127.0.0.1:{r['port']}",
                    r,
                    control=True,
                    id=r["id"],
                )
        pending = [(r, r.pop("_probe")) for r in routes if "_probe" in r]
        if pending:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
                futures = {
                    pool.submit(self.probe, target): route for route, target in pending
                }
                for fut in as_completed(futures):
                    try:
                        futures[fut].update(fut.result())
                    except Exception as e:
                        futures[fut].update({"up": False, "error": type(e).__name__})
        # Apply the mesh-wide route order; the hub card stays pinned first.
        order = {str(rid): i for i, rid in enumerate(self.mesh.route_order)}
        routes.sort(key=lambda r: (
            0 if r.get("path") == "/hub" else 1,
            order.get(str(r.get("id")), len(order)),
            str(r.get("name") or ""),
        ))
        # The hub's own Funnel ports must never appear as extra served routes.
        # A real Serve on those ports (e.g. OctoPrint or Grafana) has a different target
        # and is preserved.
        own_hub = f"http://127.0.0.1:{PORT}"
        routes = [
            r for r in routes
            if not (r.get("port") in (8443, 10000) and r.get("target") == own_hub)
        ]
        return {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "tailnet": tailnet,
            "version": f"core · {self.platform['kind']}",
            "protocol": PROTOCOL,
            "domain": self.mesh.domain(),
            "self": me,
            "peers": peers,
            "routes": routes,
        }


def make_handler(core: Core):
    class H(BaseHTTPRequestHandler):
        timeout = 30

        def log_message(self, *a):
            pass

        def setup(self):
            super().setup()
            try:  # a request arriving over https:// on this HTTP port: cut it immediately
                self.request.settimeout(10)
                if self.request.recv(1, socket.MSG_PEEK) == b"\x16":
                    self.request.close()
            except OSError:
                pass

        def handle(self):
            if self.request.fileno() != -1:
                super().handle()

        # ---------- utilities ----------
        def _json(self, obj, code=200):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def _route(self):
            p = urllib.parse.urlparse(self.path).path
            return (p[4:] or "/") if p == "/hub" or p.startswith("/hub/") else p

        def _q(self):
            return {
                k: v[0]
                for k, v in urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query
                ).items()
            }

        def _client_ip(self):
            """The caller's address. Behind tailscale serve (loopback with X-Forwarded-For) it is the last
            entry, the one tailscaled wrote; earlier entries would come from the client and could be anything."""
            peer = self.client_address[0].removeprefix("::ffff:")
            fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip().removeprefix("::ffff:")
            return fwd if fwd and self._loopback() else peer

        def _loopback(self):
            try:
                return ipaddress.ip_address(
                    self.client_address[0].removeprefix("::ffff:")
                ).is_loopback
            except ValueError:
                return False

        def _ok(self):
            """Via tailscale serve: the tailnet owner (or a mesh hub without a user, e.g. tagged).
            Direct over the tailnet: owner devices and directory hubs. Without identity (Funnel) it does not pass.

            Identity headers are only trusted when the TCP peer is loopback (tailscale serve/funnel).
            A direct tailnet client must never be able to forge X-Forwarded-For or Tailscale-User-Login.
            """
            ip = self.client_address[0].removeprefix("::ffff:")
            owners = core.owners | core.mesh.owners
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                return False
            if self.headers.get("X-Forwarded-For"):
                if not addr.is_loopback:
                    return False
                login = self.headers.get("Tailscale-User-Login")
                if login:
                    return not owners or login in owners
                return self._client_ip() in core.mesh.trusted_ips()
            if addr.is_loopback:
                return True
            if not any(addr in n for n in TAILNET):
                return False
            # Only devices this hub knows: the owner's (from Tailscale) and the hubs in its directory.
            # With nothing known yet (first boot without the Tailscale CLI, before enrolling) nobody
            # passes but this device: 100.64.0.0/10 is also carrier-grade NAT, so an address in it
            # does not prove the request came over the tailnet.
            if ip in core.mesh.trusted_ips():
                return True
            core.mesh.note_unknown(ip)
            return False

        def _peer_ok(self):
            """Hub from a paired tailnet with a valid signature. Federated APIs only."""
            if not self.headers.get(AUTH_HEADER):
                return False
            route = self._route()
            if self.command == "GET":
                if not (
                    route in ("/api/mesh", "/api/catalog")
                    or route.startswith("/api/blob/")
                ):
                    return False
            elif route not in ("/api/mesh", "/api/mesh/relay"):
                return False
            try:
                who = core.mesh.verify_hub(self)
            except Exception:
                return False
            if not who or who is False:
                return False
            rec = core.mesh.peering.by_name(who)
            if rec and core.mesh.peering.allowed(rec.get("domain") or "", "mesh"):
                self._hub_verified = who  # the adapter reuses the verification (the nonce was already spent)
                return True
            return False

        def _base_url(self):
            """The URL the installer calls back (enrollment, package). It runs on another device, so
            it must be a tailnet address that reaches this hub: the name the request came through
            when that was tailscale serve, else this hub's published URL, else its tailnet IP."""
            host = self.headers.get("Host", "")
            if self.headers.get("X-Forwarded-For") and self._loopback():
                return f"https://{host}/hub"
            me = core.mesh.dir.get(core.mesh.me) or {}
            own = urllib.parse.urlparse(me.get("url") or "").hostname or ""
            if own and own != "localhost" and not own.startswith("127."):
                return me["url"].rstrip("/")  # e.g. https://<device>.<tailnet>.ts.net/hub/ (works when bound to loopback)
            return f"http://{me.get('ip') or host.split(':')[0]}:{PORT}/hub"

        def _static(self, p):
            p = {"/install.ps1": "/install-windows.ps1"}.get(p, p)
            f = (SITE / (p.lstrip("/") or "index.html")).resolve()
            found = SITE.resolve() in f.parents and f.is_file()
            if not found and p.startswith("/modules/"):  # a one-folder module: core/modules/<id>/site/
                g = static_file(p[len("/modules/"):])
                f, found = (g, True) if g else (f, False)
            if not found:
                moved = core.modules.alias(p.lstrip("/"))
                if moved:  # an old module page (bookmark, notification): its panel now
                    query = urllib.parse.urlparse(self.path).query
                    self.send_response(302)
                    self.send_header("Location", moved + ("?" + query if query else ""))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                return self._json({"error": "not found"}, 404)
            data = f.read_bytes()
            if f.parent == SITE / "i18n" and f.suffix == ".json":  # modules bring their own strings
                merged = {**core.modules.catalog(f.stem), **json.loads(data)}
                data = json.dumps(merged, ensure_ascii=False).encode("utf-8")
            if f.name in (
                "install.sh",
                "install-windows.ps1",
            ):  # the installer points at the hub that served it
                base = self._base_url()
                me = core.mesh.dir.get(core.mesh.me) or {}
                data = (
                    data.decode("utf-8")
                    .replace("__HUB_HOST__", urllib.parse.urlparse(base).hostname or "")
                    .replace("__HUB_IP__", me.get("ip") or "")
                    .replace("__HUB__", base)
                    .encode("utf-8")
                )
            elif f.suffix == ".html":  # bust cached CSS/JS after a signed update
                data = data.replace(
                    b"__HUB_VERSION__", core.mesh.running_version.encode()
                )
            self.send_response(200)
            self.send_header(
                "Content-Type", MIME.get(f.suffix, "application/octet-stream")
            )
            self.send_header("Content-Length", str(len(data)))
            versioned = "v=" in urllib.parse.urlparse(self.path).query and f.suffix in (
                ".js",
                ".css",
            )
            if versioned:
                cache = "public, max-age=31536000, immutable"
            elif f.suffix in (
                ".html",
                ".js",
                ".css",
                ".json",
                ".webmanifest",
                ".sh",
                ".ps1",
            ):
                cache = "no-store"
            else:
                cache = "max-age=86400"
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(data)

        def _send_file(self, f: Path):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(f.stat().st_size))
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{urllib.parse.quote(f.name)}",
            )
            self.end_headers()
            with f.open("rb") as fh:
                shutil.copyfileobj(fh, self.wfile, 1 << 20)

        def _result(self, r):
            if isinstance(r, tuple) and r[0] == "file":
                return self._send_file(r[1])
            if isinstance(r, tuple) and r[0] == "bytes":  # e.g. camera snapshot
                self.send_response(200)
                self.send_header("Content-Type", r[2])
                self.send_header("Content-Length", str(len(r[1])))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(r[1])
                return
            if isinstance(r, tuple) and r[0] == "empty":
                self.send_response(204)
                self.end_headers()
                return
            if (
                isinstance(r, tuple) and r[0] == "handled"
            ):  # the adapter already responded (proxy)
                return
            if isinstance(r, tuple) and r[0] == "error":
                return self._json({"error": r[2]}, r[1])
            return self._json(r)

        # ---------- Funnel: from the internet only the gate passes ----------
        def _public(self):
            # Funnel ports are exclusively public. This also gates a tailnet
            # browser that opens the public URL (it carries an identity header).
            host = self.headers.get("Host") or ""
            try:
                public_port = int(host.rsplit(":", 1)[-1])
            except ValueError:
                public_port = 0
            if self._loopback() and public_port in (8443, 10000):
                return True
            # Only the local tailscaled process may present Funnel/forwarding headers.
            return (
                self._loopback()
                and bool(
                    self.headers.get("X-Forwarded-For")
                    or self.headers.get("Tailscale-Funnel-Request")
                )
                and not self.headers.get("Tailscale-User-Login")
            )

        def _cross_site(self):
            """Reject browser requests from another origin (CSRF, cross-site WebSocket) while allowing CLI
            and hub clients, which send neither Origin nor Sec-Fetch-*. The port counts: a page on another
            port of this same name (a Funnel share on 8443, any other service) is another origin, and
            "same-site" covers every device of the tailnet; only following a link is allowed from there."""
            site = (self.headers.get("Sec-Fetch-Site") or "").lower()
            if site == "cross-site":
                return True
            if site == "same-site" and (self.headers.get("Sec-Fetch-Mode") or "").lower() != "navigate":
                return True
            origin = self.headers.get("Origin")
            if origin is None:
                return False
            if origin.strip().lower() == "null":
                return True
            try:
                o = urllib.parse.urlparse(origin.strip())
                h = urllib.parse.urlparse("//" + (self.headers.get("Host") or ""))
                default = {"http": 80, "https": 443}.get(o.scheme.lower())
                return (not o.hostname or o.hostname != h.hostname
                        or (o.port or default) != (h.port or default))
            except ValueError:
                return True

        def end_headers(self):
            if not getattr(self, "_security_headers_sent", False):
                self._security_headers_sent = True
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                ancestors = getattr(self, "_frame_ancestors", None)
                if ancestors:
                    # A proxied service (Funnel): pages of this same host on its other public
                    # ports may embed it (a camera panel on one port showing a video service on another). No
                    # X-Frame-Options: it cannot express that, and CSP takes precedence.
                    self.send_header("Content-Security-Policy", "frame-ancestors " + ancestors)
                else:
                    self.send_header("X-Frame-Options", "DENY")
                    self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            super().end_headers()

        def _pty_relay(self, node, q):
            try:
                base = core.mesh.base(node)
            except KeyError:
                return self._json({"error": "device has no hub"}, 404)
            rest = "/api/term/pty?" + urllib.parse.urlencode({k: v for k, v in q.items() if k != "node"})
            hdrs = {**core.mesh._auth("GET", rest, None),
                    "X-Hub-User": self.headers.get("Tailscale-User-Login") or "local"}
            try:
                r = ws.relay(self, base, rest, hdrs)
            except OSError as e:
                return self._json({"error": f"the device is not responding ({type(e).__name__})"}, 504)
            if r:
                return self._json({"error": r[1]}, r[0])

        def _gate(self):
            fn = core.by_name.get("funnel")
            if fn:
                return fn.gate(self)
            return self._json({"error": "not found"}, 404)

        def do_HEAD(self):
            return self._gate() if self._public() else self.do_GET()

        def do_PUT(self):
            if self._public():
                return self._gate()
            return (
                self.do_POST()
            )  # signed PUT between hubs (e.g. blobs); same path as POST

        do_PATCH = do_DELETE = do_OPTIONS = do_PUT

        # ---------- GET ----------
        def do_GET(self):
            self._hub_verified = None
            if self._public():
                return self._gate()
            self._body_sha = hashlib.sha256(b"").hexdigest()
            if not self._ok() and not self._peer_ok():
                return self._json(
                    {"error": "unauthorized"}, 404 if self._public() else 403
                )
            core.req.handler = self
            p, q = self._route(), self._q()
            if p.startswith("/api/") and self._cross_site():
                return self._json({"error": "cross-site request rejected"}, 403)
            if p == "/api/term/pty":  # interactive terminal: the connection becomes a WebSocket
                node = q.get("node")
                if node and node != core.mesh.me:  # another hub's shell, carried through this one
                    return self._pty_relay(node, q)
                t = core.by_name.get("terminal")
                if not t:
                    return self._json({"error": "this device does not have that capability"}, 404)
                return t.websocket(self, q)
            try:
                if p == "/api/caps":
                    return self._json(
                        {
                            "kind": "core",
                            "name": core.ctx["name"],
                            "pub": core.identity.pub_b64,  # seeds this hub's mascot
                            "ui": core.mesh.ui.get("values", {}),  # theme and mascots, mesh-wide
                            "platform": core.platform["kind"],
                            "caps": core.caps(),
                            "protocol": PROTOCOL,
                            "version": core.mesh.running_version,
                            "release": core.mesh.running_release,
                            "lang": core.lang(),
                            "modules": [m["id"] for m in core.modules.statuses()],
                        }
                    )
                if p == "/api/modules":
                    return self._json(
                        {"modules": core.modules.statuses(), "lang": core.lang()}
                    )
                if p.startswith("/api/modules/"):
                    rest = p[len("/api/modules/") :]
                    mid, _, tail = rest.partition("/")
                    if mid in core.modules.items or mid in core.modules.failures:
                        if not tail or tail == "status":
                            return self._json(core.modules.status(mid))
                        if tail == "config":
                            return self._json(core.modules.status(mid).get("config"))
                        m = core.modules.items.get(mid)
                        r = m.get(p, q) if m else None
                        if r is not None:
                            return self._result(r)
                    return self._json({"error": "module not found"}, 404)
                if p == "/node":
                    return self._json(core.node_info())
                if p == "/data.json":
                    return self._json(core.data(self.headers.get("Host", "")))
                if p == "/api/hub/public-auth":
                    if not self._ok():
                        return self._json({"error": "only the tailnet owner can edit the public login"}, 403)
                    auth = core.conf.get("public_auth") or {}
                    return self._json({
                        "user": str(auth.get("user") or ""),
                        "configured": bool(auth.get("user") and auth.get("hash")),
                    })
                if p == "/api/hub/release":  # what runs, what ran before, whether updates are paused
                    return self._json(core.mesh.release_info())
                if p == "/api/hub/help":  # what the Help page needs to write ready-to-paste commands
                    if not hasattr(core, "_service"):
                        try:
                            core._service = service_info(core.platform["family"])
                        except Exception:  # the page still works, just without the restart command
                            core._service = {"kind": "none", "name": ""}
                    me = core.mesh.dir.get(core.mesh.me) or {}
                    return self._json({
                        "name": core.ctx["name"], "family": core.platform["family"], "port": PORT,
                        "bind": BIND, "app": str(APP), "data": str(DATA), "url": me.get("url") or "",
                        "ip": me.get("ip") or "", "serve": "serve" in core.caps(),
                        "author": bool(core.release_key), "service": core._service,
                        "python": "python" if core.platform["family"] in ("windows", "android") else "python3",
                    })
                if p == "/api/hub/version":  # old cores also use it to update
                    # Announce the code that is *running*, not what is on disk without a restart:
                    # so nobody adopts a version that has not passed the health check yet.
                    return self._json(
                        {
                            "version": core.mesh.running_version,
                            "role": "core",
                            "platform": core.platform["kind"],
                            "protocol": PROTOCOL,
                            "release": core.mesh.running_release,
                            "ed25519": crypto.BACKEND,
                        }
                    )
                if p == "/dist/hub.zip":  # any hub distributes the code (mesh step 1)
                    return self._result(
                        ("bytes", federation.bundle(APP), "application/zip")
                    )
                if (
                    p == "/api/services"
                ):  # local services + containers, all with a switch
                    out = {}
                    for name in ("services", "docker"):
                        a = core.by_name.get(name)
                        if a:
                            out.update(
                                a.all_status() if name == "services" else a.services()
                            )
                    return self._json(out)
                if p == "/api/whoami":
                    ip = self._client_ip()
                    d = core.data(self.headers.get("Host", ""))
                    who = next(
                        (
                            n
                            for n in [d["self"]] + d["peers"]
                            if ip in (n.get("ips") or [])
                        ),
                        d["self"],
                    )
                    return self._json(
                        {
                            "ip": ip,
                            "name": who.get("name"),
                            "dns": who.get("dns"),
                            "os": who.get("os"),
                            "isHost": who is d["self"],
                            "hostName": d["self"]["name"],
                        }
                    )
                if (
                    p == "/api/actions"
                ):  # actions from several adapters and modules (device, printer, ...)
                    out = core.modules.get(p, q) or {}
                    for a in core.adapters:
                        r = a.get(p, q)
                        if isinstance(r, dict):
                            out.update(r)
                    return self._json(out)
                for m in core.modules.active():
                    r = m.get(p, q)
                    if r is not None:
                        return self._result(r)
                for a in core.adapters:
                    r = a.get(p, q)
                    if r is not None:
                        return self._result(r)
                if p.startswith("/api/") or p in ("/clip", "/files", "/file", "/exec"):
                    return self._json(
                        {"error": "this device does not have that capability"}, 404
                    )
                return self._static(p)
            except PermissionError as e:
                return self._json({"error": str(e)}, 403)
            except FileNotFoundError:
                return self._json({"error": "not found"}, 404)

        # ---------- POST ----------
        def do_POST(self):
            self._hub_verified = None
            if self._public():
                return self._gate()
            if self._cross_site():
                return self._json({"error": "cross-site request rejected"}, 403)
            allowed = self._ok()
            self._raw = None
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0:  # read(-1) would read until the client hangs up: never
                self.close_connection = True
                return self._json({"error": "invalid Content-Length"}, 400)
            if not allowed:
                n0 = n
                if (
                    n0 > 8_000_000
                    or not self.headers.get(AUTH_HEADER)
                    or self._route() not in ("/api/mesh", "/api/mesh/relay")
                ):
                    return self._json(
                        {"error": "unauthorized"}, 404 if self._public() else 403
                    )
                raw0 = self.rfile.read(n0) if n0 else b"{}"
                self._body_sha = hashlib.sha256(raw0).hexdigest()
                if not self._peer_ok():  # now with the real body hash
                    return self._json({"error": "invalid hub signature"}, 403)
                self._raw = raw0
            core.req.handler = self
            p, q = self._route(), self._q()
            try:
                for (
                    a
                ) in (
                    core.adapters
                ):  # adapters that receive the body as a stream (files, proxy)
                    if p in getattr(a, "stream_paths", ()) or (
                        hasattr(a, "wants_stream") and a.wants_stream(p)
                    ):
                        self._body_sha = "stream"  # the hash is signed as "stream" (the content goes ahead)
                        return self._result(a.stream(p, q, self, n))
                if p == "/file":  # upload: the body is the file bytes
                    fa = core.by_name.get("files")
                    if not fa:
                        return self._json({"error": "file support is unavailable"}, 404)
                    _, dest = fa.post(p, {}, q, self)
                    full = disk_room(dest.parent, n)
                    if full:
                        self.close_connection = True
                        return self._json({"error": full}, 507)
                    left = n
                    with dest.open("wb") as fh:
                        while left > 0:
                            chunk = self.rfile.read(min(left, 1 << 20))
                            if not chunk:
                                break
                            fh.write(chunk)
                            left -= len(chunk)
                    return self._json({"ok": left == 0, "bytes": n})
                if self._raw is None and n > MAX_JSON:  # files and blobs stream; a JSON body is small
                    self.close_connection = True
                    return self._json({"error": "request body too large"}, 413)
                raw = (
                    self._raw
                    if self._raw is not None
                    else (self.rfile.read(n) if n else b"{}")
                )
                self._body_sha = hashlib.sha256(raw).hexdigest()
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except UnicodeDecodeError:
                    body = json.loads(raw.decode("latin-1"))
                if p == "/api/hub/name":
                    if not self._ok():
                        return self._json({"error": "only the tailnet owner can rename a hub"}, 403)
                    name = str((body or {}).get("name") or "").strip()
                    if not name or len(name) > 48 or "/" in name or "\\" in name:
                        return self._json({"error": "invalid hub name"}, 400)
                    old = core.ctx.get("name") or ""
                    target = core.mesh.dir.get(name)
                    if name != old and target and target.get("pub") not in (None, core.identity.pub_b64):
                        return self._json({"error": "that hub name belongs to another identity"}, 409)
                    change_dns = bool((body or {}).get("change_dns"))
                    changed_dns = False
                    new_url = ""
                    new_dns = ""
                    if change_dns and name != old:
                        funnel = core.by_name.get("funnel")
                        active = funnel.shares.view() if funnel and hasattr(funnel, "shares") else None
                        if active:
                            return self._json({"error": "stop the published Funnel group before changing DNS"}, 409)
                        if not core.conf.get("master"):
                            others = [e for e in core.mesh.dir.values()
                                      if e.get("name") != old and not e.get("removed")]
                            if others:
                                return self._json({"error": "this hub is the mesh master/seed; change its DNS only after updating the master URL on the other hubs"}, 409)
                        slug = _slugify(name)
                        if not slug:
                            return self._json({"error": "the name cannot be converted to a Tailscale DNS label"}, 400)
                        try:
                            status, own, old_dns, old_host = _ts_self()
                            old_slug = old_dns.split(".")[0]
                            for peer in (status.get("Peer") or {}).values():
                                pdns = (peer.get("DNSName") or "").rstrip(".").split(".")[0].lower()
                                phost = (peer.get("HostName") or "").strip().lower()
                                if (pdns == slug or phost == slug) and pdns != old_slug:
                                    return self._json({"error": f"the Tailscale hostname {slug} is already used by another node"}, 409)
                            if slug != old_slug:
                                snapshot = _serve_snapshot()
                                code, out = _run_ts("set", f"--hostname={slug}")
                                if code != 0:
                                    return self._json({"error": "tailscale hostname change failed: " + out}, 502)
                                try:
                                    _serve_restore(snapshot)
                                    _, _, new_dns, _ = _ts_self()
                                    new_url = f"https://{new_dns}/hub/"
                                    if not _wait_hub(new_url):
                                        raise RuntimeError("the new hostname did not become healthy")
                                    changed_dns = True
                                except Exception as e:
                                    _run_ts("set", f"--hostname={old_slug}")
                                    try:
                                        _serve_restore(snapshot)
                                        _wait_hub(f"https://{old_dns}/hub/", seconds=10)
                                    except Exception:
                                        pass
                                    return self._json({"error": "DNS change rolled back: " + str(e)}, 502)
                            else:
                                new_url = f"https://{old_dns}/hub/"
                        except Exception as e:
                            return self._json({"error": "DNS change failed: " + str(e)}, 502)
                    core.conf["name"] = name
                    if changed_dns and new_dns:
                        core.conf["self_dns"] = new_dns
                        core.conf["url"] = new_url
                    save_conf(core.conf)
                    core.ctx["name"] = name
                    core.identity.name = name
                    core.approvals.set_me(name)
                    try:
                        if name == old:
                            core.mesh.set_hub(name, {"restore": True})
                            core.mesh.refresh_self(force=True)
                        else:
                            core.mesh.rename_self(old, name)
                    except ValueError as e:
                        return self._json({"error": str(e)}, 409)
                    core.ctx.get("notify") and core.ctx["notify"](
                        "hub_name", "Hub renamed", f"{old} → {name}", "/hub/"
                    )
                    return self._json({"ok": True, "name": name, "previous": old,
                                       "dns_changed": changed_dns, "url": new_url})
                if p in ("/api/hub/rollback", "/api/hub/updates", "/api/hub/apply"):
                    if not self._ok():
                        return self._json({"error": "only the tailnet owner can change this hub's code"}, 403)
                    if p == "/api/hub/updates":
                        return self._json(core.mesh.set_updates_paused(bool((body or {}).get("paused"))))
                    who = self.headers.get("Tailscale-User-Login") or "owner"
                    try:
                        if p == "/api/hub/apply":
                            return self._json(core.mesh.apply_now(who))
                        return self._json(core.mesh.rollback(who))
                    except ValueError as e:
                        return self._json({"error": str(e)}, 409)
                if p == "/api/ui":
                    if not self._ok():
                        return self._json({"error": "only the tailnet owner can change the interface"}, 403)
                    return self._json(core.mesh.set_ui(body or {}).get("values", {}))
                if p == "/api/routes/order":
                    if not self._ok():
                        return self._json({"error": "only the tailnet owner can reorder routes"}, 403)
                    ids = (body or {}).get("ids")
                    if not isinstance(ids, list) or any(not isinstance(x, (str, int)) for x in ids):
                        return self._json({"error": "ids must be a list"}, 400)
                    return self._json(core.mesh.set_route_order(ids))
                if p == "/api/hub/public-auth":
                    if not self._ok():
                        return self._json({"error": "only the tailnet owner can edit the public login"}, 403)
                    user = str((body or {}).get("user") or "").strip()
                    password = str((body or {}).get("password") or "")
                    if not user or len(user) > 64 or any(c in user for c in "\r\n\t\0"):
                        return self._json({"error": "invalid public login user"}, 400)
                    if password and len(password) > 256:
                        return self._json({"error": "password is too long"}, 400)
                    auth = dict(core.conf.get("public_auth") or {})
                    if password:
                        if len(password) < 8:
                            return self._json({"error": "password must be at least 8 characters"}, 400)
                        salt = secrets.token_hex(16)
                        iterations = 210000
                        digest = hashlib.pbkdf2_hmac(
                            "sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations
                        ).hex()
                        auth = {
                            "user": user,
                            "salt": salt,
                            "hash": digest,
                            "iterations": iterations,
                        }
                    else:
                        if not (auth.get("user") and auth.get("hash")):
                            return self._json({"error": "password is required for the first setup"}, 400)
                        auth["user"] = user
                    core.conf["public_auth"] = auth
                    save_conf(core.conf)
                    funnel = core.by_name.get("funnel")
                    if funnel and hasattr(funnel, "set_public_auth"):
                        funnel.set_public_auth(auth)
                    return self._json({"ok": True, "user": user, "configured": True})
                if p.startswith("/api/modules/") and p.endswith("/config"):
                    mid = p[len("/api/modules/") : -len("/config")].strip("/")
                    try:
                        return self._json(
                            core.modules.set_config(
                                mid, body if isinstance(body, dict) else {}
                            )
                        )
                    except KeyError:
                        return self._json({"error": "module not found"}, 404)
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                if p.startswith("/api/modules/"):
                    mid = p[len("/api/modules/") :].split("/", 1)[0]
                    m = core.modules.items.get(mid)
                    if not m:
                        return self._json({"error": "module not found"}, 404)
                    r = m.post(p, body, q, self)
                    if r is not None:
                        return self._result(r)
                for m in core.modules.active():
                    r = m.post(p, body, q, self)
                    if r is not None:
                        return self._result(r)
                for a in core.adapters:
                    r = a.post(p, body, q, self)
                    if r is not None:
                        return self._result(r)
                return self._json(
                    {"error": "this device does not have that capability"}, 404
                )
            except PermissionError as e:
                return self._json({"error": str(e)}, 403)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    return H


def enroll(hub: str):
    """Enroll in the mesh through any hub. The tailnet of that hub verifies the identity."""
    c = load_conf()
    hub = hub.rstrip("/")
    c["master"] = hub  # seed: the first hub to ask until we have our own directory
    c["seeds"] = sorted(set((c.get("seeds") or []) + [hub]))
    if os.environ.get("HUB_IP"):
        c["master_ip"] = os.environ["HUB_IP"]
    if os.environ.get("HUB_PRIORITY"):
        c["priority"] = int(os.environ["HUB_PRIORITY"])
    save_conf(c)
    core = Core()
    f = core.mesh.self_fields()
    url = (
        f.get("url")
        or f"http://{f.get('dns') or f.get('ip') or 'localhost'}:{PORT}/hub/"
    )
    body = {
        "kind": f["kind"],
        "url": url,
        "platform": core.platform["kind"],
        "os": core.platform["os"],
        "name": core.ctx["name"],
        "capable": core.mesh.capable(),
        "priority": c.get("priority"),
        "pub": core.identity.pub_b64,
    }
    req = urllib.request.Request(
        hub + "/api/nodes/enroll",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(body).encode(),
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.load(r)
    c = load_conf()
    c.update(
        name=res["name"],
        self_ip=res.get("ip"),
        self_dns=res.get("dns"),
        token=res.get("token"),
    )
    if res.get("domain"):
        c["domain"] = res[
            "domain"
        ]  # without the Tailscale CLI, the domain is learned during enrollment
    if not f.get("url"):
        c["self_url"] = url
    save_conf(c)
    core.ctx["name"] = res["name"]
    core.identity.name = res["name"]
    core.approvals.set_me(res["name"])
    if res.get("domain"):
        core.mesh._dom, core.mesh._dom_t = res["domain"], time.time()
    core.mesh.merge(res.get("directory"))
    core.mesh.refresh_self()
    print(
        f"Enrolled as {res['name']} ({core.platform['kind']}); hub at {url}; "
        f"mesh has {len(core.mesh.entries())} hubs"
    )


def serve(bind="0.0.0.0"):
    global BIND
    BIND = bind
    core = Core()
    threading.Thread(target=core.mesh.run, daemon=True).start()

    threading.Thread(target=deps.ensure_crypto,
                     args=(core.mesh.data, core.mesh.log, core.mesh._restart), daemon=True).start()
    print(
        f"[hub] {core.ctx['name']} ({core.platform['kind']}) caps={core.caps()} "
        f"protocol={PROTOCOL} version={core.mesh.running_version} ed25519={crypto.BACKEND} on {bind}:{PORT}",
        flush=True,
    )
    HubServer((bind, PORT), make_handler(core)).serve_forever()
