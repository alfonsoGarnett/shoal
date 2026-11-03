"""Hub federation: signed code, canary and rollback.

- Any hub publishes the fingerprint of its code (`/api/hub/version`) and the hub package
  (`/dist/hub.zip`), so a new machine installs from whichever hub is on.
- `RELEASE.json` stores `{version, stamp, seq, pub?, sig?}`. `version` is the fingerprint; `seq`
  goes up by one with each change (it does not depend on anyone's clock); `stamp` is kept for
  compatibility with old hubs. If there is an author key, the release is signed with ed25519:
  a local edit without a signature stays on that machine and is not propagated.
- Before applying a package a snapshot of the previous code is saved (`state/app.prev`)
  and `update_pending.json` is written. If the new hub does not survive startup (3 attempts), the
  mesh restores the snapshot, marks that version as bad and restarts.
- `apply_bundle` preserves what is specific to the machine (.env, state/, services.json) and deletes the code
  files that the package no longer brings.

The fingerprint covers *.py in the root, README, site/ and core/*.py; it does not include
data.json, RELEASE.json or __pycache__.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from core import crypto

KEEP = {".env", "state", "services.json"}       # specific to each machine: never overwritten
SKIP_SITE = {"data.json", "data.json.tmp"}
NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CACHE_S = float(os.environ.get("HUB_CODE_CACHE", "5"))  # seconds of fingerprint cache (avoids re-reading site/)
WRITE = os.environ.get("HUB_RELEASE_WRITE", "1") != "0"  # tests/read-only installs must not rewrite RELEASE.json


def code_files(root: Path):
    # The fingerprint must not depend on the OS: Windows sorts paths ignoring case and POSIX does
    # not, so "LICENSE.txt" and "addon-fit.js" came out in different orders and Linux hubs
    # rejected a release signed on Windows. Always sort the POSIX way (case-sensitive, by part).
    posix = lambda f: f.relative_to(root).parts  # noqa: E731
    for f in sorted(root.glob("*.py"), key=posix):
        yield f, f.name
    yield root / "README.md", "README.md"
    for f in sorted((root / "site").rglob("*"), key=posix):
        if f.is_file() and f.name not in SKIP_SITE and "__pycache__" not in f.parts:
            yield f, f.relative_to(root).as_posix()
    for f in sorted((root / "core").rglob("*"), key=posix):  # universal core (Linux, Pi, macOS, Android)
        if f.is_file() and "__pycache__" not in f.parts and _core_file(f.relative_to(root).parts):
            yield f, f.relative_to(root).as_posix()


def _core_file(parts) -> bool:
    """Python anywhere in core/, plus every file inside a module folder (core/modules/<id>/...):
    a one-folder module brings its manifest, panel and strings, and they must be signed and
    shipped like its code. A tree with only .py under core/ fingerprints exactly as before."""
    return parts[-1].endswith(".py") or (len(parts) > 3 and parts[1] == "modules" and not parts[-1].endswith(".pyc"))


_HUELLA = {"root": None, "t": 0.0, "v": ""}


def code_version(root: Path) -> str:
    root = Path(root).resolve()
    if _HUELLA["root"] == str(root) and time.time() - _HUELLA["t"] < CACHE_S and _HUELLA["v"]:
        return _HUELLA["v"]
    h = hashlib.sha256()
    for f, rel in code_files(root):
        if f.exists():
            h.update(rel.encode())
            h.update(f.read_bytes())
    v = h.hexdigest()[:16]
    _HUELLA.update(root=str(root), t=time.time(), v=v)
    return v


def forget_version():
    """For tests: invalidates the fingerprint cache."""
    _HUELLA.update(root=None, t=0.0, v="")


def release_message(rel: dict) -> bytes:
    """Canonical message that is signed (stable across Python versions and platforms)."""
    fields = {"version": str(rel.get("version") or ""), "stamp": round(float(rel.get("stamp") or 0), 3),
              "seq": int(rel.get("seq") or 0)}
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


def check_release(rel: dict) -> tuple[bool, str]:
    """Checks a release's signature. Returns (ok, public_key_b64 or reason)."""
    sig, pub = rel.get("sig"), rel.get("pub")
    if not sig or not pub:
        return False, ""
    try:
        from core.identity import ub64
        if crypto.verify(ub64(sig), release_message(rel), ub64(pub)):
            return True, pub
    except Exception:
        pass
    return False, "invalid signature"


