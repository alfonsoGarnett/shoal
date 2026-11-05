"""Hub mesh (the "nodes" capability): CRDT directory, enrollment, proxy, replica, federated catalog,
blobs, relay, peering and approvals. On every hub.

The logic lives in core/mesh.py; this adapter exposes the HTTP contract:
  POST api/mesh                gossip between hubs: merges the payload and returns its own
  GET  api/mesh                the same, read-only
  GET  api/mesh/replica        shared state (CRDT) for another capable hub
  GET  api/mesh/view           view for the interface (Control › Network)
  POST api/mesh/hub            priority / removal / restore a hub (removal may require quorum)
  GET  api/catalog             routes of the whole network, with the hub that serves them best
  GET  api/labels              custom titles and descriptions of the discovered services
  POST api/labels              set (or clear) the title/description of a service; replicates to the mesh
  GET  api/goto/<route>[/...]   proxy to that route through the hub that has it (works from any hub)
  GET  api/svc/<route>[/...]    local proxy to a route of this hub (used by api/goto)
  POST api/blob, GET api/blob/<sha>, GET api/blobs, POST api/blobs/delete
  GET  api/monitor/federation  latency/status seen by several hubs
  POST api/mesh/relay          store-and-forward relay between hubs
  GET  api/mesh/invite, POST api/mesh/peer, POST api/mesh/peer/remove
  GET  api/mesh/approvals, POST api/mesh/approvals/<id>/approve|deny
  POST api/mesh/settings       quorum per action (1-3 hubs)
  GET  api/fleet, api/nodes    hubs of the network ("Network" selector, terminal)
  POST api/nodes/enroll        enrollment of a new device (from that device)
  *    api/nodes/<hub>/...     proxy to hub <hub>
"""
from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from ..approvals import ACTIONS
from ..mesh import BLOB_RF
from . import Adapter, disk_room

PROXY_HEADERS = ("Content-Type", "Content-Length", "Content-Disposition", "Location",
                 "Cache-Control", "Set-Cookie")


