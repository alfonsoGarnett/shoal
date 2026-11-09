"""Unit tests for crypto, CRDT, identity, approvals, relay, blobs, peering, releases and modules."""
import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

tmp = Path(tempfile.mkdtemp(prefix="hub-unit-"))
os.environ["HUB_CORE_HOME"] = str(tmp)
os.environ["HUB_NO_RESTART"] = "1"
os.environ["HUB_OK_GRACE"] = "1"

from core import crdt, crypto  # noqa: E402
from core.identity import Identity, Nonces, b64, entry_message, sign_entry, ub64, verify_token  # noqa: E402
from core.approvals import Approvals  # noqa: E402
from core.blobs import Blobs  # noqa: E402
from core.forward import Forward  # noqa: E402
from core.peering import Peering, make_invite, verify_invite  # noqa: E402
from core.mesh import choose_release  # noqa: E402

ok = 0


def check(cond, label):
    global ok
    if not cond:
        raise AssertionError(label)
    ok += 1
    print(f"  ok  {label}")


# ---------- crypto: RFC 8032 vectors ----------
sk = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
pk = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
                    "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
check(crypto.publickey(sk) == pk, "RFC 8032: public key")
check(crypto.sign(b"", sk) == sig, "RFC 8032: signature")
check(crypto.verify(sig, b"", pk), "RFC 8032: verification")
check(not crypto.verify(sig, b"x", pk), "RFC 8032: tampered signature is rejected")
# Both backends must agree: hubs with and without `cryptography` share keys and signatures.
check(crypto._publickey_py(sk) == pk and crypto._sign_py(b"", sk, pk) == sig
      and crypto._verify_py(sig, b"", pk) and not crypto._verify_py(sig, b"x", pk),
      "RFC 8032: pure-Python fallback")
if crypto._PublicKey is not None:
    check(crypto._verify_native(sig, b"", pk) and not crypto._verify_native(sig, b"x", pk),
          f"RFC 8032: native backend ({crypto.BACKEND})")
_hits = len(crypto._cache)
check(crypto.verify(sig, b"", pk) and len(crypto._cache) == _hits, "crypto: repeated verification is cached")

# ---------- detect: cached read-only CLI calls ----------
from core import detect  # noqa: E402
_py = [sys.executable, "-c", "import time; print(time.time())"]
detect._READ_ONLY["python-test"] = {"-c"}
first = detect.run_cached(_py, ttl=30)[1]
check(detect.run_cached(_py, ttl=30)[1] == first, "detect: read-only result reused within its TTL")
detect.invalidate()
check(detect.run_cached(_py, ttl=30)[1] != first, "detect: invalidate() drops cached results")
check(detect._mutates(["tailscale", "serve", "--bg", "--https=443", "http://x"])
      and not detect._mutates(["tailscale", "serve", "status", "--json"])
      and not detect._mutates(["tailscale", "status", "--json"])
      and detect._mutates(["docker", "stop", "x"]) and not detect._mutates(["docker", "ps", "-a"]),
      "detect: state-changing tailscale/docker commands are recognised")

# ---------- HLC ----------
a, b = crdt.HLC("a"), crdt.HLC("b")
t1 = a.tick()
time.sleep(0.002)
t2 = b.tick()
check(crdt.newer(t2, t1), "HLC orders even when the clock does not matter")
b.observe(t1)
check(crdt.key(b.tick())[0] >= crdt.key(t2)[0], "HLC observe does not go backwards")
check(crdt.key("basura") == (-1, -1, ""), "HLC tolerates an unreadable stamp")

# ---------- CRDT clipboard ----------
ca = {"items": [{"id": "1", "text": "hola", "hlc": "0000000000100:0000:a", "ts": 1}], "tomb": []}
cb = {"items": [{"id": "2", "text": "mundo", "hlc": "0000000000200:0000:b", "ts": 2}], "tomb": []}
m = crdt.clip_merge(ca, cb)
check({i["id"] for i in m["items"]} == {"1", "2"}, "clipboard: partition keeps both items")
d = crdt.clip_delete(m, "1", "a", a_t := a, "a")
check("1" not in {i["id"] for i in d["items"]}, "clipboard: local delete")
m2 = crdt.clip_merge(d, ca)
check("1" not in {i["id"] for i in m2["items"]}, "clipboard: delete wins over older data")
newer_item = {"items": [{"id": "1", "text": "hola otra vez", "hlc": d["tomb"][0]["hlc"] + "z"}], "tomb": []}
m3 = crdt.clip_merge(d, newer_item)
check(any(i["id"] == "1" for i in m3["items"]), "clipboard: a new copy resurrects with a higher hlc")
check(m3["tomb"], "clipboard: tombstones are kept")
now = time.time()
many = {"items": [{"id": str(n), "text": str(n), "hlc": f"{1000 - n:013d}:0000:a", "ts": now - n * 3600}
                  for n in range(10)], "tomb": []}
pol_old = {"max_items": 3, "max_age": 0, "hlc": "0000000000100:0000:a"}
pol_new = {"max_items": 8, "max_age": 5 * 3600 - 1, "hlc": "0000000000200:0000:b"}
m4 = crdt.clip_merge({**many, "policy": pol_old}, {"items": [], "tomb": [], "policy": pol_new})
check(m4["policy"]["max_items"] == 8 and m4["policy"]["hlc"] == pol_new["hlc"], "clipboard: newest policy wins")
check([i["id"] for i in m4["items"]] == ["0", "1", "2", "3", "4"], "clipboard: age limit drops expired items")
m5 = crdt.clip_merge({"items": [], "tomb": [], "policy": pol_new}, {**many, "policy": pol_old})
check(m5["items"] == m4["items"] and m5["policy"] == m4["policy"], "clipboard: policy merge is commutative")
check(len(crdt.clip_merge(many, {})["items"]) == 10 and "policy" not in crdt.clip_merge(many, {}),
      "clipboard: without a policy nothing changes")
check(crdt.clip_policy({"max_items": "x", "max_age": -5}) == {"max_items": 60, "max_age": 0, "hlc": ""},
      "clipboard: invalid policy falls back to the defaults")

# ---------- CRDT subscriptions ----------
sa = {"x": {"hlc": "0000000000100:0000:a", "created": 1}}
sb = {"x": {"del": True, "hlc": "0000000000200:0000:b", "ts": 2}}
check(crdt.reg_merge(sa, sb)["x"].get("del"), "push: newest unsubscribe wins")
check(not crdt.reg_visible(crdt.reg_merge(sa, sb)), "push: unsubscribed entry is not visible")
check(crdt.reg_merge(sb, sa)["x"].get("del"), "push: merge is commutative")

# ---------- CRDT service labels ----------
la = {"10000": {"name": {"v": "Octo", "hlc": "0000000000100:0000:a"}}}
lb = {"10000": {"about": {"v": "Printer", "hlc": "0000000000200:0000:b"}}}
lm = crdt.label_merge(la, lb)
eff = crdt.label_effective(lm["10000"])
check(eff["name"] == "Octo" and eff["about"] == "Printer",
      "labels: title and description merge field by field")
check(lm == crdt.label_merge(lb, la), "labels: merge is commutative")
lc = {"10000": {"name": {"v": "OctoPrint", "hlc": "0000000000300:0000:c"}}}
check(crdt.label_effective(crdt.label_merge(lm, lc)["10000"])["name"] == "OctoPrint",
      "labels: newer title wins")
check(crdt.label_effective(crdt.label_merge(crdt.label_merge(lm, lc), la)["10000"])["name"] == "OctoPrint",
      "labels: an older title does not overwrite the newest")
clear = {"10000": {"name": {"v": "", "hlc": "0000000000400:0000:a"},
                   "about": {"v": "", "hlc": "0000000000400:0000:a"}}}
cleared = crdt.label_effective(crdt.label_merge(crdt.label_merge(lm, lc), clear)["10000"])
check(cleared["name"] == "" and cleared["about"] is None and not cleared["custom"],
      "labels: clearing returns to automatic values")
long = crdt.label_merge({}, {"x": {"name": {"v": "a" * 500, "hlc": "0000000000100:0000:a"},
                                  "about": {"v": "b" * 500, "hlc": "0000000000100:0000:a"}}})
check(len(long["x"]["name"]["v"]) == crdt.LABEL_LIMITS["name"] and
      len(long["x"]["about"]["v"]) == crdt.LABEL_LIMITS["about"],
      "labels: values are bounded")
check(crdt.label_effective({"name": {"v": "  One\n line  ", "hlc": "1"}})["name"] == "One line",
      "labels: titles are single-line")

# ---------- route order ----------
check(crdt.order_merge(["x", "a", "y", "b"], ["b", "a"]) == ["x", "b", "y", "a"],
      "route order: routes another hub cannot see keep their slots")
