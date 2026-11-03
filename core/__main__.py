"""python -m core [options]

  --home DIR        data folder (config.json, state); defaults to ~/.hub-core
  --port N          port (default 8790)
  --bind IP         127.0.0.1 if the hub runs behind tailscale serve
  --name N          hub name (default: the machine's)
  --enroll URL      enroll in the mesh through any hub
  --detect          show platform and detected capabilities
  --peer-invite     print a signed invitation to pair another tailnet
  --peer-add JSON   accept the invitation from another tailnet (JSON or file)
  --peer-list       paired tailnets
  --peer-remove DOM remove a paired tailnet
  --signing-key     create (if missing) the key that signs versions and print its public key
  --identity        print this hub's public key
  --version         print the code fingerprint
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ap = argparse.ArgumentParser(prog="python -m core")
ap.add_argument("--home", help="data folder (config.json, state); defaults to ~/.hub-core")
ap.add_argument("--port", type=int, help="port (default 8790)")
ap.add_argument("--enroll", metavar="HUB_URL", help="join the mesh through any hub")
ap.add_argument("--name", help="hub name (default: hostname)")
ap.add_argument("--bind", default=None, help="127.0.0.1 when the hub is behind tailscale serve")
ap.add_argument("--auto-deps", action="store_true",
                help="install optional native dependencies (cryptography) in the background; the installers set it")
ap.add_argument("--detect", action="store_true", help="show detected platform and capabilities")
ap.add_argument("--peer-invite", action="store_true", help="signed invitation for another tailnet")
ap.add_argument("--peer-add", metavar="JSON_OR_FILE", help="accept an invitation from another tailnet")
ap.add_argument("--peer-list", action="store_true", help="paired tailnets")
ap.add_argument("--peer-remove", metavar="DOMAIN", help="remove a paired tailnet")
ap.add_argument("--signing-key", action="store_true", help="create the release signing key")
ap.add_argument("--identity", action="store_true", help="this hub's public key")
ap.add_argument("--version", action="store_true", help="code fingerprint")
ap.add_argument("--release", action="store_true", help="running and previous version, updates paused or not")
ap.add_argument("--rollback", action="store_true", help="go back to the previous version (mesh-wide on the author hub)")
ap.add_argument("--pause-updates", action="store_true", help="stop adopting new versions on this hub")
ap.add_argument("--resume-updates", action="store_true", help="adopt new versions again")
ap.add_argument("--apply-update", action="store_true",
                help="apply the code on disk now (after git pull on the author hub) instead of waiting 2 minutes")
ap.add_argument("--url", help="public hub URL for the invitation (https://.../hub/)")
ap.add_argument("--domain", help="tailnet domain for the invitation")
a = ap.parse_args()
# the server reads these variables on import: set them beforehand
if a.home:
    os.environ["HUB_CORE_HOME"] = a.home
if a.port:
    os.environ["HUB_PORT"] = str(a.port)
if a.auto_deps:
    os.environ["HUB_AUTO_DEPS"] = "1"
if a.bind:
    os.environ["HUB_BIND"] = a.bind
if a.name:
    os.environ["HUB_NAME"] = a.name


def _supervise():
    """Windows has no supervisor for the hub: the scheduled task's process becomes one.

    Task Scheduler kills every process of the task when its own process exits and does not
    restart it on an exit code, so a hub that exits to restart stayed down. This parent never
    exits on a restart request (code 3): it relaunches the hub inside the same task, picking up
    the new code. A crash loop gives up after a few fast failures. It does not import core, so
    a broken update cannot break it; the boot guardian below still restores app.prev.
    """
    import subprocess
    import time
    env = {**os.environ, "HUB_SUPERVISED": "1"}
    # Under pythonw (the scheduled task) nothing sees the hub's output, so a crash left no trace:
    # keep it in <data>/hub.log, rotated at 2 MB. From a console, output stays on the console.
    console = sys.stdout is not None and sys.stdout.isatty()
    log_path = Path(os.environ.get("HUB_CORE_HOME") or Path.home() / ".hub-core") / "hub.log"
    fails = 0
    while True:
        t0 = time.time()
        out = None
        if not console:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                if log_path.exists() and log_path.stat().st_size > 2 << 20:
                    log_path.replace(log_path.with_suffix(".log.1"))
                out = log_path.open("ab")
            except OSError:
                out = None
        try:
            code = subprocess.call([sys.executable, "-m", "core", *sys.argv[1:]], env=env,
                                   stdout=out, stderr=subprocess.STDOUT if out else None)
        finally:
            if out:
                out.close()
        if code == 3:
            fails = 0
        elif code == 0:
            sys.exit(0)
        else:
            fails = fails + 1 if time.time() - t0 < 60 else 1
            if fails > 5:
                sys.exit(code)
        time.sleep(1 if code == 3 else min(30, 2 * fails))


_serving = not any((a.detect, a.enroll, a.peer_invite, a.peer_add, a.peer_list, a.peer_remove,
                    a.signing_key, a.identity, a.version, a.release, a.rollback, a.pause_updates,
                    a.resume_updates, a.apply_update))
if sys.platform == "win32" and _serving and not os.environ.get("HUB_SUPERVISED") \
        and not os.environ.get("HUB_NO_RESTART") and not os.environ.get("HUB_NO_SUPERVISOR"):
    _supervise()


def _guard_files(root: Path):
    """The same code files as federation.code_files(), without importing federation."""
    for f in sorted(root.glob("*.py")):
        yield f
    yield root / "README.md"
    for f in sorted((root / "site").rglob("*")):
        if f.is_file() and f.name not in ("data.json", "data.json.tmp") and "__pycache__" not in f.parts:
            yield f
    for f in sorted((root / "core").rglob("*")):
        parts = f.relative_to(root).parts
        if f.is_file() and "__pycache__" not in parts and (
                parts[-1].endswith(".py") or (len(parts) > 3 and parts[1] == "modules" and not parts[-1].endswith(".pyc"))):
            yield f


def _emergency_restore(exc) -> bool:
    """If the freshly applied update does not even import, restore app.prev after 3 attempts.
    Does not import core: it has to work even if the new package is broken."""
    if os.environ.get("HUB_NO_RESTART"):
        return False
    app = Path(__file__).resolve().parents[1]
    data = Path(os.environ.get("HUB_CORE_HOME") or Path.home() / ".hub-core")
    marker, prev = data / "update_pending.json", data / "app.prev"
    if not marker.exists() or not prev.is_dir():
        return False
    try:
        d = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d["tries"] = int(d.get("tries") or 0) + 1
    try:
        marker.write_text(json.dumps(d), encoding="utf-8")
        if d["tries"] <= 2:
            return False
        for f in _guard_files(app):
            try:
                f.unlink()
            except OSError:
                pass
        shutil.copytree(prev, app, dirs_exist_ok=True)
        marker.unlink(missing_ok=True)
        with (data / "update_failed.log").open("a", encoding="utf-8") as f:
            f.write(f"{__import__('time').ctime()} boot failed ({exc!r}); restored {d.get('prev')}\n")
        os.chdir(app)
        return True
    except Exception:
        return False


try:
    from . import server  # noqa: E402
except Exception as _boot_error:
    if _emergency_restore(_boot_error):
        if os.environ.get("HUB_SUPERVISED"):
            os._exit(3)  # the supervisor relaunches the restored code (execv would exit with 0)
        os.execv(sys.executable, [sys.executable, "-m", "core", *sys.argv[1:]])
    raise
from .detect import detect  # noqa: E402


def _conf():
    return server.load_conf()


def _own_domain():
    return a.domain or _conf().get("domain") or "local"


def _identity():
    from .identity import Identity
    ident = Identity(server.DATA)
    ident.name = _conf().get("name") or a.name or detect()["hostname"]
    return ident


def _load_invite(raw):
    p = Path(raw)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(raw)


if a.detect:
    c = server.Core()
    print(json.dumps({"platform": detect(), "adapters": [x.name for x in c.adapters], "caps": c.caps()},
                     indent=1, ensure_ascii=False))
elif a.enroll:
    server.enroll(a.enroll)
elif a.peer_invite:
    from .peering import make_invite
    ident = _identity()
    url = a.url or _conf().get("url")
    if not url:
        ap.error("missing --url https://<your-hub>/hub/ (or set \"url\" in config.json)")
    print(json.dumps(make_invite(_own_domain(), ident.name, url, ident.pub_b64, ident.sign), indent=1,
                     ensure_ascii=False))
elif a.peer_add:
    from .peering import Peering
    p = Peering(server.DATA)
    rec = p.add(_load_invite(a.peer_add), _own_domain())
    print(json.dumps(rec, indent=1, ensure_ascii=False))
elif a.peer_list:
    from .peering import Peering
    print(json.dumps(Peering(server.DATA).list(), indent=1, ensure_ascii=False))
elif a.peer_remove:
    from .peering import Peering
    print("removed" if Peering(server.DATA).remove(a.peer_remove) else "not paired")
elif a.signing_key:
    import secrets
    from . import crypto
    from .identity import b64
    path = server.DATA / "release_key.json"
    if path.exists():
        d = json.loads(path.read_text(encoding="utf-8"))
    else:
        seed = secrets.token_bytes(32)
        d = {"seed": b64(seed), "pub": b64(crypto.publickey(seed)), "created": __import__("time").time()}
        path.write_text(json.dumps(d), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    print(d["pub"])
    print(f"private key: {path} - back it up offline; whoever holds it can push code to every hub "
          "of this mesh (docs/RELEASES.md)", file=sys.stderr)
elif a.identity:
    print(_identity().pub_b64)
elif a.version:
    from . import __version__
    print(f"Shoal {__version__}, code {server.federation.code_version(server.APP)}")
elif a.release or a.rollback or a.pause_updates or a.resume_updates or a.apply_update:
    import urllib.error
    import urllib.request
    port = a.port or int(os.environ.get("HUB_PORT") or 8790)
    path, body = ("/api/hub/apply", {}) if a.apply_update else ("/api/hub/rollback", {}) if a.rollback else ("/api/hub/updates", {"paused": True}) \
        if a.pause_updates else ("/api/hub/updates", {"paused": False}) if a.resume_updates else ("/api/hub/release", None)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/hub{path}", method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(json.dumps(json.load(r), indent=1))
    except urllib.error.HTTPError as e:
        sys.exit(json.load(e).get("error") or f"HTTP {e.code}")
    except OSError as e:
        sys.exit(f"no hub is answering on 127.0.0.1:{port} ({e})")
else:
    server.serve(a.bind or server.BIND)
