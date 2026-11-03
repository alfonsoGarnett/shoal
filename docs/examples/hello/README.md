# Hello (example module)

A greeting you set in Control › Modules and a counter you bump from its panel. It exists to be
copied: it shows the manifest, the settings form, an API under `/api/modules/hello/`, a panel with
its own strings in two languages, and a top-bar entry.

Try it: copy this folder to `core/modules/hello/` on your first hub, apply the update
(`python -m core --apply-update`), and enable it in Control › Modules.

Needs nothing beyond the hub. The count lives in memory and starts again from zero on restart.