check(crdt.order_merge(["a", "b"], ["b", "c", "a"]) == ["b", "c", "a"],
      "route order: new routes take their place in the sent order")
check(crdt.order_merge(["a", "a", ""], [1, True, "1", None]) == ["a", "1"],
      "route order: ids are deduplicated and invalid ones dropped")

# ---------- hidden services (label field) ----------
hid = crdt.label_merge({"x": {"name": {"v": "Cam", "hlc": "0000000000100:0000:a"}}},
                       {"x": {"hidden": {"v": True, "hlc": "0000000000200:0000:b"}}})
check(crdt.label_effective(hid["x"])["hidden"] is True and crdt.label_effective(hid["x"])["name"] == "Cam",
      "hidden: merges as its own field next to the title")
shown = crdt.label_merge(hid, {"x": {"hidden": {"v": False, "hlc": "0000000000300:0000:a"}}})
check(crdt.label_effective(shown["x"])["hidden"] is False, "hidden: a newer 'show' wins")
check(crdt.label_effective({})["hidden"] is None, "hidden: unset leaves the config value in charge")

# ---------- file locations ----------
from core.adapters import files as files_adapter  # noqa: E402
from core.adapters import services as services_adapter  # noqa: E402


class _Term:
    on = False

    def state(self):
        return {"enabled": self.on}


_term = _Term()
fa = files_adapter.Impl({"platform": {"termux": False}, "config": {"files_root": str(tmp)},
                         "adapters_by_name": {"terminal": _term}})
check(fa.probe(), "files: home root found")
other = [r for r in fa.roots() if r["id"] != "home"]
check(other and all(r["locked"] for r in other), "files: disks are locked while the terminal is off")
try:
    fa.safe("", other[0]["id"])
    locked_ok = False
except PermissionError:
    locked_ok = True
check(locked_ok, "files: a locked root cannot be listed")
_term.on = True
check(not any(r["locked"] for r in fa.roots()), "files: the terminal switch unlocks them")
try:
    fa.safe("../..", "home")
    escaped = True
except PermissionError:
    escaped = False
check(not escaped, "files: paths cannot leave their root")

# ---------- WebSocket (interactive terminal) ----------
import socket  # noqa: E402
import struct  # noqa: E402
from core import ptyhost, ws as wsmod  # noqa: E402


def _frame(op, payload, fin=True, mask=b"\x01\x02\x03\x04"):
    head = bytes([(0x80 if fin else 0) | op])
    n = len(payload)
    head += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack("!H", n)
    return head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload))


class _Conn:
    def __init__(self, sock):
        self.connection, self.rfile = sock, sock.makefile("rb")


srv_sock, cli_sock = socket.socketpair()
w = wsmod.WebSocket(_Conn(srv_sock))
cli_sock.sendall(_frame(1, '{"t":"i","d":"ls"}'.encode()))
check(w.recv() == (1, b'{"t":"i","d":"ls"}'), "ws: a masked text frame is decoded")
cli_sock.sendall(_frame(9, b"") + _frame(2, b"ab", fin=False) + _frame(0, b"c" * 300))
check(w.recv() == (2, b"ab" + b"c" * 300), "ws: fragments are joined and pings answered")
pong = cli_sock.recv(2)
check(pong[0] == 0x8A, "ws: the ping got its pong")
w.send_text("é" * 70)
hdr = cli_sock.recv(4)
check(hdr[0] == 0x81 and hdr[1] == 126 and struct.unpack("!H", hdr[2:])[0] == 140, "ws: text goes out unmasked with its length")
srv_sock.close()
cli_sock.close()
if os.name == "posix":
    check(ptyhost.AVAILABLE and ptyhost.Session is ptyhost.PosixSession, "pty: POSIX shells run on a PTY")
else:  # Windows 10 1809+ has ConPTY
    check(ptyhost.Session.__name__ == "WindowsSession", "pty: Windows shells run on ConPTY")
check(ptyhost.QUERIES.sub(b"", b"a[6nb[cc[0m") == b"abc[0m",
      "pty: a replay drops terminal queries but keeps colours")

# ---------- start hooks ----------
sa = services_adapter.Impl
line = 'HOST=127.0.0.1 npm run dev -- --host "a b"'
args = sa._split(line)
check(args == ["HOST=127.0.0.1", "npm", "run", "dev", "--", "--host", "a b"], f"hooks: a command line splits ({args})")
win_path = ["node", "tools\\start-hub.cjs", "C:\\Program Files\\x"]
check(sa._split(sa._join(win_path)) == win_path, "hooks: commands survive the round trip")

# ---------- identity ----------
ident = Identity(tmp)
ident.name = "HubA"
entry = {"name": "HubA", "url": "http://a/hub/", "ip": "100.64.0.1", "priority": 50,
         "hlc": {"name": ident.hlc_tick() if hasattr(ident, "hlc_tick") else "0000000000001:0000:HubA"}}
signed = sign_entry(entry, ident)
check(crypto.verify(ub64(signed["sig"]), entry_message(signed), ub64(signed["pub"])),
      "identity: signed entry")
tampered = dict(signed, url="http://malo/hub/")
check(not crypto.verify(ub64(tampered["sig"]), entry_message(tampered), ub64(tampered["pub"])),
      "identity: changing the URL invalidates the signature")
_req = type("Req", (), {"headers": {"X-Hub-Auth": ident.token("POST", "/api/mesh", "abc")}})()
_seen_nonces = Nonces()
_pub_of = {"HubA": ident.pub_b64}.get
check(verify_token("HubB", _pub_of, _seen_nonces, _req, "POST", "/api/mesh", "abc") == "HubA",
      "identity: a signed hub request verifies")
check(verify_token("HubB", _pub_of, _seen_nonces, _req, "POST", "/api/mesh", "abc") is False,
      "identity: the same request replayed is refused (nonce)")
_req2 = type("Req", (), {"headers": {"X-Hub-Auth": ident.token("POST", "/api/mesh", "abc")}})()
check(verify_token("HubB", _pub_of, Nonces(), _req2, "POST", "/api/mesh", "other-body") is False,
      "identity: a signature does not carry over to another body")

# ---------- peering ----------
inv = make_invite("tailnet-b.ts.net", "HubB", "https://hubb.tailnet-b.ts.net/hub/", ident.pub_b64, ident.sign)
check(verify_invite(inv)[0], "peering: valid invite")
check(not verify_invite(dict(inv, url="http://evil/hub/"))[0], "peering: tampered invite is rejected")
peers = Peering(tmp / "peer")
rec = peers.add(inv, "tailnet-a.ts.net")
check(rec["domain"] == "tailnet-b.ts.net", "peering: stores the peer")
check(peers.allowed("tailnet-b.ts.net", "mesh"), "peering: mesh is allowed by default")
check(not peers.allowed("tailnet-b.ts.net", "code"), "peering: code is never shared by default")
check(peers.by_name("HubB")["pub"] == ident.pub_b64, "peering: key by name for verification")

# ---------- approvals ----------
ap = Approvals(tmp / "appr")
ap.set_me("HubA")
ap.alive_count = lambda: 2
ap.quorum["terminal"] = 2
r = ap.request("terminal", {"on": True}, "HubA", "owner")
check("pending" in r, "quorum: terminal request stays pending")
pid = r["pending"]
check("pending" not in ap.request("mesh_remove", {"name": "X"}, "HubA"), "quorum: actions with 1 approval pass on their own")
executed = []
ap.register("terminal", lambda args: executed.append(args) or {"ok": True})
ap.merge({"id": pid, "action": "terminal", "origin": "HubA", "args": {"on": True}, "ts": time.time(),
          "need": 2, "approvals": {"HubA": time.time(), "HubB": time.time()}, "denied": {}, "result": None})
ap.tick()
check(executed == [{"on": True}], "quorum: executes on the origin hub")
res = ap.approve(pid, "HubB")
check("approvals" in res or res.get("done"), "quorum: approval is idempotent")
check("error" in ap.deny("no-existe", "HubB"), "quorum: denying a missing request does not break")
ap2 = Approvals(tmp / "appr2")
ap2.merge(ap.pendings[pid])
check(ap2.public()[0]["id"] == pid, "quorum: request is replicated")

# ---------- DTN relay ----------
fwd = Forward(tmp / "fwd")
check(fwd.enqueue("HubB", "notify", {"x": 1}) is not None, "relay: enqueues")
check(len(fwd.queue) == 1, "relay: one entry")
# dedupe by message id
check(fwd.enqueue("HubB", "notify", {"x": 1}) is not None, "relay: accepts another message")
fwd.delivered = []
fwd.deliver_direct = lambda msg: (fwd.delivered.append(msg), True)[1]
fwd.process()
check(bool(fwd.delivered), "relay: direct delivery empties the queue")
check(not fwd.queue, "relay: queue is empty after delivery")

