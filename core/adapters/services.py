"""Services with a switch on any device: turn local servers on/off and view their output.

They are declared in ~/.hub-core/config.json → "services": {
  "mi-web": {"name": "Mi web", "icon": "ph-globe", "about": "…", "route": {"port": 8080, "path": "/"},
             "control": {"cwd": "/home/pi/web", "cmd": ["python3", "-m", "http.server", "8080"], "env": {}, "ports": [8080]}},
  "nginx":  {"control": {"systemd": "nginx", "user": false, "ports": [80]}},
  "octo":   {"control": {"windows_service": "OctoPrint5000", "ports": [5000]}}
}
On = all "ports" accept connections on 127.0.0.1 (psutil is not needed).
Own processes: POSIX with a process group (killpg); Windows with taskkill /T.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..detect import has, run
from . import Adapter

WIN = sys.platform == "win32"
MAX_LOG = 512 * 1024


def port_open(port, timeout=0.4):
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


class Impl(Adapter):
    name = "services"
    caps = ["services"]

    def probe(self):
        self.logs = self.ctx["data"] / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.pids_file = self.ctx["data"] / "service-pids.json"
        self.busy = {}
        self.lock = threading.Lock()
        return bool(self.defs())

    def defs(self) -> dict:
        """Services from config.json and, on a migrated PC, those from the app's services.json (tailscale serve
        routes with "control"; their id is the route's id, e.g. "4100")."""
        out = {k: v for k, v in (self.ctx["config"].get("services") or {}).items() if v.get("control")}
        from ..server import APP
        try:
            routes = json.loads((APP / "services.json").read_text(encoding="utf-8-sig")).get("routes", {})
        except (OSError, ValueError):
            routes = {}
        for key, r in routes.items():
            if isinstance(r, dict) and r.get("control"):
                out[key.strip("/").replace("/", "-")] = {**r, "via_serve": True}
        return out

    # ---------- start hook (how the switch turns a service on) ----------
    # Viewing a hook is always allowed; saving one runs new code on this device, so it needs the
    # remote terminal of this hub to be enabled (the same switch and quorum as the terminal).
    @staticmethod
    def _kind(ctl):
        return "systemd" if "systemd" in ctl else "windows_service" if "windows_service" in ctl else "process"

    @staticmethod
    def _join(args):
        return subprocess.list2cmdline(args) if WIN else shlex.join(args)

    @staticmethod
    def _split(line):
        if not WIN:
            return shlex.split(line)
        out = []
        for tok in shlex.split(line, posix=False):  # keeps backslashes; drop the quotes it leaves on
            out.append(tok[1:-1] if len(tok) > 1 and tok[0] == tok[-1] == '"' else tok)
        return out

    def _editable(self):
        term = self.ctx.get("adapters_by_name", {}).get("terminal")
        try:
            return bool(term and term.state()["enabled"])
        except Exception:
            return False

    def hook(self, sid):
        s = self.defs()[sid]
        ctl = s["control"]
        kind = self._kind(ctl)
        jobs = ctl.get("cmds") or ([{"cmd": ctl["cmd"]}] if ctl.get("cmd") else [])
        lines = []
        for job in jobs:
            env = " ".join(f"{k}={self._join([str(v)])}" for k, v in (job.get("env") or {}).items())
            lines.append((env + " " if env else "") + self._join([str(a) for a in job.get("cmd") or []]))
        return {
            "id": sid, "kind": kind, "source": "services.json" if s.get("via_serve") else "config.json",
            "service": ctl.get("systemd") or ctl.get("windows_service") or "",
            "cwd": ctl.get("cwd") or "", "commands": "\n".join(lines),
            "env": "\n".join(f"{k}={v}" for k, v in (ctl.get("env") or {}).items()),
            "ports": ", ".join(str(p) for p in ctl.get("ports") or []),
            "editable": kind == "process" and self._editable(), "unlocked": self._editable(),
        }

    def save_hook(self, sid, body):
        if not self._editable():
            raise PermissionError("enable this hub's remote terminal to edit start hooks")
        s = self.defs()[sid]
        old = dict(s["control"])
        if self._kind(old) != "process":
            raise ValueError("this service is started by the system service manager; edit it there")
        cwd = str(body.get("cwd") or "").strip()
        if cwd and not Path(cwd).is_dir():
            raise ValueError(f"the folder does not exist: {cwd}")
        jobs = []
        for line in str(body.get("commands") or "").splitlines():
            if not line.strip():
                continue
            try:
                args = self._split(line.strip())
            except ValueError as e:
                raise ValueError(f"cannot read the command “{line.strip()}”: {e}")
            env = {}
            while args and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", args[0]):
                k, _, v = args.pop(0).partition("=")
                env[k] = v
            if not args:
                raise ValueError(f"the line “{line.strip()}” has no command")
            jobs.append({"cmd": args, **({"env": env} if env else {})})
        if not jobs:
            raise ValueError("write at least one command")
        env = {}
        for line in str(body.get("env") or "").splitlines():
            if not line.strip():
                continue
            k, sep, v = line.strip().partition("=")
            if not sep or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k.strip()):
                raise ValueError(f"variables go as NAME=value: “{line.strip()}”")
            env[k.strip()] = v
        try:
            ports = [int(p) for p in re.split(r"[\s,]+", str(body.get("ports") or "").strip()) if p]
        except ValueError:
            raise ValueError("ports are numbers separated by commas")
        if not ports or any(not 0 < p < 65536 for p in ports):
            raise ValueError("list the ports the service listens on (they tell the hub when it is on)")
        ctl = {k: v for k, v in old.items() if k not in ("cwd", "cmd", "cmds", "env", "ports")}
        if cwd:
            ctl["cwd"] = cwd
        if len(jobs) == 1 and not jobs[0].get("env"):
            ctl["cmd"] = jobs[0]["cmd"]
        else:
            ctl["cmds"] = jobs
        if env:
            ctl["env"] = env
        ctl["ports"] = ports
        if s.get("via_serve"):
            from ..server import APP
            path = APP / "services.json"
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
            key = next(k for k, r in doc["routes"].items()
                       if k.strip("/").replace("/", "-") == sid and isinstance(r, dict) and r.get("control"))
            doc["routes"][key]["control"] = ctl
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.replace(path)
        else:
            from ..server import save_conf
            conf = self.ctx["config"]
            conf["services"][sid]["control"] = ctl
            save_conf(conf)
        self.write_log(sid, f"\n[hub] {time.strftime('%Y-%m-%d %H:%M:%S')} start hook edited\n")
        return self.hook(sid)

    # ---------- state ----------
    def pids(self):
        try:
            return json.loads(self.pids_file.read_text())
        except (OSError, ValueError):
            return {}

    def save_pids(self, d):
        self.pids_file.write_text(json.dumps(d))

    def status(self, sid, ctl):
        ports = ctl.get("ports") or []
        on = bool(ports) and all(port_open(p) for p in ports)
        out = {"on": on, "ports": ports, "pids": [p for p in [self.pids().get(sid)] if p], "busy": self.busy.get(sid),
               "controllable": True, "kind": self._kind(ctl), "hook": True}
        if "systemd" in ctl:
            out["unit_state"] = run(self._systemctl(ctl) + ["is-active", ctl["systemd"]], 5)[1].strip()
        return out

    def all_status(self):
        return {sid: self.status(sid, s["control"]) for sid, s in self.defs().items()}

    def routes(self):
        """Routes for data.json: each service with its port, marked as controllable."""
        out = []
        for sid, s in self.defs().items():
            if s.get("via_serve"):
                continue  # it already appears as a tailscale serve route
            r = s.get("route") or {"port": (s["control"].get("ports") or [None])[0], "path": "/"}
            if r.get("port"):
                out.append({"id": sid, "port": r["port"], "path": r.get("path", "/"), "name": s.get("name", sid),
                            "about": s.get("about", ""), "icon": s.get("icon", "ph-gear")})
        return out

    # ---------- registry ----------
    def log_path(self, sid):
        return self.logs / f"{sid}.log"

    def write_log(self, sid, text):
        p = self.log_path(sid)
        if p.exists() and p.stat().st_size > MAX_LOG:
            p.replace(p.with_suffix(".old.log"))
        with p.open("a", encoding="utf-8", errors="replace") as f:
            f.write(text)

    def read_log(self, sid, offset):
        p = self.log_path(sid)
        if not p.exists():
            return {"offset": 0, "text": ""}
        size = p.stat().st_size
        offset = 0 if offset > size else offset
        if offset == 0:
            offset = max(0, size - 64 * 1024)
        with p.open("rb") as f:
            f.seek(offset)
            data = f.read()
        return {"offset": offset + len(data), "text": data.decode("utf-8", "replace")}

    # ---------- actions ----------
    @staticmethod
    def _systemctl(ctl):
        return ["systemctl"] + (["--user"] if ctl.get("user") else [])

    def _wait(self, sid, ctl, want, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.status(sid, ctl)["on"] == want:
                return True
            time.sleep(1)
        return False

    def start(self, sid, ctl, who):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.write_log(sid, f"\n[hub] {stamp} started by {who}\n")
        if "windows_service" in ctl:  # starting during STOP_PENDING fails (error 1056): wait
            t0 = time.time()
            while "PENDING" in run(["sc.exe", "query", ctl["windows_service"]], 10)[1] and time.time() - t0 < 60:
                time.sleep(1)
        if "systemd" in ctl or "windows_service" in ctl:
            cmd = self._systemctl(ctl) + ["start", ctl["systemd"]] if "systemd" in ctl else ["sc.exe", "start", ctl["windows_service"]]
            self.write_log(sid, f"$ {' '.join(cmd)}\n")
            code, out, err = run(cmd, 60)
            self.write_log(sid, (out + err).strip() + "\n")
        else:
            jobs = ctl.get("cmds") or [{"cmd": ctl["cmd"], "env": ctl.get("env")}]
            pids = self.pids()
            for job in jobs:
                env = {**os.environ, **(ctl.get("env") or {}), **(job.get("env") or {}), "NO_COLOR": "1", "FORCE_COLOR": "0"}
                self.write_log(sid, f"$ cd {ctl.get('cwd', '.')}\n$ {' '.join(job['cmd'])}\n")
                with self.log_path(sid).open("ab") as log:
                    kw = dict(cwd=ctl.get("cwd"), env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
                    if WIN:  # no window, own group and outside the scheduled task's job (if possible)
                        try:
                            p = subprocess.Popen(job["cmd"], creationflags=0x08000000 | 0x00000200 | 0x01000000, **kw)
                        except OSError:
                            p = subprocess.Popen(job["cmd"], creationflags=0x08000000 | 0x00000200, **kw)
                    else:
                        p = subprocess.Popen(job["cmd"], start_new_session=True, **kw)  # own group: it is shut down as a whole
                pids[sid] = p.pid
            self.save_pids(pids)
        ok = self._wait(sid, ctl, True, 120)
        self.write_log(sid, f"[hub] {'listening on ' + str(ctl.get('ports')) if ok else 'did not start listening'}\n")

    def _listener_pids(self, port):
        """PID listening on a port (to stop whatever the hub did not launch)."""
        if WIN:
            out = run(["netstat", "-ano", "-p", "TCP"], 10)[1]
            return {int(l.split()[-1]) for l in out.splitlines() if f":{port} " in l and "LISTEN" in l.upper()}
        if has("ss"):
            out = run(["ss", "-ltnpH", f"sport = :{port}"], 10)[1]
            return {int(x) for x in __import__("re").findall(r"pid=(\d+)", out)}
        if has("lsof"):
            out = run(["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], 10)[1]
            return {int(x) for x in out.split() if x.isdigit()}
        return set()

    def stop(self, sid, ctl, who):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.write_log(sid, f"\n[hub] {stamp} stopped by {who}\n")
        if "systemd" in ctl or "windows_service" in ctl:
            cmd = self._systemctl(ctl) + ["stop", ctl["systemd"]] if "systemd" in ctl else ["sc.exe", "stop", ctl["windows_service"]]
            self.write_log(sid, f"$ {' '.join(cmd)}\n")
            code, out, err = run(cmd, 60)
            self.write_log(sid, (out + err).strip() + "\n")
        else:
            pids = self.pids()
            targets = set()
            if pids.get(sid):
                targets.add(pids.pop(sid))
                self.save_pids(pids)
            for port in ctl.get("ports") or []:
                targets |= self._listener_pids(port)
            for pid in targets:
                if WIN:
                    code, out, err = run(["taskkill", "/T", "/F", "/PID", str(pid)], 20)
                    self.write_log(sid, f"$ taskkill /T /F /PID {pid}\n{(out + err).strip()}\n")
                    continue
                try:
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, signal.SIGTERM)
                    self.write_log(sid, f"$ kill -TERM -{pgid}\n")
                except (ProcessLookupError, PermissionError) as e:
                    self.write_log(sid, f"[hub] {pid}: {e}\n")
            if not WIN and not self._wait(sid, ctl, False, 8):
                for pid in targets:
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                        self.write_log(sid, f"$ kill -KILL -{pid}\n")
                    except (ProcessLookupError, PermissionError):
                        pass
        ok = self._wait(sid, ctl, False, 20)
        self.write_log(sid, f"[hub] {'stopped' if ok else 'still listening'}\n")

    # ---------- contract ----------
    def get(self, path, query):
        if path == "/api/services":
            return self.all_status()
        if path.startswith("/api/services/") and path.endswith("/log"):
            sid = path.split("/")[3]
            if sid in self.defs():
                return self.read_log(sid, int(query.get("offset", 0) or 0))
        if path.startswith("/api/services/") and path.endswith("/hook"):
            sid = path.split("/")[3]
            if sid in self.defs():
                return self.hook(sid)

    def post(self, path, body, query, handler):
        parts = path.split("/")
        if len(parts) == 5 and parts[1:3] == ["api", "services"] and parts[4] == "hook":
            if parts[3] not in self.defs():
                return None
            try:
                return self.save_hook(parts[3], body or {})
            except ValueError as e:
                return ("error", 400, str(e))
        if len(parts) == 5 and parts[1:3] == ["api", "services"] and parts[4] in ("start", "stop"):
            sid, action = parts[3], parts[4]
            defs = self.defs()
            if sid not in defs:
                return None  # maybe it belongs to another adapter (docker)
            with self.lock:
                if self.busy.get(sid):
                    return ("error", 409, "Another action is already in progress.")
                self.busy[sid] = "starting" if action == "start" else "stopping"
            ctl = defs[sid]["control"]
            who = handler.headers.get("Tailscale-User-Login") or handler.headers.get("X-Hub-User") or "local"
            reg = self.ctx.get("modules")
            reason = reg.stop_blocker(ctl) if action == "stop" and reg else None
            if reason:  # a module needs this service running right now
                self.busy.pop(sid, None)
                return ("error", 409, reason)

            def work():
                try:
                    (self.start if action == "start" else self.stop)(sid, ctl, who)
                finally:
                    self.busy.pop(sid, None)
            threading.Thread(target=work, daemon=True).start()
            return {**self.status(sid, ctl), "busy": self.busy.get(sid)}
