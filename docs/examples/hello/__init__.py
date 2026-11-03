"""Example module: a greeting and a counter you can bump from its panel.

Copy this folder to core/modules/<your_id>/ on the hub that signs your releases, rename the id in
module.json, the routes below and the paths in site/, and restart that hub (or wait for the next
release). See docs/MODULES.md.
"""
from __future__ import annotations

from .. import Module


class Impl(Module):
    # id, name, version and capabilities come from module.json

    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.count = 0

    def probe(self) -> bool:
        return True  # nothing to check: enabled means ready

    def get(self, path, query):
        if path == "/api/modules/hello/state":
            return {"greeting": self.config.get("greeting") or "Hello", "count": self.count,
                    "hub": self.ctx.get("name")}
        return None

    def post(self, path, body, query, handler):
        if path == "/api/modules/hello/bump":
            self.count += 1
            return {"count": self.count}
        return None
