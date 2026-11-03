"""Content-addressed store, shared between hubs (the tailnet's "cloud").

Each file is stored under the name of its sha256. Any hub can upload it and anyone
who has it serves it: if a hub does not have it, it looks for it in the hubs that announce it. The
replication (2 copies by default) is done by the mesh in the background.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

LIMIT = 512  # files in the index
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class Blobs:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "blobs"
        self.path = data_dir / "blobs.json"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        try:
            self.index = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.index = {}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.index, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def file(self, sha: str) -> Path:
        sha = str(sha or "")
        if not SHA_RE.fullmatch(sha):
            raise ValueError("invalid blob id")
        return self.dir / sha[:2] / sha

    def has(self, sha: str) -> bool:
        try:
            return bool(sha) and self.file(sha).exists()
        except ValueError:
            return False

    def put(self, src, name: str = "", by: str = "") -> dict:
        """Stores a file (path or bytes) and returns its index entry."""
        h = hashlib.sha256()
        dest_dir = None
        tmp = None
        try:
            if isinstance(src, (bytes, bytearray)):
                data = bytes(src)
                h.update(data)
                sha = h.hexdigest()
                dest_dir = self.dir / sha[:2]
                dest_dir.mkdir(parents=True, exist_ok=True)
                tmp = dest_dir / (sha + ".tmp")
                tmp.write_bytes(data)
                size = len(data)
            else:
                src = Path(src)
                tmp = self.dir / (hashlib.sha256(str(src).encode()).hexdigest()[:16] + ".part")
                size = 0
                with src.open("rb") as f, tmp.open("wb") as o:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        h.update(chunk)
                        o.write(chunk)
                        size += len(chunk)
                sha = h.hexdigest()
                dest_dir = self.dir / sha[:2]
                dest_dir.mkdir(parents=True, exist_ok=True)
            dest = self.file(sha)
            tmp.replace(dest)
            with self.lock:
                old = self.index.get(sha) or {}
                self.index[sha] = {"sha": sha, "name": (name or old.get("name") or sha[:12])[:120],
                                   "size": size, "ts": old.get("ts") or time.time(), "by": by or old.get("by", "")}
                self.index = dict(sorted(self.index.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:LIMIT])
                # delete files that dropped out of the index
                keep = set(self.index)
                for sha_old in list(self._all_files()):
                    if sha_old not in keep:
                        try:
                            self.file(sha_old).unlink()
                        except OSError:
                            pass
                self._save()
            return dict(self.index[sha])
        except Exception:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise

    def _all_files(self):
        for d in self.dir.iterdir() if self.dir.exists() else []:
            if d.is_dir():
                for f in d.iterdir():
                    if f.is_file() and len(f.name) == 64:
                        yield f.name

    def meta(self, sha: str):
        with self.lock:
            return dict(self.index.get(sha) or {}) or None

    def list(self):
        with self.lock:
            return [dict(v) for v in self.index.values()]

    def delete(self, sha: str) -> bool:
        try:
            path = self.file(sha)
        except ValueError:
            return False
        with self.lock:
            self.index.pop(sha, None)
            self._save()
        try:
            path.unlink()
            return True
        except OSError:
            return False
