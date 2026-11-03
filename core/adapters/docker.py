"""Docker containers as services with a switch (start/stop) and their published ports as routes."""
from __future__ import annotations

import json
import re

from ..detect import has, run, run_cached
from . import Adapter


class Impl(Adapter):
    name = "docker"
    caps = ["containers"]

    def probe(self):
        return has("docker") and run(["docker", "info", "--format", "{{.ServerVersion}}"], 10)[0] == 0

    def containers(self):
        code, out, _ = run_cached(["docker", "ps", "-a", "--format", "{{json .}}"], 15)
        items = []
        for line in out.splitlines() if code == 0 else []:
            try:
                c = json.loads(line)
            except ValueError:
                continue
            ports = sorted({int(m) for m in re.findall(r":(\d+)->", c.get("Ports", ""))})
            items.append({"id": "docker-" + c["Names"], "name": c["Names"], "image": c.get("Image"),
                          "on": c.get("State") == "running", "status": c.get("Status"), "ports": ports})
        return items

    def services(self):
        return {c["id"]: {"on": c["on"], "ports": c["ports"], "pids": [], "busy": None, "controllable": True,
                          "kind": "container", "name": c["name"], "image": c["image"], "status": c["status"]}
                for c in self.containers()}

    def routes(self, base_host):
        return [{"port": p, "path": "/", "name": c["name"], "about": f"Container {c['image']} ({c['status']})",
                 "icon": "ph-cube", "control": True, "id": c["id"], "up_hint": c["on"]}
                for c in self.containers() for p in c["ports"][:1]]

    def post(self, path, body, query, handler):
        m = re.fullmatch(r"/api/services/(docker-[\w.-]+)/(start|stop)", path)
        if m:
            code, out, err = run(["docker", m[2], m[1][len("docker-"):]], 60)
            return {"ok": code == 0, "output": (out + err).strip()}