# ---------- blobs ----------
bl = Blobs(tmp / "blobs")
meta = bl.put(b"contenido de prueba", name="prueba.txt", by="HubA")
check(bl.has(meta["sha"]), "blobs: stores by content")
check(bl.file(meta["sha"]).read_bytes() == b"contenido de prueba", "blobs: retrieves the bytes")
check(bl.meta(meta["sha"])["name"] == "prueba.txt", "blobs: keeps the name")
check(bl.delete(meta["sha"]) and not bl.has(meta["sha"]), "blobs: deletes")
try:
    bl.file("../../state/release_key.json")
    bad_blob = False
except ValueError:
    bad_blob = True
check(bad_blob, "blobs: rejects path traversal ids")

# ---------- signed releases ----------
import federation  # noqa: E402

order_root = tmp / "order"
(order_root / "site" / "vendor" / "x").mkdir(parents=True)
for name in ("addon-fit.js", "LICENSE.txt", "Zeta.css"):
    (order_root / "site" / "vendor" / "x" / name).write_text(name)
(order_root / "README.md").write_text("r")
listed = [rel for _, rel in federation.code_files(order_root) if rel.startswith("site/")]
check(listed == ["site/vendor/x/LICENSE.txt", "site/vendor/x/Zeta.css", "site/vendor/x/addon-fit.js"],
      f"releases: files are fingerprinted in the same order on every OS ({listed})")

root = tmp / "release-app"
(root / "site").mkdir(parents=True)
(root / "app.py").write_text("x = 1\n", encoding="utf-8")
seed = b"\x01" * 32
pub = crypto.publickey(seed)
rel = federation.release(root, key=(seed, pub))
sig_ok, got = federation.check_release(rel)
check(sig_ok and got == __import__("core.identity", fromlist=["b64"]).b64(pub), "release: signed and verifiable")
check(rel["seq"] == 1, "release: first sequence")
old_sig = rel["sig"]
time.sleep(0.01)
(root / "app.py").write_text("x = 2\n", encoding="utf-8")
federation.forget_version()
rel2 = federation.release(root, key=(seed, pub))
check(rel2["version"] != rel["version"], "release: detects the code change")
check(rel2["seq"] == 2 and rel2["sig"] != old_sig, "release: bumps sequence and re-signs")
check(federation.check_release(rel2)[0], "release: the new signature is valid")
check(not federation.check_release({**rel2, "seq": 99})[0], "release: changing the sequence invalidates the signature")
check(not federation.check_release({k: v for k, v in rel2.items() if k != "sig"})[0],
      "release: unsigned payload is rejected when a key is pinned")

# ---------- update bundle ----------
import io  # noqa: E402
import zipfile  # noqa: E402

pkg = federation.bundle(root)
check(federation.verify_bundle(pkg, rel2) == rel2["version"], "bundle: matches the signed digest")
with zipfile.ZipFile(io.BytesIO(pkg)) as z:
    files = {n: z.read(n) for n in z.namelist()}
files["app.py"] = b"x = 666\n"
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    for name, data in files.items():
        z.writestr(name, data)
try:
    federation.verify_bundle(buf.getvalue(), rel2)
    tampered = False
except ValueError:
    tampered = True
check(tampered, "bundle: rejects content that differs from the signed digest")

# A RELEASE.json from an older updater (version+stamp, no seq) is normalized without changing the digest.
legacy = tmp / "legacy-app"
(legacy / "site").mkdir(parents=True)
(legacy / "app.py").write_text("y = 1\n", encoding="utf-8")
v = federation.code_version(legacy)
(legacy / "RELEASE.json").write_text(json.dumps({"version": v, "stamp": 1.0}), encoding="utf-8")
rel3 = federation.release(legacy)
check(rel3.get("seq") == 1 and rel3["version"] == v, "release: normalizes seq from older updaters")
check(choose_release({"version": "nueva", "seq": 2},
                     [{"name": "viejo", "release": {"version": "vieja", "seq": 1}, "ok": "vieja"}],
                     set(), {}, time.time(), 900)[0] is None,
      "release: never downgrades to an older version")

# rollback: the previous code comes back as a NEW release (higher seq), so every hub adopts it
roll = tmp / "rollback-app"
(roll / "site").mkdir(parents=True)
(roll / "core").mkdir()
(roll / "core" / "__init__.py").write_text("", encoding="utf-8")
(roll / "app.py").write_text("good = 1\n", encoding="utf-8")
federation.forget_version()
good = federation.release(roll, key=(seed, pub))
federation.snapshot_app(roll, tmp / "rollback-prev")
(roll / "app.py").write_text("broken = 1\n", encoding="utf-8")
federation.forget_version()
broken = federation.release(roll, key=(seed, pub))
check(federation.restore_app(roll, tmp / "rollback-prev"), "rollback: the snapshot is restored")
federation.republish(roll, broken["seq"])
back = federation.release(roll, key=(seed, pub))
check(back["version"] == good["version"] and back["seq"] == broken["seq"] + 1,
      "rollback: the previous code is published with a higher sequence")
check(federation.check_release(back)[0], "rollback: the new release is signed")
pick = choose_release(broken, [{"name": "author", "release": back, "ok": back["version"]}],
                      {broken["version"]: {"why": "rolled back"}}, {back["version"]: 0}, time.time(), 900)[0]
check(pick and pick["release"]["version"] == good["version"], "rollback: a hub on the bad version adopts it")
# A hub that is not the author goes back on its own and keeps the abandoned seq (Mesh.rollback):
# the author's next signed release, even one already out before the rollback, still outranks it.
local = {"version": good["version"], "seq": broken["seq"], "stamp": time.time()}
nxt = {**back, "version": "next-release", "stamp": local["stamp"] - 60}
pick = choose_release(local, [{"name": "author", "release": nxt, "ok": nxt["version"] + "x"}],
                      {broken["version"]: {}}, {nxt["version"]: 0}, time.time(), 900)[0]
check(pick is not None, "rollback on another hub: the author's next release is still adopted")

# ---------- optional modules ----------
from core.modules import ModuleRegistry  # noqa: E402

ctx = {"data": tmp, "config": {}, "name": "HubA", "notify": lambda *a, **k: None}
reg = ModuleRegistry(ctx)
reg.load()
ids = {m["id"] for m in reg.statuses()}
check({"octoprint", "advisor"} <= ids, "modules: built-in manifests load")
check(reg.status("octoprint")["state"] == "disabled", "modules: modules are disabled by default")
reg.set_config("advisor", {"enabled": True, "api_key": "test-key",
                           "base_url": "http://127.0.0.1:9/v1", "model": "test"})
st = reg.status("advisor")
check(st["state"] == "ready", "modules: advisor can be enabled")
check(st["config"]["values"]["api_key"] == {"configured": True, "value": ""},
      "modules: secret values are never returned")
try:
    reg.set_config("advisor", {"base_url": "file:///etc/passwd"})
    bad_url = False
except ValueError:
    bad_url = True
check(bad_url, "modules: invalid URLs are rejected")
reg.set_config("advisor", {"enabled": False})
check(reg.status("advisor")["state"] == "disabled", "modules: modules can be disabled")
check(reg.status("advisor")["name"] == "Architect" and reg.status("advisor")["version"] == "0.2.0"
      and reg.items["advisor"].caps == ["advisor"], "modules: name, version and capabilities come from module.json")

# A module dropped in as one folder (docs/examples/hello) is found, served and signed like the rest.
import shutil as _shutil  # noqa: E402
from core import modules as _modules  # noqa: E402
_old_files = [rel for f, rel in federation.code_files(APP) if f.exists()]
_legacy = ([f.name for f in sorted(APP.glob("*.py"), key=lambda f: f.relative_to(APP).parts)] + ["README.md"]
           + [f.relative_to(APP).as_posix() for f in sorted((APP / "site").rglob("*"), key=lambda f: f.relative_to(APP).parts)
              if f.is_file() and f.name not in federation.SKIP_SITE and "__pycache__" not in f.parts]
           + [f.relative_to(APP).as_posix() for f in sorted((APP / "core").rglob("*.py"), key=lambda f: f.relative_to(APP).parts)
              if "__pycache__" not in f.parts])
_extra = [r for r in _old_files if r not in _legacy]
check([r for r in _old_files if r in _legacy] == [r for r in _legacy if (APP / r).exists()]
      and all(r.startswith("core/modules/") and not r.endswith(".py") and r.count("/") >= 3 for r in _extra),
      "modules: the fingerprint is the older one plus the non-Python files inside module folders")
