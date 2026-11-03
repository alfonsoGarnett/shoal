# Modules

A module adds a feature to Shoal without touching the core: a panel, an API, settings, push
notifications. The core knows about hubs, devices, the mesh, files and updates; it should not know
what a hotend is or which AI provider you use. OctoPrint and the Architect are both modules, and
they are the best examples to copy.

## Installing a module

A module is one folder. To add one:

1. Read its code. A module runs inside the hub with the hub's full rights (see
   [Trust](#trust) below), so treat it like any program you run as yourself.
2. Copy the folder to `core/modules/<id>/` on your **first hub**, the one that signs your updates.
   The folder name is the module id.
3. Wait two minutes or run `python -m core --apply-update`. The hub restarts with the module, signs
   the new code and sends it to every other hub with the next update.
4. Turn it on in **Control › Modules** on each hub where you want it. Modules start disabled.

To remove it, delete the folder on the first hub; the next update removes it everywhere.

## Writing one

Start from [`docs/examples/hello/`](examples/hello/): a greeting, a counter, a panel and two
languages, about a hundred lines in all. Copy it to `core/modules/<your_id>/`, rename `hello` everywhere,
and build from there.

```
core/modules/<id>/
  __init__.py      the Impl class (required)
  module.json      the manifest (required)
  ...              any other Python the module needs (import it relatively: from .client import X)
  i18n/en.json     its interface strings, merged into the hub's catalog
  i18n/es.json
  site/            what the browser may load, served at /hub/modules/<id>/
    panel.html     its page; starts with <base href="../../">
    panel.js
    panel.css
```

- The id is lowercase letters, digits and `_`, starting with a letter (it is also the Python
  package name). It must match `"id"` in `module.json`.
- Only `site/` is public. The module's Python, manifest and strings are not served.
- `<base href="../../">` in the panel lets it use the hub's shared files (`hub.css`, `i18n.js`,
  `fleet.js`, `palette.js`…) and its APIs exactly like a core page. Copy the `<head>` and the script
  tags at the end of `docs/examples/hello/site/panel.html`.
- Everything in the folder is part of the signed release, so it reaches every hub and is covered by
  your signature like the core.

(The older layout, with `module.json`, `i18n/` and the panel under `site/modules/<id>/` and only the
Python in `core/modules/<id>/`, still works.)

## The manifest

```json
{
  "id": "hello",
  "name": "Hello",
  "version": "1.0.0",
  "api": 1,
  "description": "Example module: a greeting and a counter.",
  "capabilities": ["hello"],
  "permissions": [],
  "panel": "modules/hello/panel.html",
  "nav": { "icon": "ph-hand-waving", "label": "module.hello.nav" },
  "aliases": [],
  "events": {
    "hello_bumped": { "label": "Counter bumped", "about": "Every time someone counts one more.", "default": false }
  },
  "config": {
    "enabled": { "type": "bool", "default": false, "label": "module.hello.enabled" },
    "greeting": { "type": "str", "default": "Hello", "max_length": 60, "label": "module.hello.greeting" }
  }
}
```

`module.json` is the only place that says who the module is: the hub reads `id`, `name`, `version`
and `capabilities` from it, so the Python class does not repeat them.

- `api`: the module API it was written for (today `1`). A hub refuses a module that needs a newer one.
- `capabilities`: added to the hub's `api/caps` while the module runs, so pages can show or hide
  parts with `data-cap`.
- `panel`: its page, relative to `/hub/`. Control › Modules and the command palette (⌘K) link to it
  while the module is enabled.
- `nav`: optional. While enabled, the module gets an entry in the top bar of every page (a
  [Phosphor](https://phosphoricons.com/) icon and an i18n key). Without it, its panel marks Modules
  as the current page.
- `aliases`: old addresses of its panel; the hub answers them with a redirect.
- `events`: push notifications it may send (`notify(kind, title, body, url)` from `self.ctx`). Every
  hub reads every manifest, so the coordinator can deliver them even if the module runs elsewhere.
  The Notifications page lists them only while the module is enabled on some hub.
- `config`: its settings, which become a form in Control › Modules. Types: `bool`, `str`, `url`,
  `secret`, `int` (with `min`/`max`), `select` (with `options`). `str` takes `max_length`. A `url`
  must be `http(s)`. A `secret` is never sent back to the browser: the form shows "configured" and
  an empty field keeps the stored value.

## The class

```python
from .. import Module


class Impl(Module):
    def probe(self) -> bool:            # called when enabled: is the configuration enough to run?
        return bool(self.config.get("greeting"))

    def start(self):                    # start threads, connections...
        self.active = True

    def stop(self):                     # ...and stop them. Called on disable, reconfigure, shutdown.
        self.active = False

    def get(self, path, query):         # GET /api/modules/<id>/...; return None if not yours
        if path == "/api/modules/hello/state":
            return {"greeting": self.config.get("greeting")}
        return None

    def post(self, path, body, query, handler):
        if path == "/api/modules/hello/bump":
            return {"ok": True}
        return None

    def status(self):                   # shown in Control › Modules
        return {"ok": self.active, "state": "ready" if self.active else "disabled", "detail": ""}
```

What a route may return: a dict (sent as JSON), `("error", code, message)`, `("bytes", data,
content_type)` or `("empty",)`. Exceptions in a module are caught and shown as its state; they do
not take the hub down.

Useful things in `self.ctx`: `name` (this hub), `data` (its data folder; keep files under
`data / "modules"`), `notify`, and `config` (the hub's `config.json`).

Optional hooks:

- `legacy_config(ctx) -> dict | None` (static): settings to adopt on the very first load, when the
  module has no saved configuration yet.
- `blocks_stop(control) -> str | None`: refuse to let a service on this hub stop, with the reason
  shown to the user (OctoPrint refuses while a print runs).

## Routes and settings

- The module's API is `/api/modules/<id>/...`, behind the same login as everything else. A module
  must never open a public route of its own.
- `GET /api/modules` lists modules; `GET/POST /api/modules/<id>/config` reads and writes settings.
- Settings are stored in `<data>/modules/<id>.json` (mode `0600` where the OS supports it). Defaults
  come from the manifest; invalid values are refused; the module is reloaded after a change.

## Trust

A module is ordinary Python running inside the hub process. It can read the hub's data, call other
hubs as this hub, and run commands. There is no sandbox, and a permission list in the manifest would
not change that. So:

- Modules are loaded only from `core/modules/`, which is part of your signed code. The hub never
  loads code from its data folder, from a download, or from anything another hub or a browser can
  write.
- A module reaches your other hubs only because your first hub signed it. Nobody else can push one
  to your mesh.
- Install modules you have read, from people you trust, the same as any script you would run.
- `permissions` in the manifest are a description for the reader (`network`, `fleet-read`…), not
  an enforcement.

## Checklist before you share a module

- [ ] `id` in the manifest matches the folder; `api: 1`; disabled by default.
- [ ] Settings with safe defaults; secrets as `secret`.
- [ ] `probe`, `start`, `stop`, `status`; threads stop on `stop()`.
- [ ] Routes only under `/api/modules/<id>/`.
- [ ] Strings in its own `i18n/en.json` and `i18n/es.json`, never in the core catalogs.
- [ ] No secrets in logs or API answers.
- [ ] No new required dependency; an optional one must fail gracefully.
- [ ] A README in the folder: what it does, what it needs, its limits.

## What not to do

- Do not load code from anywhere outside `core/modules/`.
- Do not install dependencies at runtime.
- Do not write outside `<data>/modules/` without a very good reason.
- Do not give a module a public HTTP route.
- Do not let an AI module call tools that change anything.
