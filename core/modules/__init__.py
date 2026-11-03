"""Optional feature modules.

Adapters answer "what can this device do?" and are tied to local hardware or
platform capabilities. Modules answer "what optional product feature is
enabled?" and carry their own configuration, routes, panel and status.

A module is a folder in this package (core/modules/<id>/), found at startup: drop one in on the
hub that signs your releases and every hub gets it with the next release. Modules are only loaded
from here, inside the signed code, never from the data folder or any other writable place: a
module runs with the hub's full rights, so it is code you trust like the core. See docs/MODULES.md.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import threading
import time
from pathlib import Path

MODULE_API = 1
HERE = Path(__file__).resolve().parent          # core/modules
APP = HERE.parents[1]                           # the hub's code folder
ID_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")     # a module id is also its Python package name


def discover(root: Path = HERE) -> list:
    """Module ids: every core/modules/<id>/ with an __init__.py and a valid id, sorted."""
    return sorted(d.name for d in Path(root).iterdir()
                  if d.is_dir() and ID_RE.fullmatch(d.name) and (d / "__init__.py").is_file())


def module_file(mid: str, rel: str) -> Path | None:
    """A module's non-Python file, wherever the module keeps it: in its own folder
    (core/modules/<id>/<rel>, the one-folder layout) or under site/modules/<id>/<rel>."""
    for base in (HERE / mid, APP / "site" / "modules" / mid):
        f = (base / rel).resolve()
        if base.resolve() in f.parents and f.is_file():
            return f
    return None


def static_file(rel: str) -> Path | None:
    """What /modules/<id>/<file> serves for a one-folder module: core/modules/<id>/site/<file>.
    Only that site/ subfolder is public; the module's Python and config stay private."""
    parts = rel.split("/", 1)
    if len(parts) != 2 or not ID_RE.fullmatch(parts[0]):
        return None
    base = (HERE / parts[0] / "site").resolve()
    f = (base / parts[1]).resolve()
    return f if base in f.parents and f.is_file() else None


class Module:
    """Base class for modules. id, name, version and caps come from module.json (the registry sets
    them when it loads the module); a subclass does not repeat them."""

    id = ""
    name = ""
    version = ""
    api = MODULE_API
    caps: list = []
    manifest: dict = {}

    def __init__(self, ctx, config):
        self.ctx = ctx
        self.config = config or {}
        self.last_error = ""
        self.active = False
        self._lock = threading.Lock()

    def probe(self) -> bool:
        """Return True when the module has enough configuration to run."""
        return True

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def get(self, path: str, query: dict):
        return None

    def post(self, path: str, body: dict, query: dict, handler):
        return None

    def actions(self) -> dict:
        return {}

    def status(self) -> dict:
        return {"ok": bool(self.active), "state": "ready" if self.active else "disabled", "detail": ""}

    def public_config(self) -> dict:
        schema = self.manifest.get("config") or {}
        values = {}
        for key, spec in schema.items():
            value = self.config.get(key, spec.get("default"))
            if spec.get("type") == "secret":
                values[key] = {"configured": bool(value), "value": ""}
            else:
                values[key] = value
        return {"schema": schema, "values": values}