_hello = APP / "core" / "modules" / "hello"
_shutil.copytree(APP / "docs" / "examples" / "hello", _hello)
try:
    check("hello" in _modules.discover(), "modules: a folder in core/modules is discovered, no list to edit")
    reg2 = ModuleRegistry({"data": Path(tempfile.mkdtemp(prefix="hub-mod-")), "config": {}, "name": "HubA",
                           "notify": lambda *a, **k: None})
    reg2.load()
    st = reg2.set_config("hello", {"enabled": True, "greeting": "Ahoy"})
    check(st["state"] == "ready" and st["name"] == "Hello" and st["version"] == "1.0.0" and "hello" in reg2.caps(),
          "modules: the example module loads and runs from one folder")
    check(reg2.get("/api/modules/hello/state", {})["greeting"] == "Ahoy", "modules: its API answers under /api/modules/<id>/")
    check(reg2.catalog("es").get("module.hello.name") == "Hola", "modules: its strings come from its own i18n/")
    check(_modules.static_file("hello/panel.html") == (_hello / "site" / "panel.html").resolve()
          and _modules.static_file("hello/__init__.py") is None and _modules.static_file("hello/../hello/__init__.py") is None
          and _modules.static_file("../core/server.py") is None,
          "modules: only its site/ folder is public; its Python stays private")
    _files = {rel for f, rel in federation.code_files(APP)}
    check({"core/modules/hello/module.json", "core/modules/hello/site/panel.html", "core/modules/hello/i18n/es.json"} <= _files,
          "modules: every file of a one-folder module is signed and shipped with the release")
    check(_modules.module_file("hello", "../../server.py") is None, "modules: a module file cannot point outside its folder")
finally:
    _shutil.rmtree(_hello, ignore_errors=True)
    federation.forget_version()
_old_home = Path(tempfile.mkdtemp(prefix="hub-legacy-"))
_old = ModuleRegistry({"data": _old_home, "name": "HubA", "notify": lambda *a, **k: None, "config": {
    "printer": {"octoprint_url": "http://192.0.2.5:5000", "api_key": "legacy-key", "port": "COM8"}}})
_old.load()
_vals = _old._load_values("octoprint", _old._manifest("octoprint"),
                          __import__("core.modules.octoprint", fromlist=["Impl"]).Impl.legacy_config)
check(_vals.get("enabled") and _vals.get("url") == "http://192.0.2.5:5000" and _vals.get("api_key") == "legacy-key",
      "modules: an old config.json printer block becomes the OctoPrint module's settings")
check(reg.alias("printer.html") == "modules/octoprint/panel.html" and reg.alias("index.html") is None,
      "modules: an old module page redirects to its panel")
check(reg.status("octoprint")["nav"]["icon"] and reg.status("advisor")["nav"] is None,
      "modules: nav entries come from the manifests")
check("printer.status" in reg.catalog("en") and "advisor.title" in reg.catalog("es"),
      "modules: each module brings its own strings")
core_en = json.loads((APP / "site" / "i18n" / "en.json").read_text(encoding="utf-8"))
check(not [k for k in core_en if k.startswith(("printer.", "advisor.", "module.octoprint", "module.advisor"))],
      "modules: the core catalog names no module")
check(not (APP / "site" / "printer.html").exists() and not (APP / "site" / "advisor.html").exists(),
      "modules: module panels live in their module folder")
check(all((APP / "core" / "modules" / m / f).is_file() for m in ("octoprint", "advisor")
          for f in ("module.json", "i18n/en.json", "i18n/es.json", "site/panel.html")),
      "modules: the built-in modules are one folder each, ready to copy")
_src = (APP / "push.py").read_text(encoding="utf-8")
check('"print_done"' not in _src.split("MODULE_EVENTS")[0], "modules: push.py declares only core events")

# ---------- i18n catalogs ----------
en = json.loads((APP / "site" / "i18n" / "en.json").read_text(encoding="utf-8"))
es = json.loads((APP / "site" / "i18n" / "es.json").read_text(encoding="utf-8"))
check(set(en) == set(es), "i18n: English and Spanish catalogs have the same keys")
check(all(isinstance(v, str) and v for v in en.values()), "i18n: English catalog has no empty values")

# ---------- hardening: Funnel login, first boot, uploads, English logs ----------
import io  # noqa: E402
import re  # noqa: E402
import tokenize  # noqa: E402
import types  # noqa: E402
from core.adapters import disk_room  # noqa: E402
from core.adapters import funnel as _funnel  # noqa: E402


class _FakeHandler:
    """Just enough of BaseHTTPRequestHandler for the Funnel gate."""

    def __init__(self, body=b"", length=None, peer="127.0.0.1", xff=None, command="POST", path="/__login"):
        self.headers = {"Content-Length": str(len(body) if length is None else length)}
        if xff:
            self.headers["X-Forwarded-For"] = xff
        self.client_address = (peer, 50000)
        self.rfile, self.wfile, self.command, self.path = io.BytesIO(body), io.BytesIO(), command, path
        self.status, self.close_connection, self.sent = None, False, {}

    def send_response(self, code, msg=None):
        self.status = code

    def send_header(self, k, v):
        self.sent[k] = v

    def end_headers(self):
        pass


def _gate():
    g = _funnel.Impl.__new__(_funnel.Impl)
    g.public_auth = {"user": "owner", "salt": "00", "hash": "ff", "iterations": 1}
    g.login_failures, g.sessions = {}, {}
    g.sessions_lock, g.login_checks = __import__("threading").Lock(), __import__("threading").BoundedSemaphore(2)
    return g


g = _gate()
h = _FakeHandler(length=2_000_000_000)
g._handle_login(h)
check(h.status == 413 and h.rfile.tell() == 0 and h.close_connection, "funnel: a huge login body is refused unread")
h = _FakeHandler(length=-1)
g._handle_login(h)
check(h.status == 413 and h.rfile.tell() == 0, "funnel: a negative Content-Length is refused (read(-1) would read to EOF)")
for _ in range(5):
    g._handle_login(_FakeHandler(b"user=owner&password=x", xff="203.0.113.7"))
h = _FakeHandler(b"user=owner&password=x", xff="203.0.113.7")
g._handle_login(h)
check(b"Too many attempts" in h.wfile.getvalue(), "funnel: five failures lock that Internet client out")
h = _FakeHandler(b"user=owner&password=x", xff="198.51.100.9")
g._handle_login(h)
check(b"Wrong user" in h.wfile.getvalue(), "funnel: ...but not everyone else behind tailscaled")
h = _FakeHandler(b"user=owner&password=x", xff="10.9.9.9, 203.0.113.7")
g._handle_login(h)
check(b"Too many attempts" in h.wfile.getvalue(), "funnel: a forged first X-Forwarded-For entry does not dodge the lock")
for _al, _want, _label in (("es-ES,es;q=0.9,en;q=0.8", b'lang="es"', "Spanish"),
                           ("fr-FR, es;q=0.5", "Usuario o contraseña".encode(), "the first language it has"),
                           ("de-DE", b"Wrong user or password", "English for anything else")):
    h = _FakeHandler(b"user=owner&password=x", xff="198.51.100.20")
    h.headers["Accept-Language"] = _al
    g.login_failures.clear()
    g._handle_login(h)
    check(_want in h.wfile.getvalue(), f"funnel: the login page speaks the visitor's language ({_label})")
check(_funnel.Impl._visitor_ip(_FakeHandler(peer="100.101.102.103", xff="1.2.3.4")) == "100.101.102.103",
      "funnel: X-Forwarded-For is ignored unless tailscaled (loopback) sent it")
g = _gate()
for i in range(_funnel.LOGIN_TRACKED + 50):
    g._note_failure(f"10.{i >> 16 & 255}.{i >> 8 & 255}.{i & 255}", time.time())
check(len(g.login_failures) <= _funnel.LOGIN_TRACKED, "funnel: the failure table stays bounded under a scan")

# After the login, back to the page that was asked for (a deep link), never off the hub.
_sn = _funnel.Impl._safe_next
check(_sn("/clip?id=1793811594.08-pfkx77") == "/clip?id=1793811594.08-pfkx77", "funnel: next keeps a same-hub path with its query")
for _bad in ("//evil.example/x", "https://evil.example/", "/\\evil.example", "javascript:alert(1)", "evil.example",
             "/a\r\nSet-Cookie: x=1", "/__login", "/__login?x=1", "", None, "/" + "a" * 3000):
    check(_sn(_bad) == "/", "funnel: next refuses %r" % (str(_bad)[:30],))
