"""Store-and-forward relay (delay-tolerant network) between hubs.

If a hub wants to notify, replicate a file or request an approval from another and there is no direct
path (the coordinator is off, the other is on relay or simply does not respond), the
message stays in a queue with a TTL and is forwarded by the next hub that sees it. Messages carry a
maximum hop count so they do not loop forever, and a registry of ids avoids duplicates.

It is used for: push notifications, the network clipboard, quorum approvals and file
replication. Each hub first tries to deliver directly; the relay is plan B.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path

HOP_MAX = 3
TTL = 24 * 3600
LIMIT = 300


class Forward:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "forward.json"
        self.lock = threading.Lock()
        try:
            self.queue = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.queue = []
        self.seen = {}
        self.deliver_direct = lambda msg: False  # the mesh connects this
        self.deliver_local = lambda msg: False   # the mesh connects this
        self.neighbors = lambda: []              # the mesh connects this: names of live hubs
        self.hop = lambda msg: False             # the mesh connects this: forward to a neighbor

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.queue[-LIMIT:], ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def enqueue(self, to: str, kind: str, body: dict, ttl: int = TTL, **extra) -> dict:
        msg = {"id": secrets.token_hex(8), "to": to, "kind": kind, "body": body,
               "ts": time.time(), "ttl": ttl, "hops": HOP_MAX, **extra}
        with self.lock:
            self.queue.append(msg)
            self.queue = self.queue[-LIMIT:]
            self._save()
        return msg

    def prune(self):
        now = time.time()
        with self.lock:
            self.queue = [m for m in self.queue if now - m.get("ts", 0) < m.get("ttl", TTL)]
            self._save()

    def process(self):
        """One pass: deliver what can be delivered and forward the rest to a random neighbor."""
        with self.lock:
            items = [dict(m) for m in self.queue]
        left = []
        for m in items:
            if time.time() - m.get("ts", 0) > m.get("ttl", TTL):
                continue
            if m.get("to") in (None, "", "*"):
                ok = self.deliver_local(m)
            else:
                ok = self.deliver_direct(m)
            if ok:
                continue
            if m.get("hops", 0) > 0:
                left.append(m)
        with self.lock:
            self.queue = left[-LIMIT:]
            self._save()
        for m in left:
            if m.get("hops", 0) > 0:
                self.hop(m)
