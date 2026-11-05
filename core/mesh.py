"""Mesh of hubs without a fixed master: CRDT directory, elected coordinator, signed code and
federated services.

1. Any hub serves the package (/dist/hub.zip) and the installers (/install.sh, /install.ps1),
   so a new machine installs from whichever hub is on.
2. Replicated directory (DATA/directory.json), merged **field by field** with logical clocks
   (core/crdt.py): two hubs that change different things about the same machine do not overwrite each other, and their
   wall clocks do not need to match. Removals are one more field (`removed`).
3. Gossip: each round it talks to K random hubs and every N rounds to all of them (anti-entropy), so
   a change arrives quickly without traffic growing with the network. Liveness is local (silence >
   HUB_MESH_ALIVE); if a hub appears as suspect its `inc` goes up and it re-announces itself.
4. Elected coordinator: among the live and capable hubs (Tailscale CLI + Web Push) the one with the
   highest priority wins, then the oldest and the name. All of them choose the same because they start from the
   same directory. The coordinator sends notifications and stores the network clipboard.
5. CRDT replication between capable hubs: clipboard (set with deletion markers) and
   push subscriptions (one record per device). Anyone can take over without
   losing what was copied during a partition.
6. Code: RELEASE.json {version, stamp, seq, pub, sig}. Another hub's version is adopted only if it
   has a higher `seq`, the signature matches and a live hub confirms that that version started up fine (canary).
   Before applying, state/app.prev is saved; if the new hub does not start, it restores itself. Each
   hub announces `ok` after HUB_OK_GRACE seconds healthy. A local edit without a signature is not propagated.
7. Federated extras: route catalog for the whole network with proxying through any hub
   (/api/goto/<route>), replicated content store (blobs), store-and-forward relay,
   peering between tailnets and quorum approvals for sensitive actions (Funnel is left out).
   Service titles/descriptions are a per-field CRDT (DATA/labels.json) carried by gossip and
   pushed to the other hubs on edit; they stay inside the tailnet.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import random
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__, crdt, crypto
from .forward import HOP_MAX, TTL as FORWARD_TTL, Forward
from .identity import AUTH_HEADER, Nonces, ub64, verify_token
from .peering import make_invite
from . import tailnet
from .tailnet import hub_url_ok, is_loopback

ALIVE = int(os.environ.get("HUB_MESH_ALIVE", "180"))    # seconds without hearing from a hub before considering it down
EVERY = int(os.environ.get("HUB_MESH_EVERY", "60"))      # seconds between gossip rounds
ANTI = int(os.environ.get("HUB_MESH_ANTI", "5"))         # every N rounds, full sweep
GOSSIP_K = int(os.environ.get("HUB_GOSSIP_K", "3"))      # random hubs per quick round
OK_GRACE = int(os.environ.get("HUB_OK_GRACE", "45"))     # seconds healthy before confirming a version
RELEASE_GRACE = int(os.environ.get("HUB_UPDATE_GRACE", "900"))  # max seconds waiting for canary
BLOB_RF = max(1, int(os.environ.get("HUB_BLOB_RF", "2")))  # copies of each file in the network
PROTOCOL = 2
REPLICA = ("vapid_private.pem", "subscriptions.json", "clipboard.json")

# Fields that are merged by their logical clock. The owner's fields are signed by the owner; priority/removed
# can be changed by any hub of the owner from Control › Network (that is why they are left out of the signature).
MERGEABLE = ("ip", "dns", "url", "kind", "platform", "os", "capable", "enrolled", "priority",
             "removed", "inc", "domain")


def default_priority(kind: str) -> int:
    return 100 if kind in ("raspberry-pi", "server") else 10 if kind == "android" else 50


def release_key(rel: dict):
    """Total order of versions: first the sequence (monotonic), then the stamp (compat)."""
    try:
        return int(rel.get("seq") or 0), float(rel.get("stamp") or 0)
    except (TypeError, ValueError):
        return 0, 0.0


def release_confirmed(version: str, candidates) -> bool:
    """Does any live hub declare that version V started and is still healthy?"""
    return any((c.get("release") or {}).get("version") == version and c.get("ok") == version
               for c in candidates)


def choose_release(mine: dict, candidates, bad, first_seen, now: float, grace: float):
    """Decides which version to adopt (pure function, easy to test).

    candidates: [{"name", "release", "ok"}] of live hubs in the same domain.
    Returns (candidate, reason) or (None, reason).
    """
    mine_v = (mine or {}).get("version")
    mine_key = release_key(mine)
    best = None
    for c in candidates:
        rel = c.get("release") or {}
        v = rel.get("version")
        if not v or v == mine_v or v in bad:
            continue
        if release_key(rel) <= mine_key:  # never go down to a version with less seq/stamp
            continue
        if best is None or release_key(rel) > release_key(best["release"]):
            best = c
    if not best:
        return None, "nothing new"
    v = best["release"]["version"]
    if release_confirmed(v, candidates):
        return best, "healthy canary"
    t = first_seen.get(v)
    if t is not None and now - t >= grace:
        return best, f"no confirmation after {int(grace)} s"
    return None, "waiting for canary confirmation"


_TEXT = {"ip": 64, "dns": 253, "url": 512, "kind": 64, "platform": 64, "os": 64, "domain": 253}


def field_ok(f: str, v) -> bool:
    """A directory field that arrives by gossip must have the type every hub expects. Anything else
    is dropped: a text priority, for example, would break the coordinator election on every hub."""
    if f in _TEXT:
        return v is None or (isinstance(v, str) and len(v) <= _TEXT[f])
    if f in ("capable", "removed"):
        return isinstance(v, bool)
    if f in ("priority", "inc"):
        return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= (1000 if f == "priority" else 1 << 40)
    if f == "enrolled":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return True


def _num(v, default=0.0) -> float:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def prio(e) -> int:
    """An entry's priority for sorting; entries stored before field_ok existed may hold anything."""
    v = e.get("priority", 50)
    return v if isinstance(v, int) and not isinstance(v, bool) else 50


def merge_entry(mine, remote):
    """Merges a hub's entry field by field. Returns (entry, changed)."""
    if not isinstance(remote, dict) or not remote.get("name"):
        return mine, False
    name = remote["name"]
    base = dict(mine or {"name": name})
    changed = False
    hlc_map = dict(base.get("hlc") or {})
    rem_hlc = remote.get("hlc") if isinstance(remote.get("hlc"), dict) else {}
    for f in MERGEABLE:
        if f not in remote:
            continue
        rv = remote.get(f)
        if not field_ok(f, rv):
            continue
        rc = rem_hlc.get(f) if isinstance(rem_hlc.get(f), str) else crdt.synthetic(_num(remote.get("updated")), name)
        lc = hlc_map.get(f) or crdt.synthetic(_num(base.get("updated")), name)
        if f not in base:
            base[f], hlc_map[f], changed = rv, rc, True
        elif crdt.newer(rc, lc):
            if base.get(f) != rv:
                base[f], changed = rv, True
            hlc_map[f] = rc
    for k in ("pub", "sig"):
        if remote.get(k) and base.get(k) != remote[k]:
            base[k], changed = remote[k], True
    for k in ("updated", "hb"):
        v = _num(remote.get(k))
        if v > _num(base.get(k)):
            base[k], changed = v, True
    base["hlc"] = hlc_map
    base["name"] = name
    return base, changed


def install_resolver(mesh):
    """Without MagicDNS (Termux), the *.ts.net names in the directory are resolved to their Tailscale IP.
    In addition, `hub.<tailnet>` is a virtual name that points to the hub that coordinates (single entry)."""
    socket._hub_mesh = mesh
    if getattr(socket, "_hub_patched", False):
        return
    real = socket.getaddrinfo

    def gai(host, *a, **k):
        m = getattr(socket, "_hub_mesh", None)
        ip = m.ip_for(host) if m and isinstance(host, str) else None
        return real(ip or host, *a, **k)
    socket.getaddrinfo = gai
    socket._hub_patched = True


