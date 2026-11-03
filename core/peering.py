"""Federation between different tailnets (peering) without depending on a central cloud.

Two owners of different tailnets can link their meshes with a signed invitation:

1. On a hub of tailnet A the invitation is requested (Control › Network › "Pair another tailnet"
   or `python -m core --peer-invite`). It is a JSON signed with the hub's identity: domain,
   name, URL and public key. It carries no secrets.
2. The owner of tailnet B pastes it into their hub (`--peer-add` or the same window). B verifies the
   signature with the key carried by the invitation itself and saves domain A as a peer.
3. From that moment on B accepts from domain A only what is allowed (by default: directory, mesh
   and catalog). A must do the same with an invitation from B: the link is symmetric and
   explicit, never automatic.

Hubs from another tailnet do not coordinate, do not receive code or replicas, and their entries cannot
overwrite the home ones. It is the basis for connecting the home network with the work one (or a
friend's) without opening ports or setting up a central server.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import crypto
from .identity import b64, ub64

ALLOW_DEFAULT = ["mesh", "catalog", "notify", "blobs"]


def invite_message(inv: dict) -> bytes:
    fields = {k: inv.get(k) for k in ("v", "domain", "name", "url", "pub", "ts")}
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


def make_invite(domain: str, name: str, url: str, pub_b64: str, sign) -> dict:
    inv = {"v": 1, "domain": domain, "name": name, "url": url, "pub": pub_b64, "ts": int(time.time())}
    inv["sig"] = sign(invite_message(inv))
    return inv


def verify_invite(inv: dict) -> tuple[bool, str]:
    try:
        if not inv.get("domain") or not inv.get("pub") or not inv.get("url"):
            return False, "incomplete invitation"
        if not crypto.verify(ub64(inv["sig"]), invite_message(inv), ub64(inv["pub"])):
            return False, "invalid signature"
        if abs(time.time() - int(inv.get("ts") or 0)) > 90 * 24 * 3600:
            return False, "expired invitation"
        return True, ""
    except Exception as e:
        return False, f"unreadable invitation ({type(e).__name__})"


class Peering:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "peering.json"
        self.lock = threading.Lock()
        try:
            self.peers = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.peers = {}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.peers, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, invite: dict, domain_own: str) -> dict:
        ok, why = verify_invite(invite)
        if not ok:
            raise ValueError(why)
        dom = invite["domain"]
        if dom == domain_own:
            raise ValueError("that invitation belongs to this same tailnet")
        from .tailnet import hub_url_ok
        if not hub_url_ok(invite["url"], dom):
            raise ValueError("the invitation's hub URL is not an address of that tailnet")
        # What the paired tailnet may do is decided here, never by the invitation it sent.
        rec = {"domain": dom, "name": invite.get("name", ""), "pub": invite["pub"],
               "url": invite["url"].rstrip("/") + "/", "allow": list(ALLOW_DEFAULT), "added": time.time()}
        with self.lock:
            self.peers[dom] = rec
            self._save()
        return rec

    def remove(self, domain: str) -> bool:
        with self.lock:
            if domain not in self.peers:
                return False
            del self.peers[domain]
            self._save()
            return True

    def get(self, domain: str):
        return self.peers.get(domain)

    def by_name(self, name: str):
        for p in self.peers.values():
            if p.get("name") == name:
                return p
        return None

    def allowed(self, domain: str, what: str = "mesh") -> bool:
        p = self.peers.get(domain)
        return bool(p and what in (p.get("allow") or []))

    def list(self):
        return [{"domain": d, "name": p.get("name"), "url": p.get("url"), "allow": p.get("allow"),
                 "added": p.get("added")} for d, p in sorted(self.peers.items())]