class ModuleRegistry:
    def __init__(self, ctx):
        self.ctx = ctx
        self.data = Path(ctx["data"]) / "modules"
        self.data.mkdir(parents=True, exist_ok=True)
        self.items: dict[str, Module] = {}
        self.failures: dict[str, dict] = {}
        self.ids: list = []

    def load(self):
        self.ids = discover()
        for mid in self.ids:
            self._load_one(mid)

    def _manifest(self, mid: str) -> dict:
        # In the module's own folder or under site/modules/<id>/; both are signed code.
        path = module_file(mid, "module.json")
        if not path:
            raise ValueError(f"module {mid} has no module.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("id") != mid:
            raise ValueError(f"module id mismatch in {path}")
        if int(data.get("api", 0)) > MODULE_API:
            raise ValueError(f"module {mid} requires a newer hub")
        return data

    def _defaults(self, manifest: dict) -> dict:
        out = {}
        for key, spec in (manifest.get("config") or {}).items():
            out[key] = spec.get("default")
        return out

    def _load_values(self, mid: str, manifest: dict, legacy=None) -> dict:
        path = self.data / f"{mid}.json"
        values = self._defaults(manifest)
        try:
            saved = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(saved, dict):
                values.update({k: v for k, v in saved.items() if k in values})
        except (OSError, ValueError):
            pass
        if not path.exists() and legacy:  # first run: the module may adopt an older configuration
            values.update(legacy(self.ctx) or {})
        return self._validate(mid, manifest, values, partial=False)

    def _validate(self, mid: str, manifest: dict, values: dict, partial=True) -> dict:
        schema = manifest.get("config") or {}
        out = {}
        for key, spec in schema.items():
            if key not in values:
                continue
            value = values[key]
            typ = spec.get("type", "str")
            if typ == "bool":
                value = bool(value)
            elif typ == "int":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{mid}.{key} must be an integer")
                if "min" in spec and value < int(spec["min"]):
                    raise ValueError(f"{mid}.{key} is too small")
                if "max" in spec and value > int(spec["max"]):
                    raise ValueError(f"{mid}.{key} is too large")
            elif typ == "select":
                if value not in (spec.get("options") or []):
                    raise ValueError(f"{mid}.{key} is not a valid option")
            else:
                value = "" if value is None else str(value)
                if len(value) > int(spec.get("max_length", 4096)):
                    raise ValueError(f"{mid}.{key} is too long")
                if typ == "url" and value:
                    if not value.startswith(("http://", "https://")):
                        raise ValueError(f"{mid}.{key} must be an http(s) URL")
                    if not partial and spec.get("required") and not value:
                        raise ValueError(f"{mid}.{key} is required")
            out[key] = value
        if not partial:
            defaults = self._defaults(manifest)
            defaults.update(out)
            out = defaults
            for key, spec in schema.items():
                if spec.get("required") and not out.get(key):
                    raise ValueError(f"{mid}.{key} is required")
        return out

    def _load_one(self, mid: str):
        old = self.items.pop(mid, None)
        if old:
            try:
                old.stop()
            except Exception:
                pass
        try:
            manifest = self._manifest(mid)
            mod = importlib.import_module(f"core.modules.{mid}")
            values = self._load_values(mid, manifest, getattr(mod.Impl, "legacy_config", None))
            impl = mod.Impl(self.ctx, values)
            impl.manifest = manifest
            # module.json is the one source of who the module is
            impl.id, impl.name = mid, manifest.get("name") or mid
            impl.version = str(manifest.get("version") or "")
            impl.caps = [str(c) for c in (manifest.get("capabilities") or [])]
            self.items[mid] = impl
            self.failures.pop(mid, None)
            if values.get("enabled"):
                try:
                    if impl.probe():
                        impl.start()
                    else:
                        impl.last_error = "configuration incomplete"
                except Exception as e:
                    impl.last_error = f"{type(e).__name__}: {e}"
        except Exception as e:
            self.failures[mid] = {"id": mid, "name": mid, "error": f"{type(e).__name__}: {e}"}

    def reload(self, mid: str):
        if mid not in self.ids:
            raise KeyError(mid)
        self._load_one(mid)

    def set_config(self, mid: str, values: dict) -> dict:
        impl = self.items.get(mid)
        if not impl:
            raise KeyError(mid)
        schema = impl.manifest.get("config") or {}
        merged = dict(impl.config)
        for key, value in (values or {}).items():
            if key not in schema:
                continue
            spec = schema[key]
            if spec.get("type") == "secret" and value in ("", None):
                continue
            merged[key] = value
        merged = self._validate(mid, impl.manifest, merged, partial=False)
        path = self.data / f"{mid}.json"
        path.write_text(json.dumps(merged, indent=1), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self.reload(mid)
        return self.status(mid)

    def active(self) -> list[Module]:
        return [m for m in self.items.values() if m.active]

    def caps(self) -> list:
        out = []
        for m in self.active():
            for cap in m.caps:
                if cap not in out:
                    out.append(cap)
        return out

    def get(self, path: str, query: dict):
        for m in self.active():
            r = m.get(path, query)
            if r is not None:
                return r
        return None

    def post(self, path: str, body: dict, query: dict, handler):
        for m in self.active():
            r = m.post(path, body, query, handler)
            if r is not None:
                return r
        return None

    def actions(self) -> dict:
        out = {}
        for m in self.active():
            try:
                out.update(m.actions() or {})
            except Exception:
                pass
        return out

    def status(self, mid: str) -> dict:
        if mid in self.failures:
            f = self.failures[mid]
            return {"id": mid, "name": f.get("name", mid), "version": "", "api": MODULE_API,
                    "description": "", "enabled": False, "ok": False, "state": "error",
                    "detail": f.get("error", "module failed to load"), "caps": [], "panel": "", "nav": None,
                    "config": {"schema": {}, "values": {}}}
        m = self.items.get(mid)
        if not m:
            raise KeyError(mid)
        try:
            st = m.status()
        except Exception as e:
            st = {"ok": False, "state": "error", "detail": f"{type(e).__name__}: {e}"}
        return {"id": mid, "name": m.name, "version": m.version, "api": m.api,
                "description": m.manifest.get("description", ""), "enabled": bool(m.config.get("enabled")),
                "permissions": list(m.manifest.get("permissions") or []),
                "panel": m.manifest.get("panel", ""), "nav": m.manifest.get("nav"), "caps": list(m.caps), **st,
                "config": m.public_config()}

    def alias(self, name: str) -> str | None:
        """Where an old page address of a module lives now (e.g. printer.html → its panel)."""
        for m in self.items.values():
            if name in (m.manifest.get("aliases") or []) and m.manifest.get("panel"):
                return m.manifest["panel"]
        return None

    def catalog(self, lang: str) -> dict:
        """UI strings contributed by every module (its i18n/<lang>.json)."""
        out = {}
        for mid in self.ids or discover():
            f = module_file(mid, f"i18n/{lang}.json")
            try:
                out.update(json.loads(f.read_text(encoding="utf-8")) if f else {})
            except (OSError, ValueError):
                pass
        return out

    def stop_blocker(self, control: dict) -> str | None:
        """A module may refuse to let a service stop (a print in progress, for example)."""
        for m in self.active():
            hook = getattr(m, "blocks_stop", None)
            reason = hook(control) if hook else None
            if reason:
                return reason
        return None

    def statuses(self) -> list[dict]:
        out = [self.status(mid) for mid in self.items]
        for mid, f in self.failures.items():
            if mid not in [x["id"] for x in out]:
                out.append({"id": mid, "name": f.get("name", mid), "version": "", "api": MODULE_API,
                            "description": "", "enabled": False, "ok": False, "state": "error",
                            "detail": f.get("error", "module failed to load"), "caps": [], "panel": "", "nav": None,
                            "config": {"schema": {}, "values": {}}})
        return out