class Impl(Adapter):
    name = "mesh"
    caps = ["nodes", "mesh", "catalog", "labels", "blobs", "approvals", "relay", "peering", "goto"]

    def probe(self):
        self.mesh = self.ctx["mesh"]
        self.blobs = self.ctx["blobs"]
        threading.Thread(target=self.watch, daemon=True).start()
        return True

    # ---------- directory for the interface ----------
    def fleet(self):
        v, now = self.mesh.view(), time.time()

        def seen(name):  # seconds since this hub last heard from it (the live mesh map animates contacts)
            t = self.mesh.alive.get(name)
            return 0 if name == self.mesh.me else (round(now - t, 1) if t else None)
        return {"coordinator": v["coordinator"], "domain": v.get("domain"),
                "version": v["release"].get("version"),
                "hubs": [{"name": h["name"], "kind": h.get("kind"), "os": h.get("os"), "online": h["online"],
                          "url": h.get("url"), "coordinator": h["coordinator"], "here": h["here"],
                          "domain": h.get("domain"), "ok": h.get("ok"),
                          "pub": (self.mesh.dir.get(h["name"]) or {}).get("pub"), "seen": seen(h["name"])}
                         for h in v["hubs"] if not h.get("removed") and h.get("url")]}

    def node_list(self):
        others = [e["name"] for e in self.mesh.entries() if e["name"] != self.mesh.me]

        def info(name):
            if not self.mesh.is_alive(name):
                return {"name": name, "online": False}
            try:
                with self.mesh.open(name, "/node", timeout=5) as r:
                    return {"name": name, "online": True, **json.load(r)}
            except Exception as e:
                return {"name": name, "online": False, "error": type(e).__name__}
        with ThreadPoolExecutor(max_workers=max(1, len(others))) as ex:
            return list(ex.map(info, others))

    # ---------- network notifications ----------
    def watch(self):
        """New devices on the tailnet (only the coordinator, which sees the whole tailnet) and routes of
        this hub that go down or come back (each hub its own)."""
        from ..server import CORE
        known_path, prev = self.ctx["data"] / "known_peers.json", {}
        time.sleep(30)
        while True:
            try:
                notify = self.ctx.get("notify") or (lambda *a, **k: None)
                d = CORE.data()
                if self.mesh.is_coordinator() and "tailscale" in CORE.by_name:
                    names = {p["name"] for p in d["peers"]}
                    if known_path.exists():
                        known = set(json.loads(known_path.read_text(encoding="utf-8-sig")))
                        hubs = {e["name"] for e in self.mesh.entries()}
                        for n in sorted(names - known):
                            msg = "It already has a hub." if n in hubs else "Install its hub from the index."
                            notify("new_device", "New device on the tailnet", f"{n} joined. {msg}", "/hub/#equipos")
                        known |= names
                    else:
                        known = names
                    known_path.write_text(json.dumps(sorted(known)), encoding="utf-8")
                for r in d["routes"]:
                    if r.get("hidden") or r["path"].rstrip("/") == "/hub":
                        continue
                    was = prev.get(r["key"])
                    if was is not None and was != r["up"]:
                        notify("service_down", f"{r['name']} {'is responding again' if r['up'] else 'is not responding'}",
                               r["url"], "/hub/")
                    prev[r["key"]] = r["up"]
            except Exception:
                pass
            time.sleep(60)

    # ---------- proxy to another hub ----------
    def wants_stream(self, path):
        return (path.startswith("/api/nodes/") and path != "/api/nodes/enroll") \
            or path == "/api/blob" or path.startswith("/api/mesh/blob/") \
            or path.startswith("/api/goto/") or path.startswith("/api/svc/")

    def stream(self, path, query, h, length):
        if path.startswith("/api/nodes/"):
            return self.proxy(h, "POST", length)
        if path == "/api/blob":
            return self.blob_upload(h, length)
        if path.startswith("/api/mesh/blob/"):
            sha = path.rsplit("/", 1)[-1]
            return self.blob_upload(h, length, sha=sha, signed=True)
        if path.startswith("/api/goto/"):
            return self.proxy_anycast(h, h.command, length)
        if path.startswith("/api/svc/"):
            return self.proxy_local(h, h.command, length)
        return ("error", 404, "not found")

    def proxy(self, h, method, length=0):
        parts = h.path.split("?", 1)
        path = parts[0][4:] if parts[0].startswith("/hub/") else parts[0]
        segs = path.split("/", 4)  # ['', 'api', 'nodes', name, rest]
        if len(segs) < 5:
            return ("error", 400, "incomplete route")
        name = urllib.parse.unquote(segs[3])
        rest = "/" + segs[4] + (("?" + parts[1]) if len(parts) > 1 else "")
        body = h.rfile.read(length) if method in ("POST", "PUT", "PATCH") and length else \
            (b"" if method in ("POST", "PUT", "PATCH") else None)
        hdrs = {"X-Hub-User": h.headers.get("Tailscale-User-Login") or "local"}
        if h.headers.get("Content-Type"):
            hdrs["Content-Type"] = h.headers["Content-Type"]
        try:
            r = self.mesh.open(name, rest, method, body, hdrs, timeout=150)
        except KeyError:
            return ("error", 404, "device has no hub")
        except urllib.error.HTTPError as e:
            r = e
        except Exception as e:
            return ("error", 504, f"the device is not responding ({type(e).__name__})")
        with r:
            h.send_response(getattr(r, "status", None) or r.code)
            for k in PROXY_HEADERS:
                if r.headers.get(k):
                    h.send_header(k, r.headers[k])
            h.end_headers()
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                h.wfile.write(chunk)
        return ("handled",)

    # ---------- catalog and service proxy ----------
    @staticmethod
    def _split_key(path, prefix):
        path = path[4:] if path.startswith("/hub/") else path
        path = path.split("?", 1)[0]
        tail = path[len(prefix):]
        key, _, rest = tail.partition("/")
        return urllib.parse.unquote(key), ("/" + rest if rest else "/")

    def _local_route(self, key):
        for r in self.mesh.route_map():
            if (r.get("id") or r.get("key")) == key and r.get("kind") != "static":
                return r
        return None

    @staticmethod
    def _query(h):
        q = urllib.parse.urlparse(h.path).query
        return "?" + q if q else ""

    def proxy_local(self, h, method, length=0):
        key, rest = self._split_key(h._route(), "/api/svc/")
        rest += self._query(h)
        route = self._local_route(key)
        if not route:
            return ("error", 404, "that route does not exist on this hub")
        return self._proxy_to(h, method, length, route, rest)

    def proxy_anycast(self, h, method, length=0):
        key, rest = self._split_key(h._route(), "/api/goto/")
        rest += self._query(h)
        entry = next((e for e in self.mesh.catalog() if e["key"] == key), None)
        if not entry:
            return ("error", 404, "no hub currently serves that route")
        # Only to a hub of this tailnet: the request carries the browser's cookies and Authorization,
        # and the answer is shown inside this hub's origin.
        hub = next((n for n in entry.get("hubs") or [] if not self.mesh.is_foreign(n)), None)
        if not hub:
            return ("error", 404, "that service is on a paired tailnet")
        entry = {**entry, "hub": hub}
        if entry["hub"] == self.mesh.me:
            route = self._local_route(key)
            if not route:
                return ("error", 404, "that route does not exist on this hub")
            return self._proxy_to(h, method, length, route, rest)
        body = h.rfile.read(length) if method in ("POST", "PUT", "PATCH") and length else \
            (b"" if method in ("POST", "PUT", "PATCH") else None)
        hdrs = {}
        for k in ("Content-Type", "Accept", "Accept-Language", "Cookie", "Authorization",
                  "X-CSRF-Token", "Origin", "Referer"):
            if h.headers.get(k):
                hdrs[k] = h.headers[k]
        path = f"/api/svc/{urllib.parse.quote(key)}{rest}"
        try:
            r = self.mesh.open(entry["hub"], path, method, body, hdrs, timeout=150)
        except urllib.error.HTTPError as e:
            r = e
        except Exception as e:
            return ("error", 504, f"{entry['hub']} is not responding ({type(e).__name__})")
        with r:
            h.send_response(getattr(r, "status", None) or r.code)
            for k in PROXY_HEADERS:
                if r.headers.get(k):
                    h.send_header(k, r.headers[k])
            h.end_headers()
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                h.wfile.write(chunk)
        return ("handled",)

    def _proxy_to(self, h, method, length, route, rest):
        import http.client
        import ssl
        target = (route or {}).get("target") or ""
        if not target:
            return ("error", 404, "that route has no target")
        base = target.rstrip("/")
        mount_path = (route or {}).get("path") or "/"
        mount = mount_path.strip("/")
        if mount:  # the route may be mounted on a subpath (e.g. /octo/)
            base += "/" + mount
        if rest in ("", "/"):
            rest = "/" if mount_path.endswith("/") else ""
        elif not rest.startswith("/"):
            rest = "/" + rest
        url = base + rest
        u = urllib.parse.urlparse(url)
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=150,
                                              context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=150)
        try:
            conn.putrequest(method, (u.path or "/") + (("?" + u.query) if u.query else ""))
            conn.putheader("Host", u.netloc)
            for k in ("Content-Type", "Accept", "Accept-Language", "Cookie", "Authorization",
                      "X-CSRF-Token", "Origin", "Referer"):
                if h.headers.get(k):
                    conn.putheader(k, h.headers[k])
            if method in ("POST", "PUT", "PATCH"):
                conn.putheader("Content-Length", str(length))
                conn.endheaders()
                left = length
                while left > 0:
                    chunk = h.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    conn.send(chunk)
                    left -= len(chunk)
            else:
                conn.endheaders()
            r = conn.getresponse()
            h.send_response(r.status)
            for k in PROXY_HEADERS:
                v = r.getheader(k)
                if v:
                    h.send_header(k, v)
            h.end_headers()
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                h.wfile.write(chunk)
            return ("handled",)
        except Exception as e:
            return ("error", 502, f"the service is not responding ({type(e).__name__})")
        finally:
            conn.close()

    # ---------- blobs ----------
    def blob_upload(self, h, length, sha=None, signed=False):
        if signed:
            who = self.mesh.verify_hub(h, required=True)
            if who is False:
                return ("error", 403, "invalid hub signature")
        name = urllib.parse.unquote(h.headers.get("X-Hub-Blob-Name") or "")
        full = disk_room(self.ctx["data"], length)
        if full:
            h.close_connection = True
            return ("error", 507, full)
        tmp = self.ctx["data"] / f"upload-{secrets.token_hex(6)}.part"
        hcalc = hashlib.sha256()
        left = length
        try:
            with tmp.open("wb") as fh:
                while left > 0:
                    chunk = h.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    hcalc.update(chunk)
                    fh.write(chunk)
                    left -= len(chunk)
            digest = hcalc.hexdigest()
            if sha and digest != sha:
                return ("error", 400, "the content does not match the sha")
            meta = self.blobs.put(tmp, name=name, by=h.headers.get("X-Hub-From") or "web")
            meta["sha"] = digest
            self.mesh.replicate_blob(digest, skip=(h.headers.get("X-Hub-From") or "",))
            return {"ok": True, **meta, "hubs": self.mesh.blob_holders(digest)}
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def blob_send(self, h, sha):
        try:
            path = self.mesh.ensure_blob(sha)
        except FileNotFoundError:
            return ("error", 404, "that file is not on the mesh")
        meta = self.blobs.meta(sha) or {}
        h.send_response(200)
        h.send_header("Content-Type", "application/octet-stream")
        h.send_header("Content-Length", str(path.stat().st_size))
        h.send_header("Content-Disposition",
                      f"attachment; filename*=UTF-8''{urllib.parse.quote(meta.get('name') or sha)}")
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        with path.open("rb") as fh:
            shutil.copyfileobj(fh, h.wfile, 1 << 20)
        return ("handled",)

    # ---------- contract ----------
    def get(self, path, query):
        h = self.ctx["req"].handler
        if path == "/api/mesh":
            return self.mesh.payload(public=bool(getattr(h, "_hub_verified", None)))
        if path == "/api/mesh/view":
            return self.mesh.view()
        if path == "/api/mesh/replica":
            ip = h._client_ip()
            if not self.mesh.capable():
                return ("error", 404, "this hub does not keep a replica")
            local = ip in ("127.0.0.1", "::1") and not h.headers.get("X-Forwarded-For")
            who = self.mesh.verify_hub(h, required=not local)
            if who is False:
                return ("error", 403, "invalid hub signature")
            if not local and not any(e.get("ip") == ip and e.get("capable") for e in self.mesh.entries()):
                return ("error", 403, "only for mesh hubs that can coordinate")
            return self.mesh.replica_payload()
        if path == "/api/catalog":
            return {"services": self.mesh.catalog(), "generated": time.time()}
        if path == "/api/labels":
            return {"labels": self.mesh.labels_view(), "generated": time.time()}
        if path == "/api/monitor/federation":
            return self.mesh.monitor_view()
        if path == "/api/blobs":
            holders = {}
            for n, r in self.mesh.remote.items():
                if self.mesh.is_alive(n):
                    for sha in r.get("blobs") or []:
                        holders.setdefault(sha, set()).add(n)
            out = []
            for b in self.blobs.list():
                out.append({**b, "hubs": sorted(set(holders.get(b["sha"], set())) | {self.mesh.me})})
            return {"blobs": out, "rf": BLOB_RF}
        if path.startswith("/api/blob/"):
            return self.blob_send(h, path.rsplit("/", 1)[-1])
        if path == "/api/mesh/approvals":
            return {"view": self.mesh.approvals.view(), "labels": ACTIONS}
        if path == "/api/mesh/invite":
            try:
                return {"invite": self.mesh.invite()}
            except ValueError as e:
                return ("error", 409, str(e))
        if path.startswith("/api/goto/"):
            return self.proxy_anycast(h, "GET")
        if path.startswith("/api/svc/"):
            return self.proxy_local(h, "GET")
        if path in ("/api/fleet", "/fleet"):
            return self.fleet()
        if path == "/api/nodes":
            return {"nodes": self.node_list()}
        if path.startswith("/api/nodes/") and path.count("/") >= 4:
            return self.proxy(h, "GET")

    def _peer_domain(self, handler, who):
        """The tailnet of a hub that came in as a paired peer (server._peer_ok), else None."""
        if not getattr(handler, "_hub_verified", None):
            return None
        rec = self.mesh.peering.by_name(who) or {}
        return rec.get("domain") or "?"

    def post(self, path, body, query, handler):
        if path == "/api/mesh":  # gossip in both directions: merges what it brings and replies with its own
            who = self.mesh.verify_hub(handler)
            if who is False:
                return ("error", 403, "invalid hub signature")
            peer = self._peer_domain(handler, who)
            if peer is not None:
                self.mesh.receive(body, peer_domain=peer, peer_name=who)
                return self.mesh.payload(public=True)
            self.mesh.receive(body)
            return self.mesh.payload(public=self.mesh.is_foreign(who))
        if path == "/api/mesh/relay":
            who = self.mesh.verify_hub(handler, required=True)
            if who is False:
                return ("error", 403, "invalid hub signature")
            return self.mesh.relay(body, peer_domain=self._peer_domain(handler, who))
        if path == "/api/nodes/enroll":
            try:
                return self.mesh.enroll(handler._client_ip(), body)
            except ValueError as e:
                return ("error", 400, str(e))
        if path == "/api/labels":
            if not isinstance(body, dict):
                return ("error", 400, "invalid body")
            rid = str(body.get("id") or body.get("key") or "").strip()
            if not rid:
                return ("error", 400, "missing service id")
            if body.get("name") is None and body.get("about") is None and body.get("hidden") is None:
                return ("error", 400, "nothing to save")
            if rid not in self.mesh.known_service_keys(fresh=True):
                return ("error", 404, "that service was not discovered")
            try:
                lab = self.mesh.set_label(
                    rid, name=body.get("name"), about=body.get("about"),
                    hidden=None if body.get("hidden") is None else bool(body.get("hidden")),
                    by=handler.headers.get("Tailscale-User-Login")
                    or handler.headers.get("X-Hub-User") or "owner")
            except ValueError as e:
                return ("error", 400, str(e))
            entry = next((s for s in self.mesh.catalog() if s["key"] == rid), None)
            return {"ok": True, "id": rid, "label": lab, "service": entry}
        if path == "/api/mesh/hub":
            try:
                by = handler.headers.get("Tailscale-User-Login") or "owner"
                if body.get("remove"):  # always through the approval quorum
                    return self.mesh.remove_hub(body["name"], by=by)
                return self.mesh.set_hub(body["name"], body, by=by)
            except KeyError:
                return ("error", 404, "that hub is not in the directory")
        if path.startswith("/api/mesh/approvals/"):
            segs = path.split("/")
            if len(segs) >= 6 and segs[5] in ("approve", "deny"):
                pid, action = urllib.parse.unquote(segs[4]), segs[5]
                if action == "approve":
                    return self.mesh.approvals.approve(pid, self.mesh.me)
                return self.mesh.approvals.deny(pid, self.mesh.me)
        if path == "/api/mesh/settings":
            vals = {}
            for k, v in (body.get("quorum") or {}).items():
                if k in ACTIONS:
                    try:
                        vals[k] = max(1, min(3, int(v)))
                    except (TypeError, ValueError):
                        pass
            clock = self.mesh.hlc.tick()
            self.mesh.approvals.set_quorum(vals, clock)
            for n in self.mesh._own_hubs():
                self.mesh.send_message(n, "settings", {"values": vals, "clock": clock})
            return {"ok": True, "quorum": self.mesh.approvals.quorum, "clock": clock}
        if path == "/api/mesh/peer":
            try:
                rec = self.mesh.peer_add(body.get("invite") or body)
            except ValueError as e:
                return ("error", 400, str(e))
            return {"ok": True, "peer": rec, "peers": self.mesh.peering.list()}
        if path == "/api/mesh/peer/remove":
            return {"ok": self.mesh.peer_remove(body.get("domain") or ""), "peers": self.mesh.peering.list()}
        if path == "/api/blobs/delete":
            sha = body.get("sha") or ""
            ok = self.blobs.delete(sha)
            return {"ok": ok, "sha": sha}
