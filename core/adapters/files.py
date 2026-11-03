"""Files: browse, download and upload inside the roots this hub exposes.

The home root (shared storage on Android, or "files_root" in config.json) is always open.
The other roots (the system root, every disk and mounted volume) are listed but locked until
the remote terminal of this hub is enabled: a browser over every disk is as powerful as a
shell, so it shares the terminal's switch and its quorum. EFI system partitions are never
exposed.

  GET  /files/roots                    -> {roots: [{id, name, path, kind, locked}], terminal}
  GET  /files?root=<id>&path=&hidden=1 -> {root, path, items}   (root defaults to home)
  GET  /file?root=<id>&path=           -> download
  POST /file?root=<id>&path=           -> upload (body: bytes)
"""
from __future__ import annotations

import os
import shutil
import string
import sys
from pathlib import Path

from . import Adapter

WIN = sys.platform == "win32"
MOUNT_PARENTS = ("/media", "/mnt", "/run/media", "/Volumes", "/storage")
SKIP_FS = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "pstore", "securityfs",
           "debugfs", "tracefs", "configfs", "fusectl", "mqueue", "hugetlbfs", "bpf", "autofs",
           "binfmt_misc", "efivarfs", "squashfs", "overlay", "nsfs", "ramfs", "rpc_pipefs"}


def _is_efi(path: Path) -> bool:
    """A small FAT volume with an EFI folder at its root is the boot partition: never expose it."""
    try:
        return (path / "EFI").is_dir() and shutil.disk_usage(path).total < 2 * 1024 ** 3
    except OSError:
        return False


class Impl(Adapter):
    name = "files"
    caps = ["files"]

    def probe(self):
        p, cfg = self.ctx["platform"], self.ctx["config"]
        cands = [cfg.get("files_root")] if cfg.get("files_root") else []
        if p["termux"]:
            cands += [str(Path.home() / "storage" / "shared"), "/storage/emulated/0"]
        cands.append(str(Path.home()))
        for c in cands:
            if c and Path(c).is_dir():
                self.root = Path(c).resolve()
                return True
        return False

    def info(self):
        st = shutil.disk_usage(self.root)
        return {"shared": True, "files_root": str(self.root), "storage": {"free": st.free, "total": st.total}}

    # ---------- roots ----------
    def unlocked(self) -> bool:
        term = self.ctx.get("adapters_by_name", {}).get("terminal")
        try:
            return bool(term and term.state()["enabled"])
        except Exception:
            return False

    def _volumes(self):
        """(name, path, kind) for the system root and every mounted disk, without duplicates."""
        out = []
        if WIN:
            for letter in string.ascii_uppercase:
                path = Path(f"{letter}:\\")
                try:
                    if path.exists():
                        out.append((f"{letter}:", path, "disk"))
                except OSError:
                    continue
            return out
        out.append(("/", Path("/"), "system"))
        try:
            with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
                for line in f:
                    dev, mnt, fs = (line.split() + ["", "", ""])[:3]
                    mnt = mnt.replace("\\040", " ")
                    if fs in SKIP_FS or not mnt.startswith(MOUNT_PARENTS):
                        continue
                    out.append((Path(mnt).name or mnt, Path(mnt), "disk"))
        except OSError:  # macOS or Android without /proc/mounts: list the usual mount folders
            for parent in MOUNT_PARENTS:
                try:
                    for child in Path(parent).iterdir():
                        if child.is_dir() and not child.is_symlink():
                            out.append((child.name, child, "disk"))
                except OSError:
                    continue
        return out

    def roots(self):
        locked = not self.unlocked()
        out = [{"id": "home", "name": self.root.name or str(self.root), "path": str(self.root), "kind": "home", "locked": False}]
        seen = {self.root}
        for name, path, kind in self._volumes():
            try:
                path = path.resolve()
            except OSError:
                continue
            if path in seen or _is_efi(path) or not os.access(path, os.R_OK):
                continue
            seen.add(path)
            rid = path.as_posix().rstrip("/").replace(":", "") or "root"
            out.append({"id": rid, "name": name, "path": str(path), "kind": kind, "locked": locked})
        return out

    def _root(self, rid):
        rid = rid or "home"
        root = next((r for r in self.roots() if r["id"] == rid), None)
        if not root:
            raise FileNotFoundError(rid)
        if root["locked"]:
            raise PermissionError("enable this hub's remote terminal to browse outside the home folder")
        return root, Path(root["path"])

    def _own_folder(self, p: Path, base: Path) -> bool:
        """The hub's data (keys, identity, the terminal switch) and code folders. They usually live in
        the home folder, but writing there is as powerful as the terminal, so they share its switch.
        A root that itself lies inside one of them (a demo, a custom files_root) is left alone: what
        is outside the root was never reachable."""
        from ..server import APP
        for d in (self.ctx.get("data"), APP):
            if d:
                d = Path(d).resolve()
                if (p == d or d in p.parents) and d not in base.parents:
                    return True
        return False

    def safe(self, rel, rid=None):
        root, base = self._root(rid)
        p = (base / (rel or "").lstrip("/\\")).resolve()
        if p != base and base not in p.parents:
            raise PermissionError("outside the shared folder")
        if not self.unlocked() and self._own_folder(p, base):
            raise PermissionError("enable this hub's remote terminal to open the hub's own folders")
        return root, base, p

    # ---------- contract ----------
    def get(self, path, query):
        if path == "/files/roots":
            return {"roots": self.roots(), "terminal": self.unlocked()}
        if path == "/files":
            root, base, d = self.safe(query.get("path", ""), query.get("root"))
            hidden = query.get("hidden") == "1"
            items = []
            for e in sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if e.name.startswith(".") and not hidden:
                    continue
                try:
                    s = e.stat()
                    items.append({"name": e.name, "dir": e.is_dir(), "size": s.st_size, "mtime": s.st_mtime})
                except OSError:
                    pass
            rel = d.relative_to(base).as_posix()
            return {"root": root["id"], "path": "" if rel == "." else rel, "items": items}
        if path == "/file":
            return ("file", self.safe(query.get("path", ""), query.get("root"))[2])

    def post(self, path, body, query, handler):
        if path == "/file":  # the body (bytes) is read by the server; here only the destination is validated
            f = self.safe(query.get("path", ""), query.get("root"))[2]
            f.parent.mkdir(parents=True, exist_ok=True)
            return ("upload", f)