_lp = _gate()
h = _FakeHandler(command="GET", path="/clip?id=abc")
_lp._login_page(h)
check(b'name="next" value="/clip?id=abc"' in h.wfile.getvalue(), "funnel: the login page remembers the deep link")
h = _FakeHandler(command="GET", path='/x?a="><script>')
_lp._login_page(h)
check(b"<script>" not in h.wfile.getvalue(), "funnel: the remembered link is HTML-escaped in the form")
h = _FakeHandler(b"user=owner&password=x&next=%2Fclip%3Fid%3Dabc")
_lp._handle_login(h)
check(h.status == 401 and b'value="/clip?id=abc"' in h.wfile.getvalue(), "funnel: a wrong password keeps the deep link")
_good = _gate()
_good.public_auth = {"user": "owner", "salt": "00", "iterations": 1,
                     "hash": __import__("hashlib").pbkdf2_hmac("sha256", b"pw", b"\x00", 1).hex()}
h = _FakeHandler(b"user=owner&password=pw&next=%2Fclip%3Fid%3Dabc")
_good._handle_login(h)
check(h.status == 303 and h.sent.get("Location") == "/clip?id=abc", "funnel: a good login returns to the deep link")
h = _FakeHandler(b"user=owner&password=pw&next=https%3A%2F%2Fevil.example%2F")
_good._handle_login(h)
check(h.status == 303 and h.sent.get("Location") == "/", "funnel: a good login never redirects off the hub")
h = _FakeHandler(b"user=owner&password=pw")
_good._handle_login(h)
check(h.status == 303 and h.sent.get("Location") == "/", "funnel: no next still lands on / as before")

check(disk_room(tmp, 1) is None, "uploads: a small file fits")
check("not enough disk space" in (disk_room(tmp, 1 << 60) or ""), "uploads: a file bigger than the free disk is refused")

from core import server as _server  # noqa: E402
_seen = []
_fake = types.SimpleNamespace(owners=set(), mesh=types.SimpleNamespace(
    owners=set(), known=set(), trusted_ips=lambda: _fake.mesh.known, note_unknown=_seen.append))
_H = _server.make_handler(_fake)


def _direct(ip):
    h = _H.__new__(_H)
    h.client_address, h.headers = (ip, 1), {}
    return h._ok()


check(not _direct("100.64.0.1") and _seen == ["100.64.0.1"],
      "first boot: an unknown 100.64/10 address is refused (it may be carrier-grade NAT, not the tailnet)")
check(_direct("127.0.0.1"), "first boot: this device still gets in")
_fake.mesh.known = {"100.64.0.1"}
check(_direct("100.64.0.1") and not _direct("100.101.102.103"), "known devices pass, the rest of 100.64/10 does not")
check(not _direct("192.168.1.20"), "an address outside the tailnet never passes")


def _installer_base(url, host="127.0.0.1:8790", xff=None):
    _fake.mesh.me, _fake.mesh.dir = "HubA", {"HubA": {"url": url, "ip": "100.64.0.1"}}
    h = _H.__new__(_H)
    h.client_address, h.headers = ("127.0.0.1", 1), {"Host": host, **({"X-Forwarded-For": xff} if xff else {})}
    return h._base_url()


check(_installer_base("https://huba.example.ts.net/hub/") == "https://huba.example.ts.net/hub",
      "installer: opened over loopback, it still calls back the hub's MagicDNS URL")
check(_installer_base("http://127.0.0.1:8790/hub/").startswith("http://100.64.0.1:"),
      "installer: a loopback URL is never handed to another device")
check(_installer_base("", host="huba.example.ts.net", xff="100.64.0.1") == "https://huba.example.ts.net/hub",
      "installer: through tailscale serve, the name it was reached by")

from shares import Shares  # noqa: E402
check(Shares(tmp, port=8795).port == 8795, "funnel: Funnel forwards to the hub's own port, not a fixed 8790")

from core import deps  # noqa: E402
_logs, _crypto_backend = [], crypto.BACKEND
crypto.BACKEND = "pure-python"
os.environ.pop("HUB_AUTO_DEPS", None)
deps.ensure_crypto(tmp, _logs.append, lambda: _logs.append("RESTART"))
crypto.BACKEND = _crypto_backend
check(len(_logs) == 1 and "--auto-deps" in _logs[0] and not (tmp / "deps.json").exists(),
      "deps: a hub started by hand only says how to install cryptography")

_spanish = re.compile(r"[áéíóúñ¿¡]|\b(?:coordinador|ninguno|segundo plano|dominio|aprendido|nada nuevo|sano|actualizado|desde|rechazada|fijada|clave|equipo|dispositivo|apagado|encendido|guardado|esta PC|subida|alta de|payload de|replicado|relevo|ronda|Contenedor|directa)\b")
_found = []
for f in [*APP.glob("*.py"), *(APP / "core").rglob("*.py")]:
    if "__pycache__" in f.parts or f.parent.name == "advisor":  # advisor translates old Spanish logs
        continue
    for t in tokenize.generate_tokens(io.StringIO(f.read_text(encoding="utf-8")).readline):
        if t.type in (tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1)) \
                and not t.string.startswith(('"""', "'''", 'r"""', "r'''")) and _spanish.search(t.string):
            _found.append(f"{f.name}:{t.start[0]}")
check(not _found, f"server messages are English {_found}")

import re as _re  # noqa: E402
from core import __version__  # noqa: E402
_pp = _re.search(r'^version = "([^"]+)"', (APP / "pyproject.toml").read_text(encoding="utf-8"), _re.M).group(1)
check(__version__.replace("-alpha", "a0").replace("-beta", "b0").replace("-rc", "rc") == _pp,
      f"version: core.__version__ ({__version__}) matches pyproject.toml ({_pp})")

# ---------- SSRF: hub URLs that travel by gossip ----------
from core import tailnet  # noqa: E402
from core.modules.advisor import key_unsafe  # noqa: E402
_dom = "example.ts.net"
check(tailnet.hub_url_ok("https://huba.example.ts.net/hub/", _dom)
      and tailnet.hub_url_ok("http://100.64.0.1:8790/hub/", _dom), "ssrf: tailnet names and addresses are hub URLs")
check(not any(tailnet.hub_url_ok(u, _dom) for u in (
    "http://169.254.169.254/latest/meta-data/", "http://192.168.1.1/admin", "http://127.0.0.1:8790/hub/",
    "http://localhost:6379/", "https://evil.example.com/hub/", "https://huba.other.ts.net/hub/",
    "file:///etc/passwd", "http://user:pw@100.64.0.1/hub/", "http://100.64.0.1:99999/")),
      "ssrf: metadata, LAN, loopback, other domains, other schemes and credentials are refused")
check(tailnet.hub_url_ok("http://127.0.0.1:8791/hub/", _dom, loopback=True), "ssrf: a test mesh may use loopback")
import http.server  # noqa: E402
import urllib.request  # noqa: E402