class Mesh:
    def __init__(self, core):
        from . import server
        self.core, self.srv = core, server
        self.data = server.DATA
        self.path = self.data / "directory.json"
        self.lock = threading.RLock()
        try:
            self.dir = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            self.dir = {}
        self.alive, self.remote = {}, {}      # name -> last contact / last response from /api/mesh
        self.owners, self.owner_ips = set(), set()
        self.coord = None
        self.hooks = []                       # fn(event) on "coordinator" and "replica"
        self._rev_cache = {"t": 0.0, "rev": 0.0, "hash": ""}
        self.labels = self._load_json(self.data / "labels.json", {})
        if not isinstance(self.labels, dict):
            self.labels = {}
        order_doc = self._load_json(self.data / "route_order.json", {})
        self.route_order = order_doc.get("ids") if isinstance(order_doc, dict) else []
        if not isinstance(self.route_order, list):
            self.route_order = []
        self.order_clock = order_doc.get("clock") if isinstance(order_doc, dict) else ""
        ui_doc = self._load_json(self.data / "ui.json", {})
        self.ui = ui_doc if isinstance(ui_doc, dict) and isinstance(ui_doc.get("values"), dict) else {"values": {}, "clock": ""}
        self.identity, self.pins = core.identity, core.pins
        self.nonces = Nonces()
        self.blobs, self.approvals, self.peering = core.blobs, core.approvals, core.peering
        self.hlc = crdt.HLC(self.me)
        for _e in self.dir.values():  # start the clock ahead of what is already in the directory
            for _v in (_e.get("hlc") or {}).values():
                self.hlc.observe(_v)
        if isinstance(self.order_clock, str) and self.order_clock:  # and ahead of the route order
            self.hlc.observe(self.order_clock)
        for _rec in self.labels.values():  # and ahead of the service labels
            if not isinstance(_rec, dict):
                continue
            for _f in crdt.LABEL_FIELDS:
                _v = _rec.get(_f)
                if isinstance(_v, dict) and isinstance(_v.get("hlc"), str):
                    self.hlc.observe(_v["hlc"])
        self.inc = int((self.dir.get(self.me) or {}).get("inc") or 0)
        self.bad = self._load_json(self.data / "bad_versions.json", {})
        self.release_keys = set(self._load_json(self.data / "release_keys.json", []))
        self.first_seen, self.last_pulled, self._seen_msgs = {}, {}, {}
        # version -> (failures, retry_after): a bundle that failed to download or verify is not
        # fetched again every round. Local only; unlike `bad`, it is not gossiped, because the
        # fault may be on the author's side (e.g. a fingerprint mismatch) and fixed minutes later.
        self._fetch_backoff = {}
        self._rounds = 0
        self._svc_cache = (0.0, [])
        self._ok_after = time.time() + OK_GRACE
        self.ok_version = None
        self._last_disk, self._disk_since = None, time.time()
        self.pins.set(self.me, self.identity.pub_b64)
        self.running_release = server.federation.release(server.APP, key=core.release_key)
        self.running_version = self.running_release.get("version")
        if core.release_key:  # TOFU with our own key too: an unsigned release is not valid
            self._pin_release(self.running_release)
        self._migrate()
        self.forward = Forward(self.data)
        self.forward.deliver_direct = self._deliver_direct
        self.forward.deliver_local = self._deliver_local
        self.forward.neighbors = self.capable_peers
        self.forward.hop = self._relay_hop
        self.approvals.set_me(self.me)
        self.approvals.alive_count = self._alive_hubs
        self.approvals.on_change = self._approval_changed
        self.approvals.notify = lambda *a: (self.core.ctx.get("notify") or (lambda *x, **k: None))(*a)
        self.approvals.register("mesh_remove", self._exec_remove)
        install_resolver(self)
        threading.Thread(target=self._background, daemon=True).start()

    # ---------- utilities ----------
    @property
    def me(self):
        return self.core.ctx["name"]

    def log(self, msg):
        try:
            with (self.data / "mesh.log").open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except OSError:
            pass

    @staticmethod
    def _load_json(path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _save_json(self, path: Path, obj):
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    def save(self):
        with self.lock:
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.dir, indent=1, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                pass

    def entries(self):
        with self.lock:
            return [dict(e) for e in self.dir.values() if not e.get("removed")]

    def domain(self) -> str:
        now = time.time()
        if getattr(self, "_dom", None) and now - getattr(self, "_dom_t", 0) < 60:
            return self._dom
        dom = self.srv.load_conf().get("domain")
        if not dom:
            ts = self.core.by_name.get("tailscale")
            if ts:
                try:
                    st = ts.status()
                    dom = st.get("MagicDNSSuffix") or (st.get("CurrentTailnet") or {}).get("Name")
                except Exception:
                    dom = None
        self._dom, self._dom_t = dom or "local", now
        return self._dom

    def _same_domain(self, name) -> bool:
        return (self.dir.get(name) or {}).get("domain") in (None, "", self.domain())

    def is_foreign(self, name) -> bool:
        if not name:
            return False
        dom = (self.dir.get(name) or {}).get("domain")
        if dom:
            return dom != self.domain()
        return self.peering.by_name(name) is not None

    def ip_for(self, host):
        h = host.lower().rstrip(".")
        for e in self.entries():
            if e.get("dns") and e["dns"].lower() == h and e.get("ip"):
                return e["ip"]
        c = self.srv.load_conf()  # old seed: master + its IP
        if c.get("master_ip") and urllib.parse.urlparse(c.get("master", "")).hostname == h:
            return c["master_ip"]
        # single entry: shoal.<tailnet>, hub.<tailnet> and malla.<tailnet> point to the hub that coordinates
        dom = (self.domain() or "").lower().rstrip(".")
        if dom and h in (f"shoal.{dom}", f"hub.{dom}", f"malla.{dom}"):
            name = self.coordinator() or self.me
            e = self.dir.get(name) or {}
            return e.get("ip")
        return None

    def is_alive(self, name):
        return name == self.me or time.time() - self.alive.get(name, 0) < ALIVE

    def _lab(self) -> bool:
        """A test mesh, every hub on this machine (own URL on loopback): loopback peers are fine."""
        return is_loopback((self.dir.get(self.me) or {}).get("url") or self.srv.load_conf().get("self_url") or "")

    def url_ok(self, url, domain="") -> bool:
        return hub_url_ok(url, domain or self.domain() or "", loopback=self._lab())

    def base(self, name):
        e = self.dir.get(name) or {}
        if not e.get("url") or not self.url_ok(e["url"], e.get("domain") or ""):
            raise KeyError(name)  # no URL, or one outside the tailnet (see core/tailnet.py)
        return e["url"].rstrip("/")

    def has_peers(self):
        c = self.srv.load_conf()
        return bool(c.get("master") or c.get("seeds") or any(e["name"] != self.me for e in self.entries()))

    def capable(self):
        if os.environ.get("HUB_FORCE_CAPABLE") == "1":  # isolated tests only, without the Tailscale CLI
            return True
        by = self.core.ctx.get("adapters_by_name") or {}
        return "tailscale" in by and "push" in by

    def _alive_hubs(self) -> int:
        n = 1
        for name, r in self.remote.items():
            if name == self.me or not self.is_alive(name):
                continue
            if (r.get("protocol") or 0) >= 2 and (r.get("domain") or self.domain()) == self.domain():
                n += 1
        return n

    # ---------- identity between hubs ----------
    def _auth(self, method: str, path: str, body: bytes | None):
        sha = hashlib.sha256(body or b"").hexdigest()
        return {"X-Hub-From": self.me, AUTH_HEADER: self.identity.token(method, path.split("?", 1)[0], sha)}

    def _pub_of(self, name):
        p = self.pins.get(name) or (self.dir.get(name) or {}).get("pub")
        if p:
            return p
        # a hub from another paired tailnet is not yet in the directory: its key comes in the invitation
        rec = self.peering.by_name(name)
        return rec.get("pub") if rec else None

    def verify_hub(self, handler, required=False):
        """None = request without signature (compatibility), False = invalid signature, str = signing hub."""
        if getattr(handler, "_hub_verified", None):  # already verified by the server gateway
            return handler._hub_verified
        body_sha = getattr(handler, "_body_sha", hashlib.sha256(b"").hexdigest())
        return verify_token(self.me, self._pub_of, self.nonces, handler, handler.command,
                            handler._route(), body_sha, required=required)

    def open(self, name, path, method="GET", body=None, headers=None, timeout=10):
        hh = {"X-Hub-From": self.me, "Content-Type": "application/json", **self._auth(method, path, body)}
        hh.update(headers or {})
        req = urllib.request.Request(self.base(name) + path, data=body, method=method, headers=hh)
        return tailnet.urlopen(req, timeout=timeout)

    # ---------- migration from the fixed master ----------
    def _migrate(self):
        nj = self.data / "nodes.json"
        if self.path.exists() or not nj.exists():
            return
        try:
            old = json.loads(nj.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        for name, n in old.items():
            url = n.get("hub_url") or f"http://{n['ip']}:8790/hub/"
            self.dir[name] = {"name": name, "ip": n["ip"], "dns": urllib.parse.urlparse(url).hostname, "url": url,
                              "kind": n.get("kind", "core"), "platform": "", "capable": False,
                              "enrolled": n.get("enrolled", 0), "updated": n.get("enrolled", 0)}
        self.save()

    # ---------- this hub in the directory ----------
    def self_fields(self):
        core, c = self.core, self.srv.load_conf()
        p = core.platform
        f = {"name": self.me, "platform": p["kind"], "os": p["os"],
             "kind": "pc" if p["family"] == "windows" else "core", "capable": self.capable(),
             "domain": self.domain()}
        ts = core.by_name.get("tailscale")
        if ts:
            s = ts.status().get("Self") or {}
            if s.get("TailscaleIPs"):
                f["ip"] = s["TailscaleIPs"][0]
            if s.get("DNSName"):
                f["dns"] = s["DNSName"].rstrip(".")
            if "serve" in ts.caps:  # /hub published by tailscale serve → https URL
                for hostport, cfg in (ts.serve_status().get("Web") or {}).items():
                    host, _, port = hostport.rpartition(":")
                    for path, h in (cfg.get("Handlers") or {}).items():
                        if path.rstrip("/") == "/hub" and (h.get("Proxy") or "").rstrip("/").endswith(f":{self.srv.PORT}"):
                            f["url"] = f"https://{host}" + ("" if port == "443" else f":{port}") + "/hub/"
        for k in ("ip", "dns", "url"):  # without CLI: what enrollment said
            if c.get("self_" + k) and k not in f:
                f[k] = c["self_" + k]
        if "url" not in f and self.srv.BIND != "127.0.0.1" and (f.get("dns") or f.get("ip")):
            f["url"] = f"http://{f.get('dns') or f['ip']}:{self.srv.PORT}/hub/"
        if c.get("url"):
            f["url"] = c["url"]
        return f

    def refresh_self(self, force=False):
        from .identity import SIGNED, entry_message
        f = self.self_fields()
        now = time.time()
        with self.lock:
            prev = dict(self.dir.get(self.me) or {})
            if prev.get("removed"):
                return  # removed: it does not add itself back
            if not prev and not (f.get("ip") or f.get("url")):
                return  # without CLI or enrollment: wait for another hub's directory to bring us
            e = dict(prev)
            hlc_map = dict(e.get("hlc") or {})
            changed = False
            for k, v in f.items():
                if v in (None, ""):
                    continue
                if e.get(k) != v:
                    e[k] = v
                    hlc_map[k] = self.hlc.tick()
                    changed = True
            if "priority" not in e:
                e["priority"] = int(self.srv.load_conf().get("priority") or default_priority(f.get("platform", "")))
                hlc_map["priority"] = self.hlc.tick()
                changed = True
            if not e.get("enrolled"):
                e["enrolled"] = now
                hlc_map["enrolled"] = self.hlc.tick()
                changed = True
            inc = max(int(self.inc or 0), int(e.get("inc") or 0))
            if e.get("inc") != inc or "inc" not in hlc_map:
                e["inc"] = inc
                hlc_map["inc"] = self.hlc.tick()
                changed = True
            self.inc = inc
            e["pub"] = self.identity.pub_b64
            msg = entry_message(e)
            sig_ok = False
            try:
                if e.get("sig"):
                    sig_ok = crypto.verify(ub64(e["sig"]), msg, ub64(e["pub"]))
            except Exception:
                sig_ok = False
            if force or not sig_ok or entry_message(prev) != msg:
                e["sig"] = self.identity.sign(msg)
            e["hlc"] = hlc_map
            if changed or not prev:
                e["updated"] = now
            if e != prev:
                self.dir[self.me] = e
                self.save()

    def _accept(self, entry: dict):
        name = entry.get("name")
        if not name:
            return False, "no name"
        dom = entry.get("domain") or self.domain()
        if dom != self.domain():
            if not self.peering.allowed(dom, "mesh"):
                return False, f"domain not paired: {dom}"
            cur = self.dir.get(name)
            if cur and (cur.get("domain") or self.domain()) == self.domain():
                return False, "a foreign hub cannot overwrite a local one"
        return self.pins.check(entry)

    def _reject(self, name, why):
        now = time.time()
        last = getattr(self, "_rej", {})
        if now - last.get(name, 0) > 600:
            self.log(f"entry from {name} rejected: {why}")
            last[name] = now
            self._rej = last

    def rename_self(self, old, new):
        """Rename this hub as one identity, removing old aliases.

        A hub name is also its directory key. Renaming by only adding the new
        key leaves the old key behind as an offline/removed server and can make
        the current hub inherit an old tombstone. This migrates the entry and
        marks the aliases that belong to the same identity as removed.

        The aliases stay in the directory as tombstones: other hubs only learn
        that an old name is gone from a newer "removed" stamp. Dropping the key
        instead left every other hub with the old name for good, and gossip
        brought it back here as well.
        """
        new = str(new or "").strip()
        if not new:
            raise ValueError("invalid hub name")
        with self.lock:
            target = self.dir.get(new)
            if target and target.get("pub") not in (None, self.identity.pub_b64):
                raise ValueError("that hub name belongs to another identity")
            base = dict(self.dir.get(old) or target or {})
            ip = base.get("ip")
            for name in list(self.dir):
                entry = self.dir.get(name) or {}
                if name == new:
                    continue
                same_identity = entry.get("pub") == self.identity.pub_b64
                same_address = bool(ip and entry.get("ip") == ip)
                if same_identity or same_address:
                    if not entry.get("removed"):
                        tomb = dict(entry)
                        tomb["hlc"] = dict(entry.get("hlc") or {})
                        tomb["removed"] = True
                        tomb["updated"] = time.time()
                        tomb["hlc"]["removed"] = self.hlc.tick()
                        self.dir[name] = tomb
                    self.alive.pop(name, None)
                    self.remote.pop(name, None)
            base["name"] = new
            base["removed"] = False
            base["updated"] = time.time()
            base.setdefault("hlc", {})["removed"] = self.hlc.tick()
            self.dir[new] = base
            self.pins.set(new, self.identity.pub_b64)
            self.save()
        self.coord = self.coordinator()
        self.refresh_self(force=True)
        return self.view()

    def merge(self, remote: dict, only_domain: str | None = None):
        """`only_domain`: the directory came from a hub of a paired tailnet, so only entries of that
        tailnet are taken from it (never one that claims to belong to this one)."""
        changed = False
        if not isinstance(remote, dict):
            return False
        with self.lock:
            for name, e in remote.items():
                if not isinstance(e, dict):
                    continue
                e = dict(e)
                e.setdefault("name", name)
                if only_domain is not None and e.get("domain") != only_domain:
                    self._reject(e.get("name") or name, "entry outside the sender's tailnet")
                    continue
                for _v in (e["hlc"].values() if isinstance(e.get("hlc"), dict) else ()):
                    self.hlc.observe(_v)
                ok, why = self._accept(e)
                if not ok:
                    self._reject(e.get("name") or name, why)
                    continue
                merged, ch = merge_entry(self.dir.get(e["name"]), e)
                if ch:
                    self.dir[e["name"]] = merged
                    changed = True
        if changed:
            self.save()
        return changed

    def receive(self, p: dict, peer_domain: str | None = None, peer_name: str | None = None) -> bool:
        """Integrates another hub's payload (gossip or response). It is idempotent.

        `peer_domain`/`peer_name`: the payload is known to come from that hub of a paired tailnet (it
        signed the request, or it is the address this hub called). Its own claims about its domain
        and name are then ignored, and it has no say over this tailnet's control plane."""
        if not isinstance(p, dict) or not p.get("name") or p.get("name") == self.me:
            return False
        if peer_domain is not None:
            if peer_name and p.get("name") != peer_name:
                return False
            p = {**p, "domain": peer_domain}
        name = p["name"]
        if not isinstance(name, str):
            return False
        dom = p.get("domain") or self.domain()
        mine = self.domain()
        if mine == "local" and peer_domain is None and dom not in ("", "local") and name in self.dir:
            # old hub without CLI or domain in config: it learns it from the hub it was already talking to
            self._dom, self._dom_t = dom, time.time()
            c = self.srv.load_conf()
            c["domain"] = dom
            self.srv.save_conf(c)
            own = self.dir.get(self.me)
            if own and (own.get("domain") in (None, "", "local")):
                own["domain"] = dom
                own.setdefault("hlc", {})["domain"] = self.hlc.tick()
                self.save()
            mine = dom
            self.log(f"domain learned: {dom}")
        foreign = dom != mine
        if foreign:
            if not self.peering.allowed(dom, "mesh"):
                return False
            cur = self.dir.get(name)
            if cur and (cur.get("domain") or mine) == mine:
                return False  # a paired tailnet cannot speak as one of this tailnet's hubs
        now = time.time()
        self.alive[name] = now
        rec = {k: p.get(k) for k in ("release", "ok", "rev", "hash", "capable", "coordinator", "protocol",
                                     "domain", "time")}
        rec["services"] = [s for s in p.get("services") or [] if isinstance(s, dict)] \
            if isinstance(p.get("services"), list) else []
        rec["blobs"] = [b for b in p.get("blobs") or [] if isinstance(b, str)]             if isinstance(p.get("blobs"), list) else []
        rec["monitor"] = p.get("monitor") if isinstance(p.get("monitor"), dict) else {}
        if foreign:  # a paired tailnet's release and canary mean nothing here
            rec.update(release=None, ok=None, capable=False, coordinator=None)
        self.remote[name] = rec
        changed = self.merge(p.get("directory"), only_domain=dom if foreign else None)
        if foreign:
            return changed
        if dom == self.domain():  # owners and labels from another tailnet have no authority here
            if self.merge_labels(p.get("labels")):
                changed = True
            if self.merge_route_order(p.get("route_order")):
                changed = True
            if self.merge_ui(p.get("ui")):
                changed = True
            self.owners |= set(p.get("owners") or [])
            if not self.core.by_name.get("tailscale"):
                self.owner_ips |= set(p.get("owner_ips") or [])
        self._merge_bad(p.get("bad"))
        q = p.get("quorum") or {}
        if q.get("values") and q.get("clock"):
            self.approvals.merge_quorum(q["values"], q["clock"])
        rel = p.get("release") or {}
        if rel.get("version"):
            self._pin_release(rel)
        if self.me in (p.get("suspect") or []):
            self.bump_incarnation()
        return changed

    # ---------- signed version, canary and rollback ----------
    def _pin_release(self, rel: dict):
        fed = self.srv.federation
        ok, pub = fed.check_release(rel)
        if ok and (not self.release_keys or pub in self.release_keys):
            if pub not in self.release_keys:
                self.release_keys.add(pub)
                self._save_json(self.data / "release_keys.json", sorted(self.release_keys))
                self.log(f"author key pinned: {pub[:12]}…")

    def verify_release(self, rel: dict) -> bool:
        fed = self.srv.federation
        ok, pub = fed.check_release(rel)
        if ok:
            if self.release_keys and pub not in self.release_keys:
                return False
            if not self.release_keys:
                self._pin_release(rel)
            return True
        return not self.release_keys  # unsigned: only if no key is pinned yet

    def _merge_bad(self, remote):
        if not isinstance(remote, dict) or not remote:
            return
        changed = False
        for v, info in remote.items():
            if v == self.running_version or v in self.bad:
                continue
            self.bad[v] = info if isinstance(info, dict) else {"ts": time.time(), "why": "marked by another hub"}
            changed = True
        if changed:
            self._save_json(self.data / "bad_versions.json", self.bad)

    def mark_bad(self, version, why):
        if not version or version == self.running_version:
            return
        self.bad[version] = {"ts": time.time(), "why": why, "by": self.me}
        self.bad = dict(sorted(self.bad.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:30])
        self._save_json(self.data / "bad_versions.json", self.bad)
        self.log(f"version {version} marked bad: {why}")

    def check_update_health(self):
        """On startup: confirms the version after the grace window or restores the previous one."""
        fed = self.srv.federation
        marker = self.data / "update_pending.json"
        self.ok_version = None
        self._ok_after = time.time() + OK_GRACE
        if not marker.exists():
            return
        d = self._load_json(marker, {})
        d["tries"] = int(d.get("tries") or 0) + 1
        self._save_json(marker, d)
        prev = self.data / "app.prev"
        if d["tries"] > 2 and prev.exists():
            if fed.restore_app(self.srv.APP, prev):
                self.mark_bad(d.get("version"), "did not pass startup")
                try:
                    marker.unlink()
                except OSError:
                    pass
                with (self.data / "update_failed.log").open("a", encoding="utf-8") as f:
                    f.write(f"{time.ctime()} rollback from {d.get('version')} to {d.get('prev')}\n")
                self.log(f"rollback: restored {d.get('prev')} ({d.get('version')} did not boot)")
                if not os.environ.get("HUB_NO_RESTART"):
                    self._restart()
            return
        threading.Thread(target=self._confirm_after_grace, args=(d,), daemon=True).start()

    def _confirm_after_grace(self, d):
        time.sleep(OK_GRACE)
        marker = self.data / "update_pending.json"
        if not marker.exists():
            return
        try:
            marker.unlink()
        except OSError:
            return
        self.bad.pop(d.get("version"), None)
        self._save_json(self.data / "bad_versions.json", self.bad)
        self.ok_version = self.running_version
        self._ok_after = 0
        self.log(f"version {self.running_version} confirmed healthy")

    def _tick_ok(self):
        if not self.ok_version and time.time() >= self._ok_after and self._rounds > 0:
            self.ok_version = self.running_version

    def _compiles(self) -> bool:
        files = list(self.srv.APP.glob("*.py")) + list((self.srv.APP / "core").rglob("*.py"))
        for f in files:
            if "__pycache__" in f.parts:
                continue
            try:
                compile(f.read_text(encoding="utf-8"), str(f), "exec")
            except (OSError, SyntaxError) as e:
                self.log(f"local edit does not compile: {e}")
                return False
        return True

    def _check_disk(self):
        """If the code on disk changed in a stable way (local edit), restart to apply it."""
        if os.environ.get("HUB_NO_RESTART"):
            return
        fed = self.srv.federation
        v = fed.code_version(self.srv.APP)
        now = time.time()
        if v == self.running_version:
            self._last_disk = None
            return
        if v != self._last_disk:
            self._last_disk, self._disk_since = v, now
            return
        if now - self._disk_since < 120 or not self._compiles():
            return
        self._apply_disk(v, "local edit")

    def _apply_disk(self, v, why) -> bool:
        """Snapshot the running code to app.prev and restart into what is on disk (signed on the
        author hub at startup, then rolled out like any release)."""
        if (self.data / "update_pending.json").exists():
            return False
        now = time.time()
        try:
            n = self.srv.federation.snapshot_app(self.srv.APP, self.data / "app.prev")
            self._save_json(self.data / "app.prev.json", {"version": self.running_version, "files": n, "ts": now})
            self._save_json(self.data / "update_pending.json",
                            {"version": v, "prev": self.running_version, "tries": 0, "started": now, "why": why})
        except Exception as e:
            self.log(f"could not prepare the restart: {e!r}")
            return False
        self.log(f"new code on disk ({why}): restarting to apply it")
        self._restart()
        return True

    def apply_now(self, by: str = "") -> dict:
        """After `git pull` on the author hub: apply the new code now instead of after 120 s of calm."""
        if os.environ.get("HUB_NO_RESTART"):
            raise ValueError("restarts are disabled on this hub (HUB_NO_RESTART)")
        fed = self.srv.federation
        fed.forget_version()
        v = fed.code_version(self.srv.APP)
        if v == self.running_version:
            raise ValueError("the code on disk is the code that is running: nothing to apply")
        if not self._compiles():
            raise ValueError("the code on disk does not compile; fix it (or git pull again) and retry")
        if (self.data / "update_pending.json").exists():
            raise ValueError("an update is already being applied")
        threading.Thread(target=lambda: (time.sleep(1.5), self._apply_disk(v, f"applied by {by or 'the owner'}")),
                         daemon=True).start()
        return {"from": self.running_version, "to": v, "author": bool(self.core.release_key)}

    def _restart(self):
        if os.environ.get("HUB_NO_RESTART"):
            return
        if self.core.platform["family"] == "windows":
            self.srv.federation.restart_hub()  # restarts the Shoal (or older TailscaleHub) task and exits
        os._exit(3)

    def sync_release(self):
        if self.srv.load_conf().get("updates_paused"):  # the owner froze this hub's code
            return
        fed, app = self.srv.federation, self.srv.APP
        mine = self.running_release
        now = time.time()
        candidates = []
        for name, r in self.remote.items():
            if not self.is_alive(name) or name not in self.dir or not self._same_domain(name):
                continue
            rel = r.get("release") or {}
            if not rel.get("version") or not self.verify_release(rel):
                continue
            if self._fetch_backoff.get(rel["version"], (0, 0))[1] > now:
                continue
            candidates.append({"name": name, "release": rel, "ok": r.get("ok")})
            self.first_seen.setdefault(rel["version"], now)
        pick, why = choose_release(mine, candidates, self.bad, self.first_seen, now, RELEASE_GRACE)
        if not pick:
            return
        rel = pick["release"]
        if rel["version"] == self.running_version:
            return
        try:
            with self.open(pick["name"], "/dist/hub.zip", timeout=180) as r:
                data = r.read()
            fed.verify_bundle(data, rel, keys=self.release_keys)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                got = json.loads(z.read("RELEASE.json"))
            prev = self.data / "app.prev"
            n = fed.snapshot_app(app, prev)
            self._save_json(self.data / "app.prev.json", {"version": self.running_version, "files": n, "ts": now})
            self._save_json(self.data / "update_pending.json",
                            {"version": rel["version"], "prev": self.running_version, "tries": 0,
                             "started": now, "from": pick["name"], "why": why})
            fed.apply_bundle(app, data)
            newrel = {k: got.get(k) for k in ("version", "stamp", "seq", "pub", "sig") if got.get(k) is not None}
            (app / "RELEASE.json").write_text(json.dumps(newrel), encoding="utf-8")
            self.log(f"code updated to {rel['version']} from {pick['name']} ({why})")
            with (self.data / "update.log").open("a", encoding="utf-8") as f:
                f.write(f"{time.ctime()} updated to {rel['version']} from {pick['name']} ({why})\n")
        except Exception as e:
            fails = self._fetch_backoff.get(rel["version"], (0, 0))[0] + 1
            wait = min(3600, 120 * 2 ** (fails - 1))
            self._fetch_backoff[rel["version"]] = (fails, now + wait)
            self.log(f"update from {pick['name']}: {e!r}; retrying {rel['version']} in {wait}s")
            return
        self._fetch_backoff.pop(rel["version"], None)
        if os.environ.get("HUB_NO_RESTART"):
            self.log("bundle applied; restart disabled by HUB_NO_RESTART")
            return
        self._restart()

    # ---------- the owner's brake: pause updates, go back one version ----------
    def release_info(self) -> dict:
        prev = self._load_json(self.data / "app.prev.json", {}) or {}
        has_prev = (self.data / "app.prev" / "core").exists() and prev.get("version") not in (None, self.running_version)
        rel = self.running_release or {}
        return {"version": self.running_version, "seq": rel.get("seq"), "signed": bool(rel.get("sig")),
                "author": bool(self.core.release_key), "previous": prev.get("version") if has_prev else None,
                "previous_ts": prev.get("ts") if has_prev else None,
                "paused": bool(self.srv.load_conf().get("updates_paused")), "bad": sorted(self.bad),
                "on_disk": self.srv.federation.code_version(self.srv.APP), "shoal": __version__}

    def set_updates_paused(self, paused: bool) -> dict:
        c = self.srv.load_conf()
        c["updates_paused"] = bool(paused)
        self.srv.save_conf(c)
        self.log("updates paused by the owner" if paused else "updates resumed by the owner")
        return self.release_info()

    def rollback(self, by: str = "") -> dict:
        """Back to the version that ran before this one. The current version is marked bad for the
        whole mesh (gossip), so no hub adopts it again. On the author hub the restored code is then
        signed as a new release with a higher sequence, and every hub adopts it like any update; on
        other hubs the change stays local until the author publishes something newer."""
        info = self.release_info()
        if not info["previous"]:
            raise ValueError("there is no previous version on this hub to go back to")
        bad = self.running_version
        self.bad[bad] = {"ts": time.time(), "why": f"rolled back by {by or 'the owner'}", "by": self.me}
        self._save_json(self.data / "bad_versions.json", self.bad)
        seq = (self.running_release or {}).get("seq") or 0
        if not self.srv.federation.restore_app(self.srv.APP, self.data / "app.prev"):
            raise ValueError("the previous version could not be restored")
        # The author signs the restored code as seq+1 for everyone. Any other hub keeps the abandoned
        # seq: a local seq+1 (unsigned, stamped now) would outrank the author's next signed release
        # if that one was already out, and this hub would never adopt it.
        self.srv.federation.republish(self.srv.APP, seq if info["author"] else seq - 1)
        self.log(f"rollback by {by or 'the owner'}: {bad} -> {info['previous']}")
        with (self.data / "update.log").open("a", encoding="utf-8") as f:
            f.write(f"{time.ctime()} rolled back from {bad} to {info['previous']} by {by or 'the owner'}\n")
        if not os.environ.get("HUB_NO_RESTART"):
            threading.Thread(target=lambda: (time.sleep(1.5), self._restart()), daemon=True).start()
        return {"from": bad, "to": info["previous"], "mesh": info["author"]}

    # ---------- CRDT replica ----------
    def _replica_state(self):
        now = time.time()
        if now - self._rev_cache["t"] < 2 and self._rev_cache["hash"]:
            return self._rev_cache["rev"], self._rev_cache["hash"]
        h = hashlib.sha256()
        for n in REPLICA:
            f = self.data / n
            if n == "clipboard.json":  # the local "sync" switch is not part of the shared state
                raw = json.dumps(self._clip_state(), sort_keys=True, ensure_ascii=False).encode()
            elif n == "subscriptions.json":
                raw = json.dumps(self._subs_state(), sort_keys=True, ensure_ascii=False).encode()
            else:
                raw = f.read_bytes() if f.exists() else b"-"
            h.update(n.encode() + raw)
        digest = h.hexdigest()
        meta = self.data / "replica.json"
        r = self._load_json(meta, None)
        if r is None:
            mt = [(self.data / n).stat().st_mtime for n in ("subscriptions.json", "clipboard.json")
                  if (self.data / n).exists() and (self.data / n).stat().st_size > 20]
            r = {"hash": digest, "rev": max(mt) if mt else 0}
            self._save_json(meta, r)
        elif r.get("hash") != digest:
            r = {"hash": digest, "rev": now}
            self._save_json(meta, r)
        self._rev_cache = {"t": now, "rev": float(r.get("rev") or 0), "hash": digest}
        return self._rev_cache["rev"], digest

    def replica_rev(self):
        return self._replica_state()[0]

    def _clip_state(self):
        d = self._load_json(self.data / "clipboard.json", {}) or {}
        st = {"items": d.get("items") or [], "tomb": d.get("tomb") or []}
        if d.get("policy"):
            st["policy"] = d["policy"]
        return st

    @staticmethod
    def _clip_doc(merged, raw):
        """clipboard.json after a merge: the shared state plus this hub's own "sync" switch."""
        doc = {"items": merged["items"], "tomb": merged.get("tomb") or [], "sync": raw.get("sync", True)}
        if merged.get("policy"):
            doc["policy"] = merged["policy"]
        return doc

    def _subs_state(self):
        d = self._load_json(self.data / "subscriptions.json", {}) or {}
        return d if isinstance(d, dict) else {}

    def replica_payload(self):
        rev, digest = self._replica_state()
        return {"rev": rev, "hash": digest, "protocol": PROTOCOL,
                "files": {n: base64.b64encode((self.data / n).read_bytes()).decode()
                          for n in REPLICA if (self.data / n).exists()},
                "crdt": {"clipboard": self._clip_state(), "subs": self._subs_state()}}

    def _merge_replica(self, rep: dict):
        cr = rep.get("crdt") or {}
        raw_clip = self._load_json(self.data / "clipboard.json", {}) or {}
        local_clip = self._clip_state()
        merged = crdt.clip_merge(local_clip, cr.get("clipboard") or {})
        if merged != local_clip:
            self._save_json(self.data / "clipboard.json", self._clip_doc(merged, raw_clip))
        local_subs = self._subs_state()
        msubs = crdt.reg_merge(local_subs, cr.get("subs") or {})
        if msubs != local_subs:
            self._save_json(self.data / "subscriptions.json", msubs)
        # the VAPID key must be the same across the network; if there is none yet, the coordinator's is copied
        b64pem = (rep.get("files") or {}).get("vapid_private.pem")
        if b64pem and not (self.data / "vapid_private.pem").exists():
            pem = self.data / "vapid_private.pem"
            pem.write_bytes(base64.b64decode(b64pem))
            try:
                os.chmod(pem, 0o600)
            except OSError:
                pass

    def pull_replica(self, name):
        with self.open(name, "/api/mesh/replica", timeout=25) as r:
            rep = json.load(r)
        if (rep.get("protocol") or 0) >= 2 and rep.get("crdt"):
            self._merge_replica(rep)
        else:  # old hub: file copy, as before
            for n, b64 in (rep.get("files") or {}).items():
                if n in REPLICA:
                    tmp = self.data / (n + ".tmp")
                    tmp.write_bytes(base64.b64decode(b64))
                    tmp.replace(self.data / n)
                    if n == "vapid_private.pem":
                        try:
                            os.chmod(self.data / n, 0o600)
                        except OSError:
                            pass
        if rep.get("hash"):
            self.last_pulled[name] = rep["hash"]
        self._rev_cache["t"] = 0
        self._replica_state()
        self.log(f"replica from {name} (rev {float(rep.get('rev') or 0):.0f})")
        self.fire("replica")

    def sync_replica(self):
        if not self.capable():
            return
        rev, digest = self._replica_state()
        best, best_rev = None, -1.0
        for name, r in self.remote.items():
            if not (r.get("capable") and self.is_alive(name) and name in self.dir and self._same_domain(name)):
                continue
            h = r.get("hash") or ""
            rrev = float(r.get("rev") or 0)
            if h:
                if h == digest or self.last_pulled.get(name) == h:
                    continue
            elif rrev <= rev:
                continue
            if rrev > best_rev:
                best, best_rev = name, rrev
        if best:
            try:
                self.pull_replica(best)
            except Exception as e:
                self.log(f"replica from {best}: {e!r}")

    def fire(self, event):
        for fn in self.hooks:
            try:
                fn(event)
            except Exception as e:
                self.log(f"hook {event}: {e!r}")

    # ---------- coordinator ----------
    def rev_of(self, name):
        return self.replica_rev() if name == self.me else (self.remote.get(name) or {}).get("rev", 0)

    def coordinator(self):
        cap = [e for e in self.entries()
               if e.get("capable") and self.is_alive(e["name"]) and self._same_domain(e["name"])]
        pool = [e for e in cap if self.rev_of(e["name"]) > 0] or cap
        pool.sort(key=lambda e: (-prio(e), _num(e.get("enrolled")), e["name"]))
        return pool[0]["name"] if pool else None

    def is_coordinator(self):
        return self.coordinator() == self.me

    def capable_peers(self):
        return [e["name"] for e in sorted(self.entries(), key=lambda e: -prio(e))
                if e.get("capable") and e["name"] != self.me and self.is_alive(e["name"]) and self._same_domain(e["name"])]

    def call(self, path, method="GET", body=None, headers=None, timeout=10):
        """Request to the coordinator; if it does not respond, to another capable hub online."""
        c = self.coordinator()
        order = [c] if c and c != self.me else []
        order += [e["name"] for e in sorted(self.entries(), key=lambda e: -prio(e))
                  if e.get("capable") and e["name"] not in order and e["name"] != self.me and self.is_alive(e["name"])
                  and self._same_domain(e["name"])]
        if not order:  # no directory yet: the enrollment seed
            seed = self.srv.load_conf().get("master")
            if seed:
                req = urllib.request.Request(seed.rstrip("/") + path, data=body, method=method,
                                             headers={"X-Hub-From": self.me, **(headers or {})})
                return tailnet.urlopen(req, timeout=timeout)
            raise ConnectionError("no coordinator online")
        err = None
        for name in order:
            try:
                return self.open(name, path, method, body, headers, timeout)
            except urllib.error.HTTPError:
                raise
            except Exception as e:
                err = e
        raise ConnectionError(f"no coordinator hub is responding ({type(err).__name__})")

    # ---------- network services and blobs ----------
    def route_map(self):
        """Routes of this hub (with their local destination), cached for 60 s so as not to probe on every gossip."""
        now = time.time()
        if now - getattr(self, "_routes_cache", (0, []))[0] < 60:
            return self._routes_cache[1]
        try:
            routes = self.core.data("").get("routes") or []
        except Exception:
            routes = []
        self._routes_cache = (now, routes)
        return routes

    # ---------- service labels: custom title and description ----------
    def label_for(self, key) -> dict:
        """Effective label of a service key (the catalogue key, for example "10000")."""
        rec = self.labels.get(str(key or ""))
        return crdt.label_effective(rec) if rec else crdt.label_effective({})

    def labels_view(self) -> dict:
        return {k: crdt.label_effective(v) for k, v in self.labels.items() if isinstance(v, dict)}

    def merge_labels(self, remote) -> bool:
        """Integrates another hub's labels (gossip or relay). Idempotent."""
        with self.lock:
            merged = crdt.label_merge(self.labels, remote or {})
            if merged == self.labels:
                return False
            self.labels = merged
            for rec in merged.values():
                for f in crdt.LABEL_FIELDS:
                    v = rec.get(f)
                    if isinstance(v, dict) and isinstance(v.get("hlc"), str):
                        self.hlc.observe(v["hlc"])
            self._save_json(self.data / "labels.json", self.labels)
            self._routes_cache = (0.0, [])
            self._svc_cache = (0.0, [])
        return True

    def set_route_order(self, ids, announce=True) -> dict:
        """Reorders the routes this hub shows; routes it does not send keep their slots."""
        with self.lock:
            self.route_order = crdt.order_merge(self.route_order, ids)
            self.order_clock = self.hlc.tick()
            self._save_json(self.data / "route_order.json", {"ids": self.route_order, "clock": self.order_clock})
            self._routes_cache = (0.0, [])
            doc = {"ids": list(self.route_order), "clock": self.order_clock}
        if announce:
            def work():
                for n in self._own_hubs():
                    try:
                        self.send_message(n, "route_order", doc)
                    except Exception as e:
                        self.log(f"route order to {n}: {e!r}")
            threading.Thread(target=work, daemon=True).start()
        return doc

    def merge_route_order(self, ro) -> bool:
        """Adopts another hub's route order (gossip or relay) if its clock is newer."""
        if not isinstance(ro, dict):
            return False
        clock, ids = str(ro.get("clock") or ""), ro.get("ids")
        if not isinstance(ids, list) or not clock:
            return False
        with self.lock:
            if self.order_clock and not crdt.newer(clock, self.order_clock):
                return False
            self.hlc.observe(clock)
            self.route_order = crdt.order_merge([], [x for x in ids if isinstance(x, str)])
            self.order_clock = clock
            self._save_json(self.data / "route_order.json", {"ids": self.route_order, "clock": self.order_clock})
            self._routes_cache = (0.0, [])
        return True

    # ---------- interface preferences: one record for every hub and device ----------
    UI_FIELDS = {"theme": ("acid", "abyss", "paper"), "mascots": (True, False), "pixel": (True, False)}

    def set_ui(self, values) -> dict:
        clean = {k: v for k, v in (values or {}).items() if k in self.UI_FIELDS and v in self.UI_FIELDS[k]}
        if not clean:
            return self.ui
        with self.lock:
            self.ui = {"values": {**self.ui.get("values", {}), **clean}, "clock": self.hlc.tick()}
            self._save_json(self.data / "ui.json", self.ui)
            doc = dict(self.ui)

        def work():
            for n in self._own_hubs():
                try:
                    self.send_message(n, "ui", doc)
                except Exception as e:
                    self.log(f"interface preferences to {n}: {e!r}")
        threading.Thread(target=work, daemon=True).start()
        return doc

    def merge_ui(self, doc) -> bool:
        if not isinstance(doc, dict) or not isinstance(doc.get("values"), dict) or not doc.get("clock"):
            return False
        clock = str(doc["clock"])
        with self.lock:
            if self.ui.get("clock") and not crdt.newer(clock, self.ui["clock"]):
                return False
            self.hlc.observe(clock)
            values = {k: v for k, v in doc["values"].items() if k in self.UI_FIELDS and v in self.UI_FIELDS[k]}
            self.ui = {"values": values, "clock": clock}
            self._save_json(self.data / "ui.json", self.ui)
        return True

    def set_label(self, key, name=None, about=None, by="", announce=True, hidden=None) -> dict:
        """Sets a service's custom title/description/visibility. An empty string clears that field."""
        key = str(key or "").strip()
        if not key or len(key) > 200:
            raise ValueError("invalid service id")
        for field, value in (("name", name), ("about", about)):
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"{field} must be text")
            raw = value.replace("\r\n", "\n").replace("\r", "\n").strip()
            if len(raw) > crdt.LABEL_LIMITS[field]:
                raise ValueError(f"{field} is too long (max {crdt.LABEL_LIMITS[field]} characters)")
        with self.lock:
            if key not in self.labels and len(self.labels) >= crdt.LABEL_TOTAL:
                raise ValueError("too many custom labels")
            rec = dict(self.labels.get(key)) if isinstance(self.labels.get(key), dict) else {}
            now = time.time()
            if name is not None:
                rec["name"] = {"v": crdt.label_value("name", name), "hlc": self.hlc.tick()}
            if about is not None:
                rec["about"] = {"v": crdt.label_value("about", about), "hlc": self.hlc.tick()}
            if hidden is not None:
                rec["hidden"] = {"v": bool(hidden), "hlc": self.hlc.tick()}
            rec["by"] = str(by or "")[:120]
            rec["ts"] = now
            merged = dict(self.labels)
            merged[key] = rec
            self.labels = merged
            self._save_json(self.data / "labels.json", self.labels)
            self._routes_cache = (0.0, [])
            self._svc_cache = (0.0, [])
        if announce:
            self.announce_labels()
        return self.label_for(key)

    def announce_labels(self):
        """Sends the label map to every hub of this tailnet now. Gossip also carries it later."""
        labels = json.loads(json.dumps(self.labels))

        def work():
            for n in self._own_hubs():
                try:
                    self.send_message(n, "labels", labels)
                except Exception as e:
                    self.log(f"labels to {n}: {e!r}")
        threading.Thread(target=work, daemon=True).start()

    def known_service_keys(self, fresh=False) -> set:
        """Service keys that exist in the catalogue or in this hub, including stale labels."""
        keys = set(self.labels)
        if fresh:  # a route may have just been discovered: do not trust the 60 s cache
            self._routes_cache = (0.0, [])
            self._svc_cache = (0.0, [])
        try:
            for r in self.route_map():
                key = r.get("id") or r.get("key")
                if key:
                    keys.add(str(key))
        except Exception:
            pass
        for r in self.remote.values():  # also services last seen on a hub that is offline now
            for s in r.get("services") or []:
                if isinstance(s, dict) and s.get("key"):
                    keys.add(str(s["key"]))
        return {k for k in keys if k}

    def services_summary(self):
        now = time.time()
        if now - self._svc_cache[0] < 60:
            return self._svc_cache[1]
        out = []
        try:
            for r in self.route_map():
                if r.get("hidden") or r.get("kind") == "static":
                    continue
                if (r.get("path") or "").rstrip("/") == "/hub":
                    continue
                key = r.get("id") or r.get("key")
                lab = self.label_for(key)
                about = lab["about"] if lab["about"] is not None else (r.get("about") or "")
                out.append({"key": key, "name": lab["name"] or r.get("name") or key,
                            "about": about, "custom": bool(lab.get("custom")),
                            "up": bool(r.get("up")), "ms": r.get("ms"), "port": r.get("port"),
                            "path": r.get("path"), "url": r.get("url") or ""})
        except Exception:
            out = []
        self._svc_cache = (now, out)
        return out

    def catalog(self):
        cat = {}

        def add(hub, svc):
            key = svc.get("key")
            if not key:
                return
            e = cat.setdefault(key, {"key": key, "name": svc.get("name") or key,
                                     "about": svc.get("about") or "", "hubs": {}})
            if not e.get("about") and svc.get("about"):
                e["about"] = svc["about"]
            e["hubs"][hub] = {"up": bool(svc.get("up")), "ms": svc.get("ms"), "url": svc.get("url")}
        for s in self.services_summary():
            add(self.me, s)
        for n, r in self.remote.items():
            if not self.is_alive(n):
                continue
            for s in r.get("services") or []:
                add(n, s)
        out = []
        for e in cat.values():
            hubs = [(n, v) for n, v in e["hubs"].items() if self.is_alive(n)]
            if not hubs:
                continue
            # this tailnet's hubs first: api/goto only goes to them (a paired tailnet's service is listed only)
            hubs.sort(key=lambda nv: (self.is_foreign(nv[0]), not nv[1].get("up"),
                                      nv[1].get("ms") if isinstance(nv[1].get("ms"), (int, float)) else 9999))
            best = hubs[0]
            # the service's own address, for the interface to open it directly (not inside the hub's origin)
            url = best[1].get("url")
            dom = (self.dir.get(best[0]) or {}).get("domain") or self.domain()
            if not (isinstance(url, str) and not self.is_foreign(best[0]) and self.url_ok(url, dom)):
                url = ""
            lab = self.label_for(e["key"])
            name = lab["name"] or e.get("name") or e["key"]
            about = lab["about"] if lab["about"] is not None else (e.get("about") or "")
            out.append({"key": e["key"], "name": name, "about": about,
                        "custom": bool(lab.get("custom")), "by": lab.get("by") or "",
                        "ts": lab.get("ts"), "hub": best[0], "url": url, "online": bool(best[1].get("up")),
                        "ms": best[1].get("ms"), "hubs": [n for n, _ in hubs],
                        "up_hubs": sum(1 for _, v in hubs if v.get("up"))})
        return sorted(out, key=lambda e: (not e["online"], e["name"].lower()))

    def blob_hashes(self):
        return [b.get("sha") for b in self.blobs.list() if b.get("sha")]

    def blob_holders(self, sha):
        out = [self.me] if self.blobs.has(sha) else []
        for n, r in self.remote.items():
            if self.is_alive(n) and sha in (r.get("blobs") or []):
                out.append(n)
        return out

    def ensure_blob(self, sha):
        try:
            path = self.blobs.file(sha)
        except ValueError as e:
            raise FileNotFoundError(sha) from e
        if path.exists():
            return path
        from .adapters import disk_room
        for n in self.blob_holders(sha):
            if n == self.me:
                continue
            tmp = self.data / f"fetch-{secrets.token_hex(6)}.part"
            try:
                with self.open(n, f"/api/blob/{sha}", timeout=120) as r:
                    full = disk_room(self.data, int(r.headers.get("Content-Length") or 0))
                    if full:
                        raise OSError(full)
                    h = hashlib.sha256()
                    with tmp.open("wb") as f:  # streamed: a blob can be bigger than this hub's memory
                        while True:
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            h.update(chunk)
                            f.write(chunk)
                if h.hexdigest() != sha:
                    continue
                self.blobs.put(tmp, name=sha[:12], by=n)
                return self.blobs.file(sha)
            except Exception as e:
                self.log(f"blob {sha[:10]} from {n}: {e!r}")
            finally:
                tmp.unlink(missing_ok=True)
        raise FileNotFoundError(sha)

    def replicate_blob(self, sha, skip=()):
        if BLOB_RF <= 1:
            return
        threading.Thread(target=self._replicate_blob, args=(sha, tuple(skip)), daemon=True).start()

    def _replicate_blob(self, sha, skip):
        try:
            path = self.blobs.file(sha)
            if not path.exists():
                return
            meta = self.blobs.meta(sha) or {}
            targets = [n for n in self.capable_peers()
                       if n not in skip and (self.remote.get(n) or {}).get("protocol", 0) >= 2]
            random.shuffle(targets)
            done = 0
            for n in targets:
                if done >= BLOB_RF - 1:
                    break
                if self.blobs.has(sha) and sha in (self.remote.get(n, {}).get("blobs") or []):
                    done += 1
                    continue
                try:
                    self._put_blob_stream(n, sha, path, meta.get("name") or "")
                    done += 1
                    self.log(f"blob {sha[:10]} replicated to {n}")
                except Exception as e:
                    self.log(f"blob {sha[:10]} to {n}: {e!r}")
        except Exception as e:
            self.log(f"replication {sha[:10]}: {e!r}")

    def _put_blob_stream(self, name, sha, path: Path, fname: str):
        import http.client
        import ssl
        u = urllib.parse.urlparse(self.base(name))
        conn = (http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=120, context=ssl.create_default_context())
                if u.scheme == "https" else http.client.HTTPConnection(u.hostname, u.port or 80, timeout=120))
        target = f"/api/mesh/blob/{sha}"
        try:
            conn.putrequest("PUT", target)
            conn.putheader("Content-Length", str(path.stat().st_size))
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("X-Hub-From", self.me)
            conn.putheader("X-Hub-Blob-Name", fname[:120])
            conn.putheader(AUTH_HEADER, self.identity.token("PUT", target, "stream"))
            conn.endheaders()
            with path.open("rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    conn.send(chunk)
            r = conn.getresponse()
            body = r.read()
            if r.status >= 400:
                raise ConnectionError(f"HTTP {r.status}: {body[:120]!r}")
        finally:
            conn.close()

    def _monitor_summary(self):
        a = self.core.by_name.get("monitor")
        try:
            return a.summary() if a and hasattr(a, "summary") else {}
        except Exception:
            return {}

    def monitor_view(self):
        acc = {}

        def add(hub, data):
            for peer, s in (data or {}).items():
                try:
                    ts, up, ms, via = s
                except Exception:
                    continue
                if time.time() - float(ts) > 3600:
                    continue
                acc.setdefault(peer, []).append({"hub": hub, "ts": ts, "up": bool(up), "ms": ms, "via": via})
        add(self.me, self._monitor_summary())
        for n, r in self.remote.items():
            if self.is_alive(n):
                add(n, r.get("monitor"))
        out = {}
        for peer, rows in acc.items():
            ms = sorted(r["ms"] for r in rows if r.get("ms") is not None)
            out[peer] = {"hubs": len(rows), "up": any(r["up"] for r in rows),
                         "ms": ms[len(ms) // 2] if ms else None, "samples": rows}
        return {"peers": out}

    # ---------- store-and-forward relay ----------
    def _own_hubs(self):
        return [e["name"] for e in self.entries()
                if e["name"] != self.me and self._same_domain(e["name"])]

    def send_message(self, to, kind, body):
        msg = {"id": secrets.token_hex(8), "to": to, "kind": kind, "body": body,
               "ts": time.time(), "ttl": FORWARD_TTL, "hops": HOP_MAX,
               "from": self.me, "domain": self.domain()}
        if self._deliver_direct(msg):
            return True
        self.forward.enqueue(to, kind, body, **{"from": self.me, "domain": self.domain()})
        return False

    def try_message(self, to, kind, body) -> bool:
        """Direct delivery without leaving a copy in the queue (to try several paths without duplicating)."""
        msg = {"id": secrets.token_hex(8), "to": to, "kind": kind, "body": body,
               "ts": time.time(), "ttl": FORWARD_TTL, "hops": HOP_MAX,
               "from": self.me, "domain": self.domain()}
        return self._deliver_direct(msg)

    def _deliver_direct(self, msg) -> bool:
        to = msg.get("to")
        if to in (None, "", "*") or to == self.me:
            return self._deliver_local(msg)
        try:
            body = json.dumps(msg).encode()
            with self.open(to, "/api/mesh/relay", "POST", body, {"Content-Type": "application/json"}, timeout=8) as r:
                return bool(json.load(r).get("ok"))
        except Exception:
            return False

    def _relay_hop(self, msg) -> bool:
        for n in self.capable_peers():
            if n == msg.get("to"):
                continue
            try:
                body = json.dumps({**msg, "hops": max(0, int(msg.get("hops") or 0) - 1)}).encode()
                with self.open(n, "/api/mesh/relay", "POST", body, {"Content-Type": "application/json"}, timeout=8) as r:
                    if json.load(r).get("ok"):
                        return True
            except Exception:
                continue
        return False

    def relay(self, msg: dict, peer_domain: str | None = None) -> dict:
        """`peer_domain`: signed by a hub of a paired tailnet. The message then counts as that tailnet's,
        whatever it says, so it has no authority here, and it is not carried on to other hubs."""
        if not isinstance(msg, dict):
            return {"ok": False, "error": "invalid message"}
        if peer_domain is not None:
            msg = {**msg, "domain": peer_domain}
            if msg.get("to") not in (None, "", "*", self.me):
                return {"ok": False, "error": "no path"}
        mid = msg.get("id")
        now = time.time()
        if mid:
            if mid in self._seen_msgs:
                return {"ok": True, "duplicate": True}
            self._seen_msgs[mid] = now
            if len(self._seen_msgs) > 2000:
                self._seen_msgs = {k: t for k, t in self._seen_msgs.items() if now - t < 86400}
        to = msg.get("to")
        if to in (None, "", "*") or to == self.me:
            ok = self._deliver_local(msg)
            return {"ok": bool(ok), "delivered": bool(ok)}
        if int(msg.get("hops") or 0) <= 0:
            return {"ok": False, "error": "no hops left"}
        nxt = {**msg, "hops": int(msg["hops"]) - 1}
        if self._deliver_direct(nxt):
            return {"ok": True}
        if self._relay_hop(nxt):
            return {"ok": True}
        return {"ok": False, "error": "no path"}

    def _deliver_local(self, msg) -> bool:
        kind, body = msg.get("kind"), msg.get("body") or {}
        dom = msg.get("domain")
        if dom and dom != self.domain():
            return False  # a peer from another tailnet has no authority over the home control plane
        if kind in ("approval", "settings", "clip", "labels", "route_order", "ui"):
            origin = msg.get("from") or ""
            if origin not in self._own_hubs():  # old messages did not carry "from"; these do
                return False
        try:
            if kind == "approval":
                return self.approvals.merge(body)
            if kind == "notify":
                pa = self.core.by_name.get("push")
                if not pa or not getattr(pa, "local", None):
                    return False  # this hub cannot send push: let the origin try another
                fn = self.core.ctx.get("notify")
                if not fn:
                    return False
                fn(body.get("event", "mesh"), body.get("title", "Notification"),
                   body.get("body", ""), body.get("url") or "/hub/")
                return True
            if kind == "clip":
                d = self._clip_state()
                raw = self._load_json(self.data / "clipboard.json", {}) or {}
                merged = crdt.clip_merge(d, body)
                self._save_json(self.data / "clipboard.json", self._clip_doc(merged, raw))
                self.fire("replica")
                return True
            if kind == "blob":
                self.ensure_blob(body.get("sha") or "")
                return True
            if kind == "settings":
                if body.get("values"):
                    self.approvals.merge_quorum(body["values"], body.get("clock") or "")
                return True
            if kind == "labels":
                return self.merge_labels(body)
            if kind == "route_order":
                self.merge_route_order(body)
                return True
            if kind == "ui":
                self.merge_ui(body)
                return True  # an older order is not a failure: the newer one is already here
        except Exception as e:
            self.log(f"relay {kind}: {e!r}")
        return False

    def bump_incarnation(self):
        self.inc += 1
        with self.lock:
            e = self.dir.get(self.me)
            if e:
                e["inc"] = self.inc
                e.setdefault("hlc", {})["inc"] = self.hlc.tick()
                e["updated"] = time.time()
                self.save()
        self.log(f"another hub marked me offline; incarnation {self.inc}")

    # ---------- approvals ----------
    def _approval_changed(self, p):
        for n in self._own_hubs():
            self.send_message(n, "approval", p)

    def _exec_remove(self, args):
        self.set_hub(args.get("name"), {"remove": True}, approved=True)
        return {"ok": True, "removed": args.get("name")}

    def _suspects(self):
        now = time.time()
        out = []
        for e in self.entries():
            n = e.get("name")
            if not n or n == self.me:
                continue
            if now - self.alive.get(n, 0) > ALIVE * 1.2:
                out.append(n)
        return out[:20]

    # ---------- gossip ----------
    def payload(self, public=False):
        """Mesh state. `public` (peer from another tailnet) omits the internal control plane."""
        rev, digest = self._replica_state()
        out = {"name": self.me, "protocol": PROTOCOL, "domain": self.domain(),
               "release": self.running_release, "ok": self.ok_version,
               "rev": rev, "hash": digest, "capable": self.capable(),
               "coordinator": self.coordinator(), "directory": self.dir,
               "labels": self.labels,
               "route_order": {"ids": self.route_order, "clock": self.order_clock},
               "ui": self.ui,
               "owners": sorted(self.owners | set(self.core.owners)),
               "owner_ips": sorted(self.owner_ips), "time": time.time(),
               "bad": self.bad,
               "quorum": {"values": self.approvals.quorum, "clock": self.approvals.clock},
               "services": self.services_summary(), "monitor": self._monitor_summary(),
               "blobs": self.blob_hashes(), "suspect": self._suspects()}
        if public:  # a paired tailnet sees the directory and services, not who is in charge here
            for k in ("owners", "owner_ips", "bad", "quorum", "suspect", "labels"):
                out.pop(k, None)
        return out

    def targets(self):
        out = {}
        for e in self.entries():
            if e["name"] == self.me or not e.get("url"):
                continue
            if not self._same_domain(e["name"]) and not self.peering.allowed(e.get("domain") or "", "mesh"):
                continue
            if not self.url_ok(e["url"], e.get("domain") or ""):
                continue  # never gossip to an address outside the tailnet
            out[e["name"]] = e["url"]
        c = self.srv.load_conf()
        known = {u.rstrip("/") for u in out.values()}
        for s in [c.get("master")] + list(c.get("seeds") or []):
            if s and s.rstrip("/") not in known:
                out["seed:" + s] = s
        return out

    def fetch(self, item):
        key, url, body = item
        try:
            headers = {"Content-Type": "application/json"}
            if key.startswith("seed:"):
                headers["X-Hub-From"] = self.me
            else:
                headers.update(self._auth("POST", "/api/mesh", body))
            req = urllib.request.Request(url.rstrip("/") + "/api/mesh", data=body, method="POST", headers=headers)
            with tailnet.urlopen(req, timeout=8) as r:
                return key, json.load(r)
        except Exception as e:
            return key, e

    def round(self):
        self.learn_owners()
        self.refresh_self()
        self._rounds += 1
        body = json.dumps(self.payload()).encode()
        public = None
        t = self.targets()
        names = [k for k in t if not k.startswith("seed:")]
        picks = random.sample(names, min(GOSSIP_K, len(names)))
        if self._rounds % max(1, ANTI) == 0:
            picks = list(t.keys())
        elif not picks and t:
            picks = list(t.keys())[:1]
        items = []
        for k in picks:
            if not k.startswith("seed:") and self.is_foreign(k):  # a paired tailnet: no internal control plane
                public = public or json.dumps(self.payload(public=True)).encode()
                items.append((k, t[k], public))
            else:
                items.append((k, t[k], body))
        if items:
            with ThreadPoolExecutor(max_workers=min(8, len(items))) as ex:
                results = list(ex.map(self.fetch, items))
            for key, p in results:
                if isinstance(p, Exception) or not isinstance(p, dict):
                    continue
                try:
                    if not key.startswith("seed:") and self.is_foreign(key):
                        self.receive(p, peer_domain=(self.dir.get(key) or {}).get("domain") or "?", peer_name=key)
                    else:
                        self.receive(p)
                except Exception as e:
                    self.log(f"payload from {p.get('name') or key}: {e!r}")
            self.refresh_self()
        self.coord = self.coordinator()
        if self.coord != getattr(self, "_coord_prev", "?"):
            self.log(f"coordinator: {self.coord or 'none'}")
            self._coord_prev = self.coord
            self.fire("coordinator")
        if self.capable():
            self.sync_replica()
        self.fire("round")
        self._tick_ok()
        self.approvals.tick()
        self.sync_release()

    def _background(self):
        time.sleep(5)
        while True:
            try:
                self.forward.prune()
                self.forward.process()
                self.approvals.tick()
                self._tick_ok()
            except Exception as e:
                self.log(f"background task: {e!r}")
            time.sleep(20)

    def run(self):
        time.sleep(3)
        while True:
            try:
                self.round()
            except Exception as e:
                self.log(f"round: {e!r}")
            try:
                self._check_disk()
            except Exception as e:
                self.log(f"code watchdog: {e!r}")
            time.sleep(EVERY)

    # ---------- enrollments and changes from the interface ----------
    def enroll(self, ip, body):
        name, dns = body.get("name"), body.get("dns")
        ts = self.core.by_name.get("tailscale")
        if ts:  # with the CLI, Tailscale provides the name, not whoever requests the enrollment
            peer = next((p for p in (ts.status().get("Peer") or {}).values()
                         if ip in (p.get("TailscaleIPs") or [])), None)
            if not peer:
                raise PermissionError("enrollment is done from the device being added (over the tailnet)")
            name, dns = peer.get("HostName"), (peer.get("DNSName") or "").rstrip(".")
        if not name or not isinstance(name, str) or name == self.me:
            raise ValueError("invalid device name")
        prev = self.dir.get(name) or {}
        if not ts and prev.get("pub") and body.get("pub") != prev["pub"] and prev.get("ip") != ip:
            # without the CLI the name is the caller's word: it must not take over another hub's identity
            raise PermissionError("that hub name belongs to another device")
        url = body.get("url") or ""
        if not self.url_ok(url):  # the URL every hub will call: only a tailnet address
            url = f"http://{dns or ip}:8790/hub/"
        plat = body.get("platform", "")
        with self.lock:
            old = self.dir.get(name) or {}
            e = {"name": name, "ip": ip, "dns": dns or urllib.parse.urlparse(url).hostname, "url": url,
                 "kind": body.get("kind", "core"), "platform": plat, "os": body.get("os", ""),
                 "capable": bool(body.get("capable")), "domain": self.domain(),
                 "priority": int(body.get("priority") or old.get("priority") or default_priority(plat)),
                 "enrolled": old.get("enrolled") or time.time(), "updated": time.time()}
            if body.get("pub"):  # the enrollment already brings its key: the first signature can be verified
                e["pub"] = body["pub"]
                self.pins.set(name, body["pub"])
            e["hlc"] = {k: self.hlc.tick() for k in MERGEABLE if k in e}
            self.dir[name] = e
            self.save()
        self.log(f"{name} joined ({plat or '?'}) at {url}")
        notify = self.core.ctx.get("notify")
        if notify:
            notify("new_device", "New hub on the mesh", f"{name} joined the hub mesh.", "/hub/")
        return {"name": name, "ip": ip, "dns": e["dns"], "domain": self.domain(), "directory": self.dir,
                "owners": sorted(self.owners), "token": secrets.token_urlsafe(16), "allow": []}

    def remove_hub(self, name, by=""):
        res = self.approvals.request("mesh_remove", {"name": name}, self.me, by)
        if not res.get("approved"):
            return {"pending": res, "need": res.get("need"), "view": self.view()}
        return self.set_hub(name, {"remove": True}, approved=True)

    def set_hub(self, name, body, by="", approved=False):
        """Changes a hub's entry. Removing one needs the approval quorum: `approved` is only set by
        this hub's own code after the quorum agreed, never from a request."""
        if body.get("remove") and not approved:
            res = self.approvals.request("mesh_remove", {"name": name}, self.me, by)
            if not res.get("approved"):
                return {"pending": res, "need": res.get("need"), "view": self.view()}
        with self.lock:
            e = self.dir.get(name)
            if not e:
                raise KeyError(name)
            if "priority" in body:
                e["priority"] = max(0, min(1000, int(body["priority"])))
                e.setdefault("hlc", {})["priority"] = self.hlc.tick()
            if body.get("remove"):
                e["removed"] = True
                e.setdefault("hlc", {})["removed"] = self.hlc.tick()
            if body.get("restore"):
                e["removed"] = False
                e.setdefault("hlc", {})["removed"] = self.hlc.tick()
            e["updated"] = time.time()
            self.save()
        self.coord = self.coordinator()
        return self.view()

    # ---------- peering / single entry ----------
    def invite(self):
        e = self.dir.get(self.me) or {}
        url = e.get("url") or self.srv.load_conf().get("url")
        if not url:
            raise ValueError("this hub does not have a mesh URL yet")
        return make_invite(self.domain(), self.me, url, self.identity.pub_b64, self.identity.sign)

    def peer_add(self, invite):
        return self.peering.add(invite, self.domain())

    def peer_remove(self, domain):
        return self.peering.remove(domain)

    def view(self):
        coord = self.coordinator()
        mine = self.running_release
        # A rename can leave old names behind in peers' CRDT directories. They
        # are aliases of the same identity, not extra servers. Collapse them by
        # public key (or DNS when an old entry has no key) before presenting
        # the mesh; the current name of this hub always wins its own group.
        groups = {}
        for entry in self.dir.values():
            key = entry.get("pub") or (
                "dns:" + entry["dns"] if entry.get("dns") else "name:" + entry["name"]
            )
            groups.setdefault(key, []).append(entry)
        chosen, aliases = [], {}
        for group in groups.values():
            if len(group) == 1:
                pick = group[0]
            else:
                pick = next((e for e in group if e.get("name") == self.me), None)
                if pick is None:
                    active = [e for e in group if not e.get("removed")] or group
                    pick = max(active, key=lambda e: (_num(e.get("updated")), _num(e.get("enrolled"))))
            chosen.append(pick)
            for entry in group:
                aliases[entry["name"]] = pick["name"]
        chosen.sort(key=lambda e: (e.get("removed") is True, -prio(e), e["name"]))
        coord = aliases.get(coord, coord)
        hubs = []
        for e in chosen:
            r = self.remote.get(e["name"]) or {}
            rel = mine if e["name"] == self.me else (r.get("release") or {})
            hubs.append({**{k: e.get(k) for k in ("name", "url", "ip", "dns", "kind", "platform", "os",
                                                   "priority", "capable", "enrolled", "removed", "domain", "pub")},
                         "online": self.is_alive(e["name"]), "here": e["name"] == self.me,
                         "coordinator": e["name"] == coord,
                         "seen": time.time() if e["name"] == self.me else self.alive.get(e["name"]),
                         "version": rel.get("version"), "stamp": rel.get("stamp"), "seq": rel.get("seq"),
                         "protocol": (PROTOCOL if e["name"] == self.me else r.get("protocol")) or 0,
                         "ok": (self.ok_version == self.running_version) if e["name"] == self.me
                         else (r.get("ok") == rel.get("version")),
                         "services": len((r.get("services") if e["name"] != self.me else self.services_summary()) or []),
                         "blobs": len((r.get("blobs") if e["name"] != self.me else self.blob_hashes()) or []),
                         "rev": self.rev_of(e["name"]) if e.get("capable") else None})
        return {"me": self.me, "coordinator": coord, "release": mine, "protocol": PROTOCOL,
                "domain": self.domain(), "peers": self.peering.list(),
                "quorum": {a: int(v) for a, v in self.approvals.quorum.items()},
                "pending": self.approvals.public(), "bad": sorted(self.bad),
                "about": {"identity": self.identity.pub_b64[:16] + "…",
                          "ok": self.ok_version == self.running_version, "nodes": len(chosen)},
                "hubs": hubs}

    # ---------- compatibility with the interface ----------
    def learn_owners(self):
        ts = self.core.by_name.get("tailscale")
        if not ts:
            return
        st = ts.status()
        me = st.get("Self") or {}
        uid = me.get("UserID")
        login = ((st.get("User") or {}).get(str(uid)) or {}).get("LoginName")
        if login:
            self.owners.add(login)
        ips = set(me.get("TailscaleIPs") or [])
        for p in (st.get("Peer") or {}).values():
            if p.get("UserID") == uid and not p.get("ShareeNode"):
                ips |= set(p.get("TailscaleIPs") or [])
        self.owner_ips = ips

    def trusted_ips(self):
        """Addresses that pass as the owner: the owner's devices and the hubs of this tailnet. A hub of a
        paired tailnet is in the directory too, but only reaches the federated APIs (server._peer_ok)."""
        mine = self.domain()

        def own(e):
            dom = e.get("domain")
            if dom in (None, "", mine):
                return True
            # a hub that has not learned its tailnet yet ("local") still trusts its mesh, never a paired one
            return mine == "local" and self.peering.get(dom) is None
        return self.owner_ips | {e["ip"] for e in self.entries() if isinstance(e.get("ip"), str) and e["ip"] and own(e)}

    def note_unknown(self, ip):
        """Log a refused direct request once per address and hour, so a closed door is explainable."""
        now, seen = time.time(), self.__dict__.setdefault("_unknown_seen", {})
        if now - seen.get(ip, 0) < 3600:
            return
        if len(seen) > 256:
            seen.clear()
        seen[ip] = now
        hint = " (this hub knows no devices yet: enroll it, or open it from this device)"             if not self.trusted_ips() else ""
        self.log(f"refused a direct request from {ip}: not an owner device or a hub of this mesh{hint}")