def release(root: Path, key=None) -> dict:
    """{version, stamp, seq, pub?, sig?}. `key` = (seed, pub) of the author, if this hub can sign."""
    root = Path(root)
    v, p = code_version(root), root / "RELEASE.json"
    try:
        r = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        r = None
    if r and r.get("files"):  # just applied by an old updater (without prune): remove orphans
        if WRITE:
            listed = set(r["files"])
            for f, rel in list(code_files(root)):
                if rel not in listed and f.exists():
                    f.unlink()
            forget_version()
            v = code_version(root)
        r = {k: r.get(k) for k in ("version", "stamp", "seq", "pub", "sig") if r.get(k) is not None}
        if WRITE:
            p.write_text(json.dumps(r), encoding="utf-8")
    if r is None or r.get("version") != v:
        old = r or {}
        # a new version invalidates the previous signature: it covered the old message
        r = {"version": v, "stamp": (0 if not old else time.time()), "seq": int(old.get("seq") or 0) + 1}
    elif not r.get("seq"):
        # old updaters left version+stamp without seq; do not break a legacy signature
        r = dict(r)
        r["seq"] = 0 if r.get("sig") else 1
    # sign (or re-sign if the version/sequence or the key changed)
    if key:
        from core.identity import b64
        seed, pub = key
        need = not r.get("sig") or r.get("pub") != b64(pub)
        if not need:
            ok, _ = check_release(r)
            need = not ok
        if need:
            r["pub"], r["sig"] = b64(pub), b64(crypto.sign(release_message(r), seed, pub))
    try:
        on_disk = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        on_disk = None
    if WRITE and on_disk != r:
        p.write_text(json.dumps(r), encoding="utf-8")
    return r


def bundle(root: Path) -> bytes:
    buf = io.BytesIO()
    files = [rel for f, rel in code_files(root) if f.exists()]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f, rel in code_files(root):
            if f.exists():
                z.write(f, rel)
        z.writestr("VERSION", code_version(root))
        z.writestr("RELEASE.json", json.dumps({**release(root), "files": files}))
    return buf.getvalue()


