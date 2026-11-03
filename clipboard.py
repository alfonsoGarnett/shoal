"""Shared clipboard across the tailnet.

History lives in state/clipboard.json (text; 60 entries by default, with a mesh-wide limit on item
count and age: `policy`). When "sync" is enabled, the hub copies items arriving from other devices
to the Windows clipboard and publishes what you copy on this PC. It uses the Win32 API directly
(the hub runs in the user session). On other operating systems the history works the same, without
syncing with the system clipboard (os_sync = False).

Every entry and every deletion carries a logical clock (`hlc`) and deletions are kept as tombstones
(`tomb`): two hubs that partition and later reconnect merge their history without resurrecting
deleted items or losing what was copied during the partition (`core/crdt.py`).
"""
from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path

from core import crdt

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
MAX_ITEMS, MAX_LEN = 60, 100_000
WINDOWS = sys.platform == "win32"

if not WINDOWS:
    class _NoWin:
        @staticmethod
        def GetClipboardSequenceNumber():
            return 0
    u32 = _NoWin()

    def win_get():
        return None

    def win_set(text):
        return False

else:
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.GetClipboardData.restype = wintypes.HANDLE
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE
    u32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]


    def _open():
        for _ in range(10):
            if u32.OpenClipboard(None):
                return True
            time.sleep(0.05)
        return False


    def win_get() -> str | None:
        if not _open():
            return None
        try:
            h = u32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return None
            p = k32.GlobalLock(h)
            try:
                return ctypes.wstring_at(p)
            finally:
                k32.GlobalUnlock(h)
        finally:
            u32.CloseClipboard()


    def win_set(text: str) -> bool:
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        if not _open():
            return False
        try:
            u32.EmptyClipboard()
            h = k32.GlobalAlloc(GMEM_MOVEABLE, size)
            p = k32.GlobalLock(h)
            ctypes.memmove(p, data, size)
            k32.GlobalUnlock(h)
            return bool(u32.SetClipboardData(CF_UNICODETEXT, h))
        finally:
            u32.CloseClipboard()


class Clipboard:
    def __init__(self, state_dir: Path, device_name: str = "this PC"):
        self.path = state_dir / "clipboard.json"
        self.device = device_name
        self.lock = threading.Lock()
        self.hlc = crdt.HLC(device_name)
        d = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.items, self.sync = d.get("items", []), d.get("sync", True)
        self.tomb = d.get("tomb") or []
        self.policy = d.get("policy")  # retention (crdt.clip_policy); None = default policy
        for it in self.items:  # data from older hubs: synthetic clock from its timestamp
            if isinstance(it, dict) and not it.get("hlc"):
                it["hlc"] = crdt.synthetic(it.get("ts"), it.get("from", ""))
        for it in [*self.items, *self.tomb, *([self.policy] if self.policy else [])]:
            if isinstance(it, dict) and it.get("hlc"):
                self.hlc.observe(it["hlc"])
        self.os_sync = WINDOWS  # False while this hub does not hold the mesh history (not coordinator)
        self._last_seq, self._last_text = u32.GetClipboardSequenceNumber(), None

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        d = {"items": self.items, "sync": self.sync, "tomb": self.tomb}
        if self.policy:
            d["policy"] = self.policy
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def view(self):
        with self.lock:
            items = crdt.clip_retain(self.items, self.policy)
            p = crdt.clip_policy(self.policy)
            return {"items": items, "sync": self.sync, "tomb": len(self.tomb),
                    "policy": {"max_items": p["max_items"], "max_age": p["max_age"]},
                    "rev": (items[0].get("hlc") if items else "") + f"|{len(items)}|{p['hlc']}"}

    def set_policy(self, max_items, max_age):
        with self.lock:
            p = crdt.clip_policy({"max_items": max_items, "max_age": max_age})
            p["hlc"] = self.hlc.tick()
            self.policy = p
            self.items = crdt.clip_retain(self.items, p)
            self._save()

    def prune(self):
        """Drop expired items from the history (the view already hides them; this removes them from disk)."""
        with self.lock:
            kept = crdt.clip_retain(self.items, self.policy)
            if len(kept) != len(self.items):
                self.items = kept
                self._save()

    def add(self, text: str, source: str, push_to_windows=True) -> dict:
        text = text[:MAX_LEN]
        if not text.strip():
            raise ValueError("empty")
        with self.lock:
            if self.items and self.items[0]["text"] == text:
                return self.items[0]
            item = {"id": uuid.uuid4().hex[:12], "text": text, "ts": time.time(), "from": source,
                    "hlc": self.hlc.tick()}
            self.items = crdt.clip_retain([item] + [i for i in self.items if i["text"] != text], self.policy)
            self._save()
        if push_to_windows and self.sync and self.os_sync:
            self._last_text = text
            win_set(text)
            self._last_seq = u32.GetClipboardSequenceNumber()
        return item

    def delete(self, item_id: str):
        with self.lock:
            st = crdt.clip_delete({"items": self.items, "tomb": self.tomb, "sync": self.sync},
                                  item_id, self.device, self.hlc, self.device)
            self.items, self.tomb = st["items"], st["tomb"]
            self._save()

    def clear(self):
        with self.lock:
            st = {"items": self.items, "tomb": self.tomb, "sync": self.sync}
            for it in list(self.items):
                st = crdt.clip_delete(st, it["id"], self.device, self.hlc, self.device)
            self.items, self.tomb = st["items"], st["tomb"]
            self._save()

    def merge(self, remote: dict) -> dict:
        """Merge another hub's state (replica or relay) and return the result."""
        with self.lock:
            cur = {"items": self.items, "tomb": self.tomb, "sync": self.sync}
            if self.policy:
                cur["policy"] = self.policy
            merged = crdt.clip_merge(cur, remote or {})
            changed = merged != cur
            self.items, self.tomb = merged["items"], merged.get("tomb") or []
            self.policy = merged.get("policy") or self.policy
            if changed:
                self._save()
            return merged

    def set_sync(self, on: bool):
        with self.lock:
            self.sync = bool(on)
            self._save()
        self._last_seq = u32.GetClipboardSequenceNumber()

    def reload(self):
        """After receiving the coordinator's replica."""
        d = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        with self.lock:
            self.items = d.get("items", [])
            self.tomb = d.get("tomb") or []
            self.sync = d.get("sync", self.sync)
            self.policy = d.get("policy") or self.policy
            for it in [*self.items, *self.tomb, *([self.policy] if self.policy else [])]:
                if isinstance(it, dict) and it.get("hlc"):
                    self.hlc.observe(it["hlc"])

    def run(self):
        """Publish to the history whatever is copied in Windows (only when sync is enabled)."""
        tick = 0
        while True:
            try:
                tick += 1
                if tick % 60 == 0:
                    self.prune()
                seq = u32.GetClipboardSequenceNumber()
                if self.sync and self.os_sync and seq != self._last_seq:
                    self._last_seq = seq
                    text = win_get()
                    if text and text != self._last_text:
                        self._last_text = text
                        self.add(text, self.device, push_to_windows=False)
            except Exception:
                pass
            time.sleep(1.0)
