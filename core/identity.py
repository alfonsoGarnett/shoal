"""Cryptographic identity of each hub.

- Each hub generates its ed25519 key pair the first time it starts (`identity.json` in its
  data folder). Nothing needs to be configured.
- Its directory entry is signed: nobody can impersonate it or change its
  address without its key. Keys are learned the first time they are seen (TOFU) and pinned;
  from then on an entry with another key or with an invalid signature is rejected.
- Requests between hubs are signed (`X-Hub-Auth`): origin, time, nonce and body hash.
  With `HUB_HUB_AUTH=strict` they are also required from hubs that already announced they are version 2.

This is defense in depth: Tailscale's identity is still the main door.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
from pathlib import Path

from . import crypto

AUTH_HEADER = "X-Hub-Auth"
AUTH_TTL = 300  # seconds of validity of a token between hubs

# Fields signed by the owner: their identity and their address. `priority`/`removed` can be
# changed by any hub of the owner (Control › Network) and `hlc`/`updated`/`hb`/`inc` are volatile.
SIGNED = ("name", "ip", "dns", "url", "kind", "platform", "os", "capable", "enrolled", "pub", "domain")


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def ub64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Identity:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "identity.json"
        self.lock = threading.Lock()
        d = {}
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        seed = ub64(d["seed"]) if d.get("seed") else os.urandom(32)
        self.seed = seed
        self.pub = crypto.publickey(seed)
        if d.get("seed") != b64(seed) or not d.get("pub"):
            data_dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"seed": b64(seed), "pub": b64(self.pub), "created": time.time()}),
                                 encoding="utf-8")
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self.name = ""

    @property
    def pub_b64(self) -> str:
        return b64(self.pub)

    def sign(self, msg: bytes) -> str:
        with self.lock:
            return b64(crypto.sign(msg, self.seed, self.pub))

    def token(self, method: str, path: str, body_sha: str) -> str:
        ts = f"{time.time():.3f}"
        nonce = secrets.token_hex(8)
        msg = f"{self.name}|{ts}|{nonce}|{method.upper()}|{path}|{body_sha}".encode()
        return b64(json.dumps({"from": self.name, "ts": ts, "nonce": nonce,
                               "v": 2, "sig": self.sign(msg)}).encode())


def entry_message(entry: dict) -> bytes:
    e = {k: entry.get(k) for k in SIGNED if entry.get(k) not in (None, "")}
    return json.dumps(e, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign_entry(entry: dict, identity: Identity) -> dict:
    e = dict(entry)
    e["pub"] = identity.pub_b64
    e["sig"] = identity.sign(entry_message(e))
    return e


class Pins:
    """Public keys pinned by name (TOFU). Stored in the data folder."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "identity_pins.json"
        self.lock = threading.Lock()
        try:
            self.pins = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.pins = {}

    def get(self, name: str):
        return self.pins.get(name)

    def set(self, name: str, pub: str):
        with self.lock:
            if self.pins.get(name) == pub:
                return
            self.pins[name] = pub
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.pins, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def check(self, entry: dict) -> tuple[bool, str]:
        """Validates another hub's entry. Returns (ok, reason)."""
        name, pub, sig = entry.get("name"), entry.get("pub"), entry.get("sig")
        if not name:
            return False, "no name"
        if not pub:
            return (True, "no key") if not self.pins.get(name) else (False, "key missing")
        pinned = self.pins.get(name)
        if pinned and pinned != pub:
            return False, "different key"
        if sig:
            try:
                if crypto.verify(ub64(sig), entry_message(entry), ub64(pub)):
                    self.set(name, pub)
                    return True, "valid signature"
            except Exception:
                pass
            return False, "invalid signature"
        return (self.pins.get(name) is None, "no signature")


class Nonces:
    def __init__(self, ttl: int = AUTH_TTL):
        self.ttl, self.seen, self.lock = ttl, {}, threading.Lock()

    def fresh(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            if now - self.seen.get(key, 0) < self.ttl:
                return False
            self.seen[key] = now
            if len(self.seen) > 4096:  # pruning
                for k, t in list(self.seen.items()):
                    if now - t > self.ttl:
                        del self.seen[k]
            return True


def verify_token(identity_name: str, pub_for, nonces: Nonces, h, method: str, path: str,
                 body_sha: str, required: bool = False):
    """Verifies a hub's signature on a request.

    Returns the name of the signing hub, or None if no signature was present (compatibility), or False if
    one was present and is not valid. With required=True, the absence of a signature is also False.
    """
    raw = h.headers.get(AUTH_HEADER)
    if not raw:
        return False if required else None
    try:
        d = json.loads(ub64(raw))
        frm, ts, nonce, sig = d["from"], d["ts"], d["nonce"], d["sig"]
        if abs(time.time() - float(ts)) > AUTH_TTL:
            return False
        pub = pub_for(frm)
        if not pub:
            return None  # no key pinned yet: compatibility; entries are validated by TOFU when merging
        msg = f"{frm}|{ts}|{nonce}|{method.upper()}|{path}|{body_sha}".encode()
        if not crypto.verify(ub64(sig), msg, ub64(pub)):
            return False
        if not nonces.fresh(frm + ":" + nonce):
            return False
        return frm
    except Exception:
        return False
