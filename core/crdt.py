"""Logical clocks and merging of shared data (minimal CRDTs, no dependencies).

The mesh does not depend on the machines' clocks to decide which datum wins: every change carries a
hybrid logical clock (`hlc`) = "<ms>:<counter>:<node>". With that:
- the directory is merged field by field (per-field LWW);
- the clipboard is a set with deletion markers (the newest change by `hlc` wins);
- push subscriptions are one record per device (LWW).
Time is only used to bring the clock up to date (with tolerance), never to compare machines.
"""
from __future__ import annotations

import threading
import time

SKEW_MS = 120_000  # an hlc from the future beyond this is considered suspicious and is trimmed


class HLC:
    """Hybrid logical clock: monotonic, orders changes even when clocks do not agree."""

    def __init__(self, node: str):
        self.node = node
        self.lock = threading.Lock()
        self.ms, self.ctr = 0, 0

    def tick(self) -> str:
        with self.lock:
            now = max(self.ms, int(time.time() * 1000))
            self.ctr = self.ctr + 1 if now == self.ms else 0
            self.ms = now
            return self.stamp()

    def observe(self, ts: str) -> None:
        """Learns from someone else's hlc (so it does not fall behind after receiving data)."""
        ms, ctr, _ = parse(ts)
        if ms < 0:
            return
        with self.lock:
            if ms > self.ms:
                self.ms, self.ctr = ms, ctr
            elif ms == self.ms:
                self.ctr = max(self.ctr, ctr)

    def stamp(self) -> str:
        return f"{self.ms:013d}:{self.ctr:04d}:{self.node}"


def parse(ts: str):
    try:
        ms_s, ctr_s, node = str(ts).split(":", 2)
        ms, ctr = int(ms_s), int(ctr_s)
        if ms > time.time() * 1000 + SKEW_MS:
            ms = int(time.time() * 1000)
        return ms, ctr, node
    except Exception:
        return -1, -1, ""


def key(ts: str):
    ms, ctr, node = parse(ts)
    return (ms, ctr, node)


def newer(a: str, b: str) -> bool:
    return key(a) > key(b)


def synthetic(updated: float | None, node: str = "") -> str:
    """Equivalent hlc for old data that only had a date."""
    return f"{int((updated or 0) * 1000):013d}:0000:{node}"


# ---------- clipboard: set with deletion markers ----------

CLIP_ITEMS = 60      # default number of entries kept
CLIP_ITEMS_MAX = 500


def clip_policy(p) -> dict:
    """Retention policy of the network clipboard (an LWW register): at most `max_items`
    entries and none older than `max_age` seconds (0 = no age limit)."""
    p = p if isinstance(p, dict) else {}
    try:
        n = int(p.get("max_items") or CLIP_ITEMS)
    except (TypeError, ValueError):
        n = CLIP_ITEMS
    try:
        age = max(0, int(p.get("max_age") or 0))
    except (TypeError, ValueError):
        age = 0
    return {"max_items": min(max(n, 1), CLIP_ITEMS_MAX), "max_age": age, "hlc": str(p.get("hlc") or "")}


def clip_retain(items: list, policy: dict, now: float | None = None) -> list:
    """Applies the retention policy to a list already sorted from newest to oldest."""
    p = clip_policy(policy)
    if p["max_age"]:
        floor = (now or time.time()) - p["max_age"]
        items = [i for i in items if not i.get("ts") or i["ts"] >= floor]
    return items[:p["max_items"]]


def clip_merge(local: dict, remote: dict, limit: int | None = None) -> dict:
    """Merges two histories {items:[…], tomb:[…], policy:{…}} without losing changes from either.
    An item wins if its hlc is newer than that of the deletion of the same id; the newest policy
    wins and is applied to the result. Expired entries simply drop out on every hub alike."""
    items: dict = {}
    tomb: dict = {}
    sync = local.get("sync", True)
    lp, rp = local.get("policy"), (remote or {}).get("policy")
    policy = rp if rp and (not lp or newer(rp.get("hlc") or "", lp.get("hlc") or "")) else lp

    def add(state):
        for it in (state or {}).get("items") or []:
            if not isinstance(it, dict) or not it.get("id"):
                continue
            it = dict(it)
            it.setdefault("hlc", synthetic(it.get("ts"), it.get("from", "")))
            if it["id"] not in items or newer(it["hlc"], items[it["id"]]["hlc"]):
                items[it["id"]] = it
        for t in (state or {}).get("tomb") or []:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            t = dict(t)
            t.setdefault("hlc", synthetic(t.get("ts"), t.get("from", "")))
            if t["id"] not in tomb or newer(t["hlc"], tomb[t["id"]]["hlc"]):
                tomb[t["id"]] = t

    add(local)
    add(remote)
    live = [i for i in items.values() if i["id"] not in tomb or newer(i["hlc"], tomb[i["id"]]["hlc"])]
    live.sort(key=lambda i: key(i["hlc"]), reverse=True)
    dead = sorted(tomb.values(), key=lambda t: key(t["hlc"]), reverse=True)[:500]
    out = {"items": clip_retain(live, policy)[:limit or CLIP_ITEMS_MAX], "tomb": dead, "sync": sync}
    if policy:
        out["policy"] = clip_policy(policy)
    return out