class _Redirector(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_HEAD = do_GET

    def log_message(self, *a):
        pass


_rsrv = http.server.HTTPServer(("127.0.0.1", 0), _Redirector)
__import__("threading").Thread(target=_rsrv.handle_request, daemon=True).start()
try:
    tailnet.urlopen(urllib.request.Request(f"http://127.0.0.1:{_rsrv.server_port}/x"), timeout=5)
    _followed = True
except urllib.error.HTTPError as e:
    _followed = e.code != 302
check(not _followed, "ssrf: hub-to-hub calls do not follow a redirect out of the tailnet")
__import__("threading").Thread(target=_rsrv.handle_request, daemon=True).start()
_probe_res = _server.Core.probe(f"http://127.0.0.1:{_rsrv.server_port}/", ttl=0)
check(_probe_res.get("up") and _probe_res.get("code") == 302,
      "ssrf: Core.probe marks a 302 service as up without following its redirect")
from core.modules.octoprint.printer import Printer as _Printer  # noqa: E402
__import__("threading").Thread(target=_rsrv.handle_request, daemon=True).start()
check(_Printer("http://127.0.0.1:9", "k", snapshot_url=f"http://127.0.0.1:{_rsrv.server_port}/snap").snapshot() is None,
      "ssrf: OctoPrint snapshot refuses redirects")
check(not key_unsafe("https://api.example.com/v1") and not key_unsafe("http://127.0.0.1:11434/v1")
      and key_unsafe("http://api.example.com/v1") and key_unsafe("file:///etc/passwd"),
      "architect: the API key goes over https, or plain http only to this machine, the LAN or the tailnet")
_bad_inv = make_invite("tailnet-b.ts.net", "HubB", "http://169.254.169.254/", ident.pub_b64, ident.sign)
try:
    Peering(tmp / "peer-ssrf").add(_bad_inv, "tailnet-a.ts.net")
    _added = True
except ValueError:
    _added = False
check(not _added, "ssrf: a peering invitation pointing outside its tailnet is refused")

# ---------- security review before the public release ----------
import threading as _th  # noqa: E402
from core.identity import Pins  # noqa: E402
from core.mesh import Mesh, merge_entry, prio  # noqa: E402
from core.peering import ALLOW_DEFAULT  # noqa: E402
from core.adapters import mesh as _mesh_adapter  # noqa: E402
from core import ptyhost  # noqa: E402

# A paired tailnet decides nothing about what it may do: the invitation's "allow" is ignored.
_greedy = make_invite("tailnet-c.ts.net", "HubC", "https://hubc.tailnet-c.ts.net/hub/", ident.pub_b64, ident.sign)
_greedy["allow"] = ["mesh", "catalog", "notify", "blobs", "directory", "replica", "code"]
check(Peering(tmp / "peer-greedy").add(_greedy, "tailnet-a.ts.net")["allow"] == ALLOW_DEFAULT,
      "peering: an invitation cannot grant itself more than the defaults")


def _mesh(dom="tailnet-a.ts.net"):
    """A Mesh with just the state the federation checks need (no server, no threads)."""
    m = Mesh.__new__(Mesh)
    d = Path(tempfile.mkdtemp(dir=tmp))
    m.data, m.path, m.lock = d, d / "directory.json", _th.RLock()
    m.dir, m.alive, m.remote, m.owners, m.owner_ips = {}, {}, {}, set(), set()
    m.pins, m.peering, m.hlc = Pins(d), Peering(d), crdt.HLC("HubA")
    m.labels, m.route_order, m.order_clock, m.ui = {}, [], "", {"values": {}, "clock": ""}
    m.bad, m.release_keys, m._seen_msgs = {}, set(), {}
    m._dom, m._dom_t = dom, time.time() + 1e9
    m.core = types.SimpleNamespace(ctx={"name": "HubA"}, by_name={})
    m.calls = []
    m.approvals = types.SimpleNamespace(merge_quorum=lambda *a: m.calls.append(("quorum", a)),
                                        merge=lambda b: m.calls.append(("approval", b)) or True)
    m._deliver_direct = lambda msg: m.calls.append(("forward", msg)) or True
    m._relay_hop = lambda msg: m.calls.append(("hop", msg)) or True
    return m


_m = _mesh()
_peer_ident = Identity(tmp / "peer-ident")
_m.peering.add(make_invite("tailnet-b.ts.net", "HubB", "https://hubb.tailnet-b.ts.net/hub/",
                           _peer_ident.pub_b64, _peer_ident.sign), "tailnet-a.ts.net")
_m.dir["HubOwn"] = {"name": "HubOwn", "ip": "100.64.0.2", "domain": "tailnet-a.ts.net"}
_payload = {"name": "HubB", "domain": "tailnet-a.ts.net",  # lies about its tailnet
            "directory": {"Evil": {"name": "Evil", "ip": "100.64.9.9", "domain": "tailnet-a.ts.net"},
                          "HubB": {"name": "HubB", "ip": "100.100.1.2", "domain": "tailnet-b.ts.net"}},
            "bad": {"0123456789abcdef": {"why": "x"}}, "quorum": {"values": {"terminal": 1}, "clock": "z"},
            "owners": ["mallory@example.com"], "owner_ips": ["100.64.7.7"], "services": "not a list"}
_m.receive(_payload, peer_domain="tailnet-b.ts.net", peer_name="HubB")
check("Evil" not in _m.dir and _m.dir.get("HubB", {}).get("domain") == "tailnet-b.ts.net",
      "federation: a paired tailnet cannot add a hub that claims to be in this tailnet")
check(not _m.bad and not _m.calls and not _m.owners and not _m.owner_ips,
      "federation: a paired tailnet has no say over bad versions, quorum or owners")
check(_m.remote["HubB"]["services"] == [] and _m.remote["HubB"]["release"] is None,
      "federation: a paired hub's release and malformed services are not kept")
check("100.64.0.2" in _m.trusted_ips() and "100.100.1.2" not in _m.trusted_ips(),
      "federation: a paired tailnet's hub is not trusted as the owner (only this tailnet's hubs are)")
_ml = _mesh("local")
_ml.peering.add(make_invite("tailnet-b.ts.net", "HubB", "https://hubb.tailnet-b.ts.net/hub/",
                            _peer_ident.pub_b64, _peer_ident.sign), "local")
_ml.dir.update({"HubOwn": {"name": "HubOwn", "ip": "100.64.0.2", "domain": "tailnet-a.ts.net"},
                "HubB": {"name": "HubB", "ip": "100.100.1.2", "domain": "tailnet-b.ts.net"}})
check(_ml.trusted_ips() == {"100.64.0.2"},
      "federation: a hub that has not learned its tailnet yet trusts its mesh, not a paired one")
check(not _m.receive({"name": "HubOwn", "domain": "tailnet-b.ts.net"}, peer_domain="tailnet-b.ts.net")
      and _m.dir["HubOwn"]["ip"] == "100.64.0.2",
      "federation: a paired tailnet cannot speak as one of this tailnet's hubs")
check(not _m.receive({"name": "Other"}, peer_domain="tailnet-b.ts.net", peer_name="HubB"),
      "federation: a payload speaks only for the hub that signed it")
_r = _m.relay({"id": "m1", "to": "*", "kind": "approval", "from": "HubOwn", "domain": "tailnet-a.ts.net",
               "body": {"id": "p", "approvals": ["x"]}}, peer_domain="tailnet-b.ts.net")
check(not _r["ok"] and not _m.calls, "federation: a relayed message from a paired tailnet cannot approve anything")
_r = _m.relay({"id": "m2", "to": "HubOwn", "kind": "settings", "hops": 3, "domain": "tailnet-a.ts.net",
               "body": {"values": {"terminal": 1}}}, peer_domain="tailnet-b.ts.net")
check(not _r["ok"] and not _m.calls, "federation: ...nor be carried on to this tailnet's hubs")

# Directory fields must have the right type: a text priority used to break the election everywhere.
_e, _ = merge_entry(None, {"name": "X", "priority": "999; boom", "capable": "yes", "ip": "100.64.0.3",
                           "updated": "later"})
check("priority" not in _e and "capable" not in _e and _e["ip"] == "100.64.0.3",
      "directory: fields of the wrong type are dropped")
_m.dir["Legacy"] = {"name": "Legacy", "capable": True, "priority": "x", "enrolled": "y", "domain": "tailnet-a.ts.net"}
_m.alive["Legacy"] = time.time()
check(prio(_m.dir["Legacy"]) == 50 and _m.coordinator() == "Legacy",
      "directory: a stored malformed priority no longer breaks the coordinator election")

# Without the CLI, enrollment must not hand an existing hub's name to another device.
_m.dir["HubC"] = {"name": "HubC", "pub": "P1", "ip": "100.64.0.3"}
try:
    _m.enroll("100.64.0.9", {"name": "HubC", "pub": "P2", "url": "http://100.64.0.9:8790/hub/"})
    _taken = True
except PermissionError:
    _taken = False
check(not _taken, "enroll: another device cannot take over a hub's name and key")

# Renaming a hub must tell the other hubs that the old name is gone (a tombstone, not a dropped key).
_rn = _mesh()
_rn.identity = Identity(tmp / "rename-ident")
_rn.save = lambda: None
_rn.coordinator = lambda: None
_rn.refresh_self = lambda force=False: None
_rn.view = lambda: {}
_rn.dir["localhost"] = {"name": "localhost", "pub": _rn.identity.pub_b64, "ip": "100.64.0.5",
                        "domain": "tailnet-a.ts.net", "hlc": {"removed": _rn.hlc.tick()}}
_peer = _mesh()
_peer.dir["localhost"] = json.loads(json.dumps(_rn.dir["localhost"]))  # what the others knew before
_rn.rename_self("localhost", "Galaxy Watch")
check(_rn.dir["localhost"].get("removed") is True and _rn.dir["Galaxy Watch"].get("removed") is False,
      "rename: the old name stays as a tombstone and the new one is live")
_peer.merge(json.loads(json.dumps(_rn.dir)))
check(_peer.dir["localhost"].get("removed") is True and not _peer.dir["Galaxy Watch"].get("removed"),
      "rename: other hubs learn that the old name is gone")
_rn.merge({"localhost": {**_peer.dir["localhost"], "removed": False,
                         "hlc": {"removed": "0000000000000:0000:Old"}}})
check(_rn.dir["localhost"].get("removed") is True,
      "rename: an older copy of the old name gossiped back does not revive it")

# Removing a hub always goes through the approval quorum: a request cannot ask to skip it.
_rq = _mesh()
_rq.save = lambda: None
_rq.coordinator = lambda: None
_rq.view = lambda: {"view": True}
_rq.dir["Old"] = {"name": "Old", "ip": "100.64.0.7", "domain": "tailnet-a.ts.net"}
_asked = []
_rq.approvals = types.SimpleNamespace(request=lambda kind, args, me, by: _asked.append(kind) or
                                      {"approved": False, "need": 2})
_res = _rq.set_hub("Old", {"remove": True, "skip_quorum": True})
check("pending" in _res and _asked == ["mesh_remove"] and not _rq.dir["Old"].get("removed"),
      "approvals: a request cannot skip the quorum to remove a hub")
_ad = _mesh_adapter.Impl.__new__(_mesh_adapter.Impl)
_ad.mesh = _rq
_hdl = types.SimpleNamespace(headers={})
_res = _ad.post("/api/mesh/hub", {"name": "Old", "remove": True, "skip_quorum": True}, {}, _hdl)
check(isinstance(_res, dict) and "pending" in _res and not _rq.dir["Old"].get("removed"),
      "approvals: ...not through the API either")
_rq._exec_remove({"name": "Old"})
check(_rq.dir["Old"].get("removed") is True, "approvals: once the quorum agrees, the hub is removed")

# The fleet carries each hub's tailnet address and DNS name, so the devices list can match a device to its
# hub when the names differ ("Galaxy Watch" runs on the device galaxy-watch).
_fm = _mesh()
_fm.dir["Galaxy Watch"] = {"name": "Galaxy Watch", "ip": "100.64.0.5", "dns": "galaxy-watch.tailnet-a.ts.net",
                           "url": "https://galaxy-watch.tailnet-a.ts.net/hub/", "domain": "tailnet-a.ts.net"}
_fm.view = lambda: {"coordinator": "HubA", "domain": "tailnet-a.ts.net", "release": {},
                    "hubs": [{"name": "Galaxy Watch", "online": True, "coordinator": False, "here": False,
                              "url": "https://galaxy-watch.tailnet-a.ts.net/hub/"}]}
_fa = _mesh_adapter.Impl.__new__(_mesh_adapter.Impl)
_fa.mesh = _fm
_fh = _fa.fleet()["hubs"][0]
check(_fh["ip"] == "100.64.0.5" and _fh["dns"] == "galaxy-watch.tailnet-a.ts.net",
      "fleet: hubs carry their tailnet address and DNS name")

# Changing a hub's Tailscale hostname waits for the new DNS name before recreating Serve routes
# (and again on rollback), and allows 90 s for a cold first certificate.
check(_server._wait_hub.__defaults__ == (90,), "rename: _wait_hub allows 90 s for cold certificate issuance")
_orig_ts = (_server._ts_self, _server._serve_snapshot, _server._serve_restore,
            _server._run_ts, _server._wait_hub, _server.save_conf)
try:
    _dns_queue = ["galaxy-watch.tailnet-a.ts.net",
                  "galaxy-watch.tailnet-a.ts.net",  # still old right after `tailscale set --hostname`
                  "watch-two.tailnet-a.ts.net"]
    _cur_dns = ["galaxy-watch.tailnet-a.ts.net"]
    _restored_on = []

    def _fake_ts_self():
        if _dns_queue:
            _cur_dns[0] = _dns_queue.pop(0)
        return {"Peer": {}}, {}, _cur_dns[0], _cur_dns[0].split(".")[0]

    _server._ts_self = _fake_ts_self
    _server._serve_snapshot = lambda: [{"port": 443, "path": "/hub", "target": "http://127.0.0.1:8790"}]
    _server._serve_restore = lambda routes: _restored_on.append(_cur_dns[0])
    _server._run_ts = lambda *a, **k: (0, "")
    _server._wait_hub = lambda url, seconds=90: True
    _server.save_conf = lambda c: None
    _rcore = types.SimpleNamespace(
        owners=set(), by_name={}, adapters=(), req=_th.local(),
        conf={"name": "Galaxy Watch", "master": "https://huba.tailnet-a.ts.net/hub/",
              "self_dns": "galaxy-watch.tailnet-a.ts.net",
              "self_url": "http://galaxy-watch.tailnet-a.ts.net:8790/hub/"},
        ctx={"name": "Galaxy Watch", "req": _th.local()},
        identity=types.SimpleNamespace(pub_b64="P1", name="Galaxy Watch"),
        approvals=types.SimpleNamespace(set_me=lambda n: None),
        mesh=types.SimpleNamespace(
            owners=set(), known=set(), dir={}, trusted_ips=lambda: set(),
            note_unknown=lambda ip: None, verify_hub=lambda h: None,
            rename_self=lambda old, new: None,
        ),
    )
    _RH = _server.make_handler(_rcore)

    def _post_name(body_dict, sleep_fn):
        _old_sleep = _server.time.sleep
        _server.time.sleep = sleep_fn
        try:
            raw = json.dumps(body_dict).encode()
            h = _RH.__new__(_RH)
            h.client_address, h.command, h.path = ("127.0.0.1", 1), "POST", "/hub/api/hub/name"
            h.headers = {"Host": "127.0.0.1:8790", "Content-Length": str(len(raw))}
            h.rfile = io.BytesIO(raw)
            out = {}
            h._json = lambda d, code=200: out.update(status=code, body=d)
            h.do_POST()
            return out
        finally:
            _server.time.sleep = _old_sleep

    _r1 = _post_name({"name": "Watch Two", "change_dns": True}, lambda s: None)
    check(_r1.get("status") == 200 and _restored_on == ["watch-two.tailnet-a.ts.net"]
          and _rcore.conf.get("self_url") == "https://watch-two.tailnet-a.ts.net/hub/",
          "rename: Serve routes are recreated only after Tailscale reports the new DNS name")

    # Rollback when the new hostname does not become healthy must also wait for the old DNS name before
    # recreating Serve, so Serve is never left pointing at the failed hostname.
    _rcore.conf["name"] = _rcore.ctx["name"] = "Galaxy Watch"
    _dns_queue[:] = ["galaxy-watch.tailnet-a.ts.net",
                     "watch-two.tailnet-a.ts.net",
                     "watch-two.tailnet-a.ts.net",  # still new right after rollback `tailscale set --hostname`
                     "galaxy-watch.tailnet-a.ts.net"]
    _restored_on.clear()
    _server._wait_hub = lambda url, seconds=90: "galaxy-watch" in url
    _r2 = _post_name({"name": "Watch Two", "change_dns": True}, lambda s: None)
    check(_r2.get("status") == 502 and _restored_on == ["watch-two.tailnet-a.ts.net", "galaxy-watch.tailnet-a.ts.net"],
          "rename: rollback waits for the previous DNS name before restoring Serve routes")
finally:
    (_server._ts_self, _server._serve_snapshot, _server._serve_restore,
     _server._run_ts, _server._wait_hub, _server.save_conf) = _orig_ts


class _GotoHandler:
    def __init__(self, path):
        self.path, self.headers, self.command = path, {}, "GET"

    def _route(self):
        return urllib.parse.urlparse(self.path).path[4:]


import urllib.parse  # noqa: E402
_ga = _mesh_adapter.Impl.__new__(_mesh_adapter.Impl)
_opened = []
_ga.mesh = types.SimpleNamespace(me="HubA", catalog=lambda: [{"key": "k", "hubs": ["HubB"], "hub": "HubB"}],
                                 is_foreign=lambda n: n == "HubB",
                                 open=lambda *a, **k: _opened.append(a) or (_ for _ in ()).throw(OSError()))
check(_ga.proxy_anycast(_GotoHandler("/hub/api/goto/k/"), "GET")[1] == 404 and not _opened,
      "goto: never proxies the browser's request to a paired tailnet's hub")
_ga.mesh.catalog = lambda: [{"key": "k", "hubs": ["HubB", "HubD"], "hub": "HubB"}]
_ga.proxy_anycast(_GotoHandler("/hub/api/goto/k/search?q=1"), "GET")
check(_opened and _opened[0][0] == "HubD" and _opened[0][1] == "/api/svc/k/search?q=1",
      "goto: picks this tailnet's hub and keeps the query string")

# Update bundles carry only signed files.
with zipfile.ZipFile(io.BytesIO(pkg)) as z:
    _files = {n: z.read(n) for n in z.namelist()}
for _extra, _why in (("core/__pycache__/server.cpython-312.pyc", "a .pyc"),
                     ("core/crypto.cp312-win_amd64.pyd", "a native module")):
    _buf = io.BytesIO()
    with zipfile.ZipFile(_buf, "w") as z:
        for name, data in {**_files, _extra: b"evil"}.items():
            z.writestr(name, data)
    try:
        federation.verify_bundle(_buf.getvalue(), rel2)
        _passed = True
    except ValueError:
        _passed = False
    check(not _passed, f"bundle: {_why} outside the signed fingerprint is refused")
_dest = tmp / "apply-extra"
_dest.mkdir()
federation.apply_bundle(_dest, _buf.getvalue())
check((_dest / "app.py").exists() and not (_dest / "core" / "crypto.cp312-win_amd64.pyd").exists(),
      "bundle: applying never copies a file the signature does not cover")

# Browser requests from another origin, including another port of the same name.
def _xsite(headers):
    h = _H.__new__(_H)
    h.client_address, h.headers = ("127.0.0.1", 1), headers
    return h._cross_site()


check(_xsite({"Host": "huba.example.ts.net", "Origin": "https://huba.example.ts.net:8443"}),
      "csrf: a page on another port of the hub's name (a Funnel share) is another origin")
check(not _xsite({"Host": "huba.example.ts.net", "Origin": "https://huba.example.ts.net"})
      and not _xsite({"Host": "127.0.0.1:8790", "Origin": "http://127.0.0.1:8790"})
      and not _xsite({"Host": "huba.example.ts.net:443", "Origin": "https://huba.example.ts.net"}),
      "csrf: the hub's own pages pass (default ports are the same origin)")
check(_xsite({"Host": "huba.example.ts.net", "Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "no-cors"})
      and not _xsite({"Host": "huba.example.ts.net", "Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate"}),
      "csrf: another device of the tailnet may link to the hub, not fetch from it")
check(not _xsite({"Host": "huba.example.ts.net"}), "csrf: CLI and hub clients (no Origin) still pass")
_ch = _H.__new__(_H)
_ch.client_address, _ch.headers = ("127.0.0.1", 1), {"X-Forwarded-For": "100.64.0.9, 100.64.0.5"}
check(_ch._client_ip() == "100.64.0.5", "client ip: behind tailscale serve, the entry tailscaled wrote")
_ch.client_address = ("100.64.0.7", 1)
check(_ch._client_ip() == "100.64.0.7", "client ip: X-Forwarded-For counts only from loopback")

# The hub's own folders follow the terminal switch, even inside the home folder.
(tmp / "hubdata").mkdir(exist_ok=True)
_fa2 = files_adapter.Impl({"platform": {"termux": False}, "config": {"files_root": str(tmp)},
                           "adapters_by_name": {"terminal": _term}, "data": tmp / "hubdata"})
_fa2.probe()
_term.on = False
try:
    _fa2.safe("hubdata/release_key.json", "home")
    _open = True
except PermissionError:
    _open = False
check(not _open and _fa2.safe("order", "home"), "files: the hub's data folder stays shut while the terminal is off")
_term.on = True
check(_fa2.safe("hubdata", "home"), "files: ...and opens with it")
_term.on = False
_fa3 = files_adapter.Impl({"platform": {"termux": False}, "config": {"files_root": str(tmp / "hubdata")},
                           "adapters_by_name": {"terminal": _term}, "data": tmp})
_fa3.probe()
check(_fa3.safe("", "home"), "files: a shared root inside the data folder still opens (the demo's fake home)")
_term.on = True

# Funnel: the hub itself is never published; odd tokens do not crash the gate.
_sh = Shares(tmp, port=8795)
for _route_ in ({"kind": "proxy", "path": "/hub/", "target": "http://127.0.0.1:8795", "id": "h"},
                {"kind": "proxy", "path": "/", "target": "http://localhost:8795", "id": "h2"}):
    try:
        _sh.start([_route_])
        _shared = True
    except ValueError:
        _shared = False
    check(not _shared, f"funnel: the hub itself cannot be shared ({_route_['target']}{_route_['path']})")
_sh.groups = [{"token": "abc", "expires": None, "routes": []}]
check(_sh.valid("ñ") is None and _sh.valid("abc") is not None, "funnel: a non-ASCII token is refused, not a crash")


class _FakeTailscale:
    """Stands in for subprocess.run: per-port Serve state and a version warning on stderr."""
    def __init__(self):
        self.web, self.calls, self.fail_on = {}, [], None

    def __call__(self, args, **kw):
        import types
        self.calls.append(args[1:])
        out, err, code = b"", b"", 0
        if args[1:3] == ["serve", "status"]:
            web = {f"h.example:{p}": {"Handlers": {"/": {"Proxy": t}}} for p, t in self.web.items()}
            out, err = json.dumps({"Web": web}).encode(), b'Warning: client version "1" != server "2"\n'
        else:
            port = int(next(a for a in args if a.startswith("--https=")).split("=")[1])
            if args[-1] == "off":
                self.web.pop(port, None)
            elif port == self.fail_on:
                code, err = 1, b"funnel refused"
            else:
                self.web[port] = args[-1]
        return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)


import shares as _shares_mod  # noqa: E402
_real_run, _fake = _shares_mod.subprocess.run, _FakeTailscale()
_shares_mod.subprocess.run = _fake
_gdir = Path(tempfile.mkdtemp())
_cam = {"kind": "proxy", "path": "/", "target": "http://127.0.0.1:8765", "id": "cam", "name": "Cam"}
_vid = {"kind": "proxy", "path": "/", "target": "http://127.0.0.1:5050", "id": "vid", "name": "Vid"}
_app = {"kind": "proxy", "path": "/", "target": "http://127.0.0.1:4319", "id": "app", "name": "App"}
_fake.web[10000] = "http://127.0.0.1:7000"  # something the owner already served there
_sg = Shares(_gdir, port=8795)
_a = _sg.start([_cam, _vid], "parents", 0, "", "h.example")
check(len(_sg.view()) == 1 and 8443 in _fake.web, "funnel: a version warning on stderr does not break publishing")
_b = _sg.start([_app], "app", 0, "", "h.example")
check(_a["token"] != _b["token"] and _sg.valid(_a["token"]) and _sg.valid(_b["token"]),
      "funnel: a new group leaves the existing group and its token alone")
check(_sg.target_for(_sg.valid(_a["token"], 8443), 8443)["id"] == "cam"
      and _sg.target_for(_sg.valid(_b["token"], 8443), 8443)["id"] == "app",
      "funnel: on one public port the token picks the service")
check(_sg.valid(_b["token"], 10000) is None, "funnel: a group has no access to a port it does not map")
_sg2 = Shares(_gdir, port=8795)
check([g["token"] for g in _sg2.view()] == [_a["token"], _b["token"]], "funnel: every group survives a hub restart")
_sg2.groups = []
_oldfmt = json.loads((_gdir / "shares.json").read_text(encoding="utf-8"))
check(_oldfmt["token"] == _a["token"] and _oldfmt["routes"], "funnel: the first group stays readable by a rolled-back hub")
_calls = len(_fake.calls)
check(_sg.stop(_b["token"])["stopped"] and _sg.valid(_a["token"], 8443) and 8443 in _fake.web
      and not [c for c in _fake.calls[_calls:] if "off" in c and "--https=8443" in c],
      "funnel: stopping a group keeps a port another group still uses")
_sg.stop(_a["token"])
check(_fake.web.get(10000) == "http://127.0.0.1:7000", "funnel: closing a port restores what was served there before")
_c = _sg.start([_cam, _vid], "one", 0, "", "h.example")
_d = _sg.start([_app, _vid], "two", 0, "", "h.example")
_sg.stop(_c["token"])
check(_sg.valid(_d["token"], 8443) and 8443 in _fake.web and _fake.web.get(10000) == "http://127.0.0.1:8795",
      "funnel: the group left on the ports keeps them open")
_sg.stop(_d["token"])
check(not _sg.view() and not (_gdir / "shares.json").exists() and 8443 not in _fake.web
      and _fake.web.get(10000) == "http://127.0.0.1:7000", "funnel: the last group out restores what was served before")
_fake.fail_on = 8443
try:
    _sg.start([_cam], "bad", 0, "", "h.example")
    _raised = False
except RuntimeError:
    _raised = True
check(_raised and not _sg.view(), "funnel: a group that cannot open its port is not recorded")
_fake.fail_on = None
_shares_mod.subprocess.run = _real_run

from core.adapters import monitor as _monitor  # noqa: E402
_now = time.time()
_kept = _monitor.prune({"gone": {"os": "linux", "samples": [[_now - 11 * 86400, 1, 5.0, "direct"]]},
                        "here": {"os": "linux", "samples": [[_now - 2 * 86400, 0, None, None],
                                                            [_now - 60, 1, 4.0, "direct"]]}}, _now)
check(list(_kept) == ["here"] and len(_kept["here"]["samples"]) == 1,
      "monitor: only the last 24 h, without devices that left the tailnet or were renamed")
check(ptyhost._size("wide", None) == (80, 24) and ptyhost._size(9999, 2) == (500, 4),
      "terminal: odd sizes from the browser are clamped instead of raising")
_avisos = (APP / "site" / "avisos.js").read_text(encoding="utf-8")
check("${l.title}" not in _avisos and "${esc(l.title)}" in _avisos,
      "ui: notification titles (device and service names) are escaped")

print(f"\n{ok} checks OK - data in {tmp}")
