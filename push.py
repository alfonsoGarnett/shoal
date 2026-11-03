"""Web Push notifications from the hub: VAPID keys, per-device subscriptions and sending.

Each subscription (a browser on a device) stores its own switches per event type
in state/subscriptions.json. The VAPID private key lives in state/vapid_private.pem.

Subscriptions are merged between hubs with one record per device (logical clock and tombstones
on unsubscribe), so a replica that comes back after a partition does not resurrect devices that
had been unsubscribed on another hub (`core/crdt.py`).
"""
import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01
from pywebpush import WebPushException, webpush

from core import crdt

EVENTS = {  # key: (label, description, enabled by default)
    "service_down": ("Tailnet routes", "When a served route goes down or recovers.", False),
    "new_device": ("New device on the tailnet", "When a device joins, so you can install its hub.", True),
}
MODULE_EVENTS = {}  # key: module id. Every hub ships every manifest, so any hub can deliver them.


def _module_events():
    root = Path(__file__).parent
    for f in sorted([*(root / "site" / "modules").glob("*/module.json"), *(root / "core" / "modules").glob("*/module.json")]):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key, e in (m.get("events") or {}).items():
            EVENTS.setdefault(key, (e.get("label", key), e.get("about", ""), bool(e.get("default"))))
            MODULE_EVENTS[key] = m.get("id")


_module_events()
# "sub" claim required by push services: a URL of our own, with no personal data.
CLAIM_SUB = "https://example.invalid/hub/"


class Push:
    def __init__(self, state_dir: Path, node: str = "", claim_sub: str = ""):
        self.dir = state_dir
        self.claim_sub = claim_sub or CLAIM_SUB
        self.dir.mkdir(exist_ok=True)
        self.key_path = self.dir / "vapid_private.pem"
        self.subs_path = self.dir / "subscriptions.json"
        self.log_path = self.dir / "push-log.json"
        self.lock = threading.Lock()
        self.hlc = crdt.HLC(node or "push")
        if not self.key_path.exists():
            v = Vapid01()
            v.generate_keys()
            v.save_key(str(self.key_path))
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
        v = Vapid01.from_file(str(self.key_path))
        raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        raw_subs = json.loads(self.subs_path.read_text(encoding="utf-8")) if self.subs_path.exists() else {}
        self.subs = {k: v for k, v in raw_subs.items() if isinstance(v, dict) and not v.get("del")}
        self.tomb = {k: v for k, v in raw_subs.items() if isinstance(v, dict) and v.get("del")}
        for r in self.subs.values():  # old data without a logical clock
            r.setdefault("hlc", crdt.synthetic(r.get("created"), r.get("label", "")))
        for r in [*self.subs.values(), *self.tomb.values()]:
            if isinstance(r, dict) and r.get("hlc"):
                self.hlc.observe(r["hlc"])
        self.log = json.loads(self.log_path.read_text(encoding="utf-8")) if self.log_path.exists() else []

    @staticmethod
    def sub_id(endpoint: str) -> str:
        return hashlib.sha256(endpoint.encode()).hexdigest()[:16]

    def _save(self):
        tmp = self.subs_path.with_suffix(".tmp")
        raw = {**self.subs, **self.tomb}
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.subs_path)

    def subscribe(self, subscription: dict, label: str, user: str) -> dict:
        sid = self.sub_id(subscription["endpoint"])
        with self.lock:
            cur = self.subs.get(sid, {})
            self.subs[sid] = {
                "subscription": subscription, "label": label[:60] or "device", "user": user,
                "created": cur.get("created") or time.time(),
                "events": cur.get("events") or {k: v[2] for k, v in EVENTS.items()},
                "hlc": self.hlc.tick(),
            }
            self.tomb.pop(sid, None)
            self._save()
        return self.view(sid)

    def unsubscribe(self, sid: str):
        with self.lock:
            self.subs.pop(sid, None)
            self.tomb[sid] = {"del": True, "ts": time.time(), "hlc": self.hlc.tick()}
            self.tomb = dict(sorted(self.tomb.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:200])
            self._save()

    def set_event(self, sid: str, event: str, on: bool) -> dict:
        with self.lock:
            if sid not in self.subs or event not in EVENTS:
                raise KeyError(sid if sid not in self.subs else event)
            self.subs[sid]["events"][event] = bool(on)
            self.subs[sid]["hlc"] = self.hlc.tick()
            self._save()
        return self.view(sid)

    def merge_state(self, remote: dict) -> bool:
        """Merges {sid: record} from another hub; returns whether anything changed."""
        with self.lock:
            cur = {**self.subs, **self.tomb}
            merged = crdt.reg_merge(cur, remote or {})
            subs = {k: v for k, v in merged.items() if not v.get("del")}
            tomb = {k: v for k, v in merged.items() if v.get("del")}
            changed = subs != self.subs or tomb != self.tomb
            self.subs, self.tomb = subs, tomb
            if changed:
                self._save()
            return changed

    def view(self, sid: str):
        s = self.subs.get(sid)
        if not s:
            return None
        return {"id": sid, "label": s["label"], "events": s["events"]}

    def catalog(self):
        return [{"key": k, "label": v[0], "about": v[1], "default": v[2], "module": MODULE_EVENTS.get(k)}
                for k, v in EVENTS.items()]

    def devices(self):
        return [{"id": k, "label": v["label"], "since": v["created"]} for k, v in self.subs.items()]

    def send(self, event: str, title: str, body: str, url: str = "/hub/", only: str | None = None) -> int:
        payload = json.dumps({"title": title, "body": body, "url": url, "tag": event, "ts": time.time()})
        sent, gone = 0, []
        with self.lock:
            targets = [(k, v) for k, v in self.subs.items()
                       if (only == k) or (only is None and v["events"].get(event))]
        for sid, s in targets:
            try:
                webpush(s["subscription"], data=payload, vapid_private_key=str(self.key_path),
                        vapid_claims={"sub": self.claim_sub}, ttl=3600, timeout=10)
                sent += 1
            except WebPushException as e:
                if e.response is not None and e.response.status_code in (404, 410):
                    gone.append(sid)  # subscription expired or revoked in the browser
            except Exception:
                pass
        if gone:
            with self.lock:
                for sid in gone:
                    self.subs.pop(sid, None)
                self._save()
        with self.lock:
            self.log = ([{"ts": time.time(), "event": event, "title": title, "body": body, "sent": sent}] + self.log)[:50]
            self.log_path.write_text(json.dumps(self.log, ensure_ascii=False, indent=1), encoding="utf-8")
        return sent