def clip_delete(local: dict, item_id: str, node: str, clock: HLC, from_: str = "") -> dict:
    state = {"items": local.get("items") or [], "tomb": local.get("tomb") or [], "sync": local.get("sync", True)}
    h = clock.tick()
    state["tomb"] = ([{"id": item_id, "hlc": h, "ts": time.time(), "from": from_}] +
                     [t for t in state["tomb"] if t.get("id") != item_id])[:500]
    state["items"] = [i for i in state["items"] if i.get("id") != item_id]
    return state


# ---------- push subscriptions: one record per device ----------

def reg_merge(local: dict, remote: dict) -> dict:
    """Merges {sid: {…, hlc, del?}} keeping the newest record for each device."""
    out = dict(local or {})
    for sid, r in (remote or {}).items():
        if not isinstance(r, dict):
            continue
        r = dict(r)
        r.setdefault("hlc", synthetic(r.get("created"), r.get("label", "")))
        cur = out.get(sid)
        if not cur:
            out[sid] = r
            continue
        cur = dict(cur)
        cur.setdefault("hlc", synthetic(cur.get("created"), cur.get("label", "")))
        if newer(r["hlc"], cur["hlc"]):
            out[sid] = r
    return out


def reg_visible(subs: dict) -> dict:
    return {k: v for k, v in (subs or {}).items() if not v.get("del")}


# ---------- service labels: custom title and description, LWW per field ----------

LABEL_LIMITS = {"name": 80, "about": 400}
LABEL_FIELDS = ("name", "about", "hidden")  # hidden: True hides the card; False shows it over config
LABEL_TOTAL = 500


def label_value(field: str, value):
    """Normalizes one label field. A title is single-line; a description keeps newlines."""
    if field == "hidden":
        return bool(value)
    text = str(value if value is not None else "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if field == "name":
        text = " ".join(text.split())
    return text[: LABEL_LIMITS.get(field, 200)]


def label_effective(rec: dict) -> dict:
    """Visible part of a label record. Empty name/about mean "use the automatic value"."""
    rec = rec if isinstance(rec, dict) else {}
    name_rec = rec.get("name")
    name = label_value("name", name_rec.get("v")) if isinstance(name_rec, dict) else ""
    about_rec = rec.get("about")
    about = label_value("about", about_rec.get("v")) if isinstance(about_rec, dict) else ""
    about = about or None
    hidden_rec = rec.get("hidden")
    hidden = bool(hidden_rec.get("v")) if isinstance(hidden_rec, dict) else None
    return {"name": name, "about": about, "hidden": hidden, "custom": bool(name or about),
            "by": rec.get("by") or "", "ts": rec.get("ts")}


def label_merge(local: dict, remote: dict) -> dict:
    """Merges {service_key: {name: {v, hlc}, about: {v, hlc}, by, ts}} field by field.

    Each field has its own hlc, so two hubs editing the title and the description of the same
    service do not overwrite each other. An empty value is a valid newer override: it means
    "use the automatic value" and does not resurrect older text.
    """
    out = {}
    for state in (local or {}, remote or {}):
        if not isinstance(state, dict):
            continue
        for key, rec in state.items():
            if not isinstance(key, str) or not key or len(key) > 200 or not isinstance(rec, dict):
                continue
            dst = out.setdefault(key, {})
            for field in LABEL_FIELDS:
                fv = rec.get(field)
                if not isinstance(fv, dict) or not isinstance(fv.get("hlc"), str):
                    continue
                cur = dst.get(field)
                if cur is None or newer(fv["hlc"], cur.get("hlc") or ""):
                    dst[field] = {"v": label_value(field, fv.get("v")), "hlc": fv["hlc"]}
            ts = rec.get("ts")
            if isinstance(ts, (int, float)) and ts > (dst.get("ts") or 0):
                dst["ts"] = float(ts)
                if isinstance(rec.get("by"), str):
                    dst["by"] = rec["by"][:120]
    out = {k: v for k, v in out.items() if any(v.get(f) for f in LABEL_FIELDS)}
    if len(out) > LABEL_TOTAL:
        newest = sorted(out.items(), key=lambda kv: kv[1].get("ts") or 0, reverse=True)[:LABEL_TOTAL]
        out = dict(newest)
    return out


ORDER_LIMIT = 1000


def order_merge(old, new) -> list:
    """Applies a reorder of some routes to the mesh-wide route order.

    `new` lists the routes one hub can see, in their new order. Ids that hub did not send
    (another hub's routes, hidden routes) keep their slots; the sent ids fill the slots they
    already held, in the new order, and ids never seen before go at the end.
    """
    def clean(ids):
        out, seen = [], set()
        for x in ids or []:
            if isinstance(x, (str, int)) and not isinstance(x, bool):
                s = str(x)[:200]
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
        return out

    old, new = clean(old), clean(new)
    sent = set(new)
    pending = iter(new)
    out = [next(pending) if x in sent else x for x in old]
    out.extend(pending)
    return out[:ORDER_LIMIT]