def verify_bundle(data: bytes, expected: dict, keys=None) -> str:
    """Checks that the zip is exactly the announced release: same signature and same code fingerprint."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if len(names) != len(set(names)):
            raise ValueError("bundle repeats a file name")
        for n in names:
            p = Path(n)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError("bundle contains an unsafe path")
        try:
            got = json.loads(z.read("RELEASE.json"))
        except (KeyError, ValueError) as e:
            raise ValueError(f"bundle has no valid RELEASE.json ({e})")
        if got.get("version") != expected.get("version"):
            raise ValueError("bundle does not match the announced version")
        if (got.get("pub"), got.get("sig")) != (expected.get("pub"), expected.get("sig")):
            raise ValueError("bundle signature does not match the announced signature")
        if expected.get("pub"):
            ok, pub = check_release(got)
            if not ok or (keys and pub not in keys):
                raise ValueError("bundle signature does not verify")
        with tempfile.TemporaryDirectory() as td:
            z.extractall(td)
            for py in Path(td).rglob("*.py"):
                if "__pycache__" in py.parts:
                    continue
                try:
                    compile(py.read_text(encoding="utf-8"), str(py), "exec")
                except (OSError, SyntaxError) as e:
                    raise ValueError(f"bundle does not compile: {e}")
            # Only signed files may travel: anything else (a __pycache__/*.pyc, a native .so/.pyd next to
            # a module, which Python loads before the .py) would run without being covered by the signature.
            extra = sorted(n for n in names if not n.endswith("/") and n not in _signed_names(Path(td)))
            if extra:
                raise ValueError(f"bundle carries files outside the signed fingerprint: {', '.join(extra[:3])}")
            forget_version()
            calc = code_version(Path(td))
            forget_version()
    if calc != expected.get("version"):
        raise ValueError("bundle content does not match the signed fingerprint")
    return calc


def _signed_names(root: Path) -> set:
    return {rel for f, rel in code_files(root) if f.exists()} | {"VERSION", "RELEASE.json"}


def apply_bundle(root: Path, data: bytes, prune: bool = True):
    """Extracts the package over the current code without touching what is specific to the machine. With prune, it deletes
    the code files that the package no longer brings (otherwise the fingerprint would never match)."""
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        if prune and "RELEASE.json" in names:
            for f, rel in list(code_files(root)):
                if rel not in names and f.exists() and rel.split("/")[0] not in KEEP:
                    f.unlink()
        z.extractall(tmp)
        signed = _signed_names(Path(tmp))
        for f in [f for f in Path(tmp).rglob("*") if f.is_file()]:  # never copy what the signature does not cover
            if f.relative_to(tmp).as_posix() not in signed:
                f.unlink()
        for item in Path(tmp).iterdir():
            if item.name in KEEP or item.name == "VERSION":
                continue
            dest = root / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
    forget_version()


def snapshot_app(root: Path, dest: Path) -> int:
    """Saves the current code in `dest` (state/app.prev) before applying an update."""
    root, dest = Path(root), Path(dest)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for f, rel in code_files(root):
        if not f.exists():
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, out)
        n += 1
    rp = root / "RELEASE.json"
    if rp.exists():
        shutil.copy2(rp, dest / "RELEASE.json")
    return n


def restore_app(root: Path, dest: Path) -> bool:
    """Restores the snapshot `dest` over the code (rollback)."""
    root, dest = Path(root), Path(dest)
    if not (dest / "core").exists():
        return False
    for f, rel in list(code_files(root)):
        try:
            f.unlink()
        except OSError:
            pass
    for item in dest.rglob("*"):
        if not item.is_file():
            continue
        rel = item.relative_to(dest)
        out = root / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, out)
    for pyc in (root / "core").rglob("__pycache__"):
        shutil.rmtree(pyc, ignore_errors=True)
    forget_version()
    return True


def republish(root: Path, after_seq: int) -> dict:
    """After a rollback: the restored code becomes a new release with a sequence above the one
    being abandoned (its old signature does not cover the new sequence, so release() signs it again
    on the author hub). Without that, no hub would ever move back down to it."""
    root = Path(root)
    forget_version()
    r = {"version": code_version(root), "stamp": time.time(), "seq": int(after_seq or 0) + 1}
    (root / "RELEASE.json").write_text(json.dumps(r), encoding="utf-8")
    return r


def restart_hub():
    """Restarts the hub through its scheduled task (this process exits; the task relaunches it)."""
    if os.environ.get("HUB_SUPERVISED"):
        os._exit(3)  # the supervisor in core/__main__.py relaunches it inside the same task
    # "Shoal" since the rename; hubs installed before it still run under the old task name. Only a
    # task that runs from this hub's folder counts, so another copy on the same PC is never started.
    ps = ("Start-Sleep 5; foreach ($t in 'Shoal', 'TailscaleHub') { $k = Get-ScheduledTask -TaskName $t "
          "-ErrorAction SilentlyContinue; if ($k -and $k.Actions.WorkingDirectory -eq $env:HUB_TASK_DIR) "
          "{ Start-ScheduledTask -TaskName $t; break } }")
    argv = ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps]
    # The helper must outlive this process: detach it, and break away from the task's job if possible.
    for flags in (NO_WINDOW | DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB,
                  NO_WINDOW | DETACHED_PROCESS):
        try:
            subprocess.Popen(argv, creationflags=flags, env={**os.environ, "HUB_TASK_DIR": os.getcwd()})
            break
        except OSError:
            continue
    # Exit non-zero as well: Task Scheduler's RestartOnFailure is the guaranteed fallback
    # if the helper does not survive or Start-ScheduledTask is ignored while the task is finishing.
    os._exit(3)


if __name__ == "__main__":
    print(code_version(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent))
