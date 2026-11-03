"""Build the public tree of Shoal from this working copy and refuse to ship anything private.

    python tools/export_public.py OUT_DIR [--home STATE_DIR] [--allow FILE:TERM ...]

1. The file list is what git would publish: tracked plus untracked files that .gitignore does not
   exclude (so state/, .env, services.json, RELEASE.json, HANDOFF.md, CLAUDE.md never leave).
2. Every text file is scanned for:
   - the identity of *this* installation, read from its own state: tailnet domain, hub and device
     names, DNS names, Tailscale IPs, owner logins, the git author's e-mail;
   - generic secrets: private key blocks, signing-key seeds, API-key-shaped strings, Tailscale IPs,
     absolute home paths.
3. Nothing is copied if anything is found; the report lists file:line for each hit.

The output is a plain folder: start a fresh repository there (no history from this one), e.g.
`git init && git add -A && git commit -m "Shoal 0.1.0-alpha"`. Nothing is pushed anywhere.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = {".py", ".js", ".json", ".md", ".html", ".css", ".txt", ".toml", ".yml", ".yaml", ".sh", ".ps1",
        ".cfg", ".ini", ".example", ".webmanifest", ".svg", ""}
GENERIC = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key block"),
    (re.compile(r'"seed"\s*:\s*"[A-Za-z0-9_\-]{20,}'), "signing key seed"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{20,}"), "API key"),
    (re.compile(r"\btskey-[A-Za-z0-9\-]{10,}"), "Tailscale auth key"),
    (re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"), "Tailscale IP"),
    (re.compile(r"[A-Za-z]:\\\\?(?:Users|Proyectos|Projects|home)\\\\?[A-Za-z0-9_.-]+|/home/[a-z][a-z0-9_-]+/|/Users/[A-Za-z][A-Za-z0-9_-]+/"), "home path"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "e-mail address"),
]
GENERIC_SERVICE_NAMES = {"octoprint", "docker", "tailscale", "hub", "shoal", "server", "service", "grafana",
                         "jellyfin", "plex", "frigate", "homeassistant", "nextcloud", "portainer", "pihole",
                         "camera", "printer", "terminal", "files", "index", "home"}
# Placeholders and project addresses that are fine to publish.
OK_EMAILS = re.compile(r"@(?:example\.(?:com|org|net)|users\.noreply\.github\.com|anthropic\.com)$|^(?:git@github\.com)$")
OK_IPS = {"100.64.0.1", "100.100.100.100", "100.101.102.103", "100.64.0.0",
          # made-up addresses of the federation and enrollment tests
          "100.64.0.2", "100.64.0.3", "100.64.0.5", "100.64.0.7", "100.64.0.9", "100.64.7.7", "100.64.9.9",
          "100.100.1.2"}
OK_PATHS = re.compile(r"^/home/(?:pi|user|you|USER)/$")


def publishable(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                         capture_output=True, check=True).stdout.decode("utf-8")
    return sorted({p for p in out.split("\0") if p and (root / p).is_file()})


def private_terms(home: Path) -> set[str]:
    """Names this installation is known by, read from its own state (never hardcoded here)."""
    terms = set()

    def load(name):
        try:
            return json.loads((home / name).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
    conf = load("config.json")
    for k in ("domain", "name", "url", "self_dns", "self_ip"):
        if isinstance(conf.get(k), str):
            terms.add(conf[k])
    for name, e in (load("directory.json") or {}).items():
        terms.add(name)
        for k in ("dns", "ip", "url", "domain"):
            if isinstance(e, dict) and isinstance(e.get(k), str):
                terms.add(e[k])
    for svc_file in (home / "services.json", ROOT / "services.json"):
        try:
            svcs = json.loads(svc_file.read_text(encoding="utf-8-sig")) if svc_file.is_file() else {}
        except (OSError, ValueError):
            svcs = {}
        groups = [svcs.get("routes"), svcs.get("services")] if isinstance(svcs, dict) else []
        for group in groups:
            for v in (group or {}).values() if isinstance(group, dict) else ():
                name = v.get("name") if isinstance(v, dict) else None
                # a one-word service name of your own (a project, a camera) is private; generic
                # names and phrases ("This index") are not, and would only block the export
                if isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{3,}", name)                         and name.lower() not in GENERIC_SERVICE_NAMES:
                    terms.add(name)
    try:
        email = subprocess.run(["git", "-C", str(ROOT), "config", "user.email"], capture_output=True, text=True).stdout.strip()
        if email:
            terms.add(email)
    except OSError:
        pass
    out = set()
    for t in terms:
        t = t.strip().rstrip("/")
        for part in re.split(r"https?://|/", t):  # a URL contributes its host
            part = part.split(":")[0].strip()
            if len(part) >= 4 and part.lower() not in ("hub", "http", "https", "local", "localhost", "127.0.0.1"):
                out.add(part)
    # a DNS name also leaks its first label (the device name) and the tailnet suffix
    for t in list(out):
        if t.endswith(".ts.net"):
            out.add(t.split(".")[0])
            out.add(".".join(t.split(".")[-3:]))
    return {t for t in out if len(t) >= 4}


def scan(root: Path, files: list[str], terms: set[str], allow: set[tuple[str, str]]) -> list[str]:
    hits = []
    words = [(re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![a-z0-9])", re.I), t)
             for t in sorted(terms, key=len, reverse=True)]  # whole names, also inside CamelCase
    for rel in files:
        p = root / rel
        if p.suffix.lower() not in TEXT or p.stat().st_size > 2_000_000:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for rx, t in words:
                if rx.search(line) and (rel, t) not in allow:
                    hits.append(f"{rel}:{n}: private name '{t}'")
            for rx, what in GENERIC:
                for m in rx.finditer(line):
                    v = m.group(0)
                    if what == "e-mail address" and OK_EMAILS.search(v):
                        continue
                    if what == "Tailscale IP" and v in OK_IPS:
                        continue
                    if what == "home path" and OK_PATHS.match(v):
                        continue
                    if (rel, v) in allow or (rel, what) in allow:
                        continue
                    hits.append(f"{rel}:{n}: {what} '{v[:60]}'")
    return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", help="empty (or new) folder for the public tree")
    ap.add_argument("--home", default=None, help="this installation's state folder (to learn its private names)")
    ap.add_argument("--allow", nargs="*", default=[], help="FILE:TERM pairs that are known to be fine")
    ap.add_argument("--check", action="store_true", help="only scan, copy nothing")
    ap.add_argument("--root", default=str(ROOT), help="working copy to export (default: this one)")
    a = ap.parse_args()
    root = Path(a.root)
    files = publishable(root)
    a.home = a.home or str(root / "state")
    terms = private_terms(Path(a.home))
    allow = {tuple(x.split(":", 1)) for x in a.allow if ":" in x}
    hits = scan(root, files, terms, allow)
    print(f"{len(files)} files would be published; {len(terms)} private names known from {a.home}")
    if hits:
        print(f"\n{len(hits)} problem(s); nothing was exported:\n")
        print("\n".join(hits[:400]))
        sys.exit(1)
    if a.check:
        print("clean")
        return
    out = Path(a.out)
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} is not empty")
    for rel in files:
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, dst)
    print(f"exported to {out}")


if __name__ == "__main__":
    main()
