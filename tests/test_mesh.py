"""Integration: two hubs isolated on 127.0.0.1 (8791/8792), without touching production.

Checks signed gossip, the directory, terminal quorum and removal of a hub.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
ROOT = Path(tempfile.mkdtemp(prefix="hub-integration-"))
PY = sys.executable
H1 = "http://127.0.0.1:8791/hub"
H2 = "http://127.0.0.1:8792/hub"

if ROOT.exists():
    shutil.rmtree(ROOT)
H1_DIR, H2_DIR = ROOT / "h1", ROOT / "h2"
H1_DIR.mkdir(parents=True)
H2_DIR.mkdir(parents=True)
(H1_DIR / "config.json").write_text(json.dumps({
    "name": "HubA", "domain": "testnet", "url": H1 + "/", "priority": 50,
    "routes": [{"port": 5050, "path": "/"}]}), encoding="utf-8")
(H2_DIR / "config.json").write_text(json.dumps({
    "name": "HubB", "priority": 10, "routes": [{"port": 6060, "path": "/"}]}), encoding="utf-8")

# The real Tailscale CLI must stay out of reach: an isolated hub that found it would learn the real
# tailnet domain. Drop only the folders that contain it, on any OS.
def _has_tailscale(d):
    try:
        return any((Path(d) / n).exists() for n in ("tailscale", "tailscale.exe"))
    except OSError:  # a folder we may not even look into (WSL sees some Windows folders like that)
        return False


NO_TAILSCALE = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep) if d and not _has_tailscale(d))
ENV = {**os.environ,
       "PATH": NO_TAILSCALE,
       "HUB_NO_RESTART": "1", "HUB_OK_GRACE": "1", "HUB_MESH_EVERY": "2",
       "HUB_MESH_ALIVE": "8", "HUB_MESH_ANTI": "1", "HUB_UPDATE_GRACE": "9999",
       "HUB_FORCE_CAPABLE": "1", "HUB_RELEASE_WRITE": "0"}
procs = []


def start(home, port, name):
    env = dict(ENV, HUB_CORE_HOME=str(home), HUB_PORT=str(port), HUB_NAME=name, HUB_BIND="127.0.0.1")
    log = (ROOT / f"{name}.log").open("w", encoding="utf-8")
    p = subprocess.Popen([PY, "-m", "core"], cwd=APP, env=env, stdout=log, stderr=subprocess.STDOUT)
    procs.append((p, log))
    return p


def get(base, path, timeout=5):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.load(r)


def post(base, path, body, timeout=10):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def wait_for(fn, label, timeout=40, interval=0.5):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            v = fn()
            if v:
                print(f"  ok  {label}")
                return v
        except Exception as e:
            last = e
        time.sleep(interval)
    raise AssertionError(f"{label} ({last})")


def stop_all():
    for p, log in procs:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            p.kill()
        log.close()


try:
    print("starting HubA...")
    start(H1_DIR, 8791, "HubA")
    wait_for(lambda: get(H1, "/api/caps"), "HubA responds on /api/caps")
    caps = get(H1, "/api/caps")
    assert caps["name"] == "HubA" and caps["protocol"] == 2, caps
    print("  ok  protocol 2")

    print("enrolling HubB...")
    env = dict(ENV, HUB_CORE_HOME=str(H2_DIR), HUB_PORT="8792", HUB_NAME="HubB", HUB_BIND="127.0.0.1")
    r = subprocess.run([PY, "-m", "core", "--home", str(H2_DIR), "--port", "8792", "--bind", "127.0.0.1",
                        "--name", "HubB", "--enroll", H1 + "/"], cwd=APP, env=env,
                       capture_output=True, text=True, timeout=60)
    print(r.stdout.strip() or r.stderr.strip())
    assert r.returncode == 0, r.stderr
    conf2 = json.loads((H2_DIR / "config.json").read_text(encoding="utf-8"))
    assert conf2.get("domain") == "testnet", conf2
    print("  ok  enrollment succeeded and domain was learned")

    start(H2_DIR, 8792, "HubB")
    wait_for(lambda: get(H2, "/api/caps"), "HubB responds on /api/caps")

    def both():
        v1 = get(H1, "/api/mesh/view")
        v2 = get(H2, "/api/mesh/view")
        names1 = {h["name"] for h in v1["hubs"] if not h["removed"]}
        names2 = {h["name"] for h in v2["hubs"] if not h["removed"]}
        if {"HubA", "HubB"} <= names1 and {"HubA", "HubB"} <= names2 and len(v1["hubs"]) == 2 and len(v2["hubs"]) == 2:
            return v1, v2
        return None

    v1, v2 = wait_for(both, "both hubs appear in the directory", timeout=45)
    wait_for(lambda: all(h["online"] for h in get(H1, "/api/mesh/view")["hubs"] if not h["removed"]),
             "gossip: HubB appears online", timeout=40)
    assert get(H1, "/api/mesh/view")["coordinator"] in ("HubA", "HubB"), "no coordinator"
    print("  ok  coordinator elected")

    pins1 = json.loads((H1_DIR / "identity_pins.json").read_text(encoding="utf-8"))
    pins2 = json.loads((H2_DIR / "identity_pins.json").read_text(encoding="utf-8"))
    assert pins1.get("HubB") and pins2.get("HubA"), (pins1, pins2)
    print("  ok  TOFU keys pinned in both directions")

    print("service labels...")
    assert "labels" in get(H1, "/api/caps")["caps"]
    r = post(H1, "/api/labels", {"id": "5050", "name": "Media server", "about": "Jellyfin"})
    assert r.get("ok") and r["label"]["name"] == "Media server", r
    assert get(H1, "/api/labels")["labels"]["5050"]["name"] == "Media server"
    print("  ok  custom title and description saved")

    def labelled_on_b():
        s = next((x for x in get(H2, "/api/catalog")["services"] if x["key"] == "5050"), None)
        return s if s and s["name"] == "Media server" and s.get("about") == "Jellyfin" and s.get("custom") else None

    wait_for(labelled_on_b, "HubB receives the label through the mesh", timeout=25)
    route = next(x for x in get(H1, "/data.json")["routes"] if x["id"] == "5050")
    assert route["name"] == "Media server" and route["about"] == "Jellyfin", route
    print("  ok  the index renders the custom label")

    post(H1, "/api/labels", {"id": "5050", "name": "", "about": ""})

    def automatic_on_b():
        s = next((x for x in get(H2, "/api/catalog")["services"] if x["key"] == "5050"), None)
        return s if s and s["name"] == "5050/" and not s.get("custom") else None

    wait_for(automatic_on_b, "clearing the label returns to the automatic value", timeout=25)
    print("  ok  clearing a label propagates too")

    # A label set on one hub also renames a service that lives on another hub.
    r = post(H1, "/api/labels", {"id": "6060", "name": "Remote lab", "about": "Label set from HubA"})
    assert r.get("ok"), r

    def remote_index():
        route = next((x for x in get(H2, "/data.json")["routes"] if x["id"] == "6060"), None)
        return route if route and route["name"] == "Remote lab" and route["about"] == "Label set from HubA" else None

    wait_for(remote_index, "a label set on HubA reaches HubB's own index", timeout=25)
    print("  ok  labels apply to services hosted on another hub")
    post(H1, "/api/labels", {"id": "6060", "name": "", "about": ""})
    try:
        post(H1, "/api/labels", {"id": "not-a-service", "name": "x"})
        unknown = False
    except urllib.error.HTTPError as e:
        unknown = e.code == 404
    assert unknown, "an unknown service cannot be labelled"
    print("  ok  unknown services are rejected")

    print("route order...")
    order_of = lambda home: json.loads((home / "route_order.json").read_text(encoding="utf-8"))["ids"]
    post(H2, "/api/routes/order", {"ids": ["6060", "hidden-on-b"]})
    wait_for(lambda: order_of(H1_DIR) == ["6060", "hidden-on-b"], "HubA receives HubB's route order", timeout=25)
    r = post(H1, "/api/routes/order", {"ids": ["5050", "6060"]})
    assert r["ids"] == ["5050", "hidden-on-b", "6060"], r
    print("  ok  routes HubA cannot see keep their slot")
    wait_for(lambda: order_of(H2_DIR) == ["5050", "hidden-on-b", "6060"], "HubB adopts the newer order", timeout=25)
    assert [x["id"] for x in get(H1, "/data.json")["routes"] if x["id"] in ("5050", "6060")][0] == "5050"
    print("  ok  the index follows the mesh order")

    print("terminal quorum = 2...")
    post(H1, "/api/mesh/settings", {"quorum": {"terminal": 2}})
    time.sleep(4)  # let HubB receive the setting
    q2 = get(H2, "/api/mesh/approvals")["view"]["quorum"]
    assert q2.get("terminal") == 2, q2
    print("  ok  the setting was replicated")

    r = post(H1, "/api/term/enable", {"on": True})
    assert "pending" in r, r
    assert r["pending"]["need"] == 2, r
    print("  ok  enabling the terminal waits for another hub")

    def pending_on_b():
        a = get(H2, "/api/mesh/approvals")["view"]["pending"]
        p = next((x for x in a if x["action"] == "terminal" and not x["result"]), None)
        if p and "HubA" in p["approvals"]:
            return p
        return None

    p = wait_for(pending_on_b, "HubB sees the pending request", timeout=20)
    res = post(H2, f"/api/mesh/approvals/{p['id']}/approve", {})
    assert res, res
    print("  ok  HubB approves it")

    def enabled():
        t = get(H1, "/api/term")
        return t if t.get("enabled") else None

    wait_for(enabled, "the origin hub executes and enables the terminal", timeout=25)
    done = next(x for x in get(H1, "/api/mesh/approvals")["view"]["pending"] if x["id"] == p["id"])
    assert done["result"] and done["result"].get("ok"), done
    print("  ok  result was propagated")
    post(H1, "/api/term/enable", {"on": False})

    print("quorum to remove a hub = 2...")
    post(H1, "/api/mesh/settings", {"quorum": {"mesh_remove": 2}})
    time.sleep(4)
    r = post(H1, "/api/mesh/hub", {"name": "HubB", "remove": True})
    assert "pending" in r, r
    print("  ok  the removal stays pending")

    def remove_pending():
        for x in get(H2, "/api/mesh/approvals")["view"]["pending"]:
            if x["action"] == "mesh_remove" and not x["result"] and "HubA" in x["approvals"]:
                return x
        return None

    p2 = wait_for(remove_pending, "HubB sees the removal request", timeout=20)
    post(H2, f"/api/mesh/approvals/{p2['id']}/approve", {})
    print("  ok  HubB approves its own removal")

    def removed():
        v = get(H1, "/api/mesh/view")
        h = next((x for x in v["hubs"] if x["name"] == "HubB"), None)
        return h if h and h["removed"] else None

    wait_for(removed, "HubB is marked as removed in the directory", timeout=25)

    print("\nINTEGRATION OK")
finally:
    stop_all()
    for f in ROOT.glob("*.log"):
        print(f"\n--- {f.name} ---")
        tail = f.read_text(encoding="utf-8", errors="replace")[-3000:]
        sys.stdout.write(tail.encode("ascii", "replace").decode("ascii"))
    shutil.rmtree(ROOT, ignore_errors=True)
