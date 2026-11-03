# Publishing checklist

## How the public repository is made

```bash
python tools/export_public.py ../shoal          # refuses if anything private is found
cd ../shoal && git init && git add -A && git commit -m "Shoal 0.1.0-alpha"
```

The export takes only what git would publish (tracked plus non-ignored files), so `state/`, `.env`,
`services.json`, `RELEASE.json`, `HANDOFF.md`, `CLAUDE.md` and `.dev/` never leave. The new
repository starts without this repository's history. Names: repo `alfonsoGarnett/shoal` on GitHub, PyPI package `shoal-hub`.

This is the gate between "working personal project" and "public repository". Do not skip the secret and security items.

## 1. Repository hygiene

- [x] `git` initialized with a baseline commit.
- [x] `.gitignore` excludes `state/`, `.env`, `services.json`, `site/data.json`, `RELEASE.json`, `HANDOFF.md`, `CLAUDE.md`, `.dev/`, logs, zips, keys, and caches.
- [x] Public tree built from a clean export, not from the production working directory.
- [x] No personal domains, IPs, hostnames, user names, node IDs, or absolute paths in README/docs (the export scan enforces it; re-check any screenshot by eye).
- [x] `services.example.json` and `config.example.json` use placeholders.
- [x] Legacy single-master files (`hub.py`, `nodes.py`, `printer.py`, `services.py`, `terminal.py`, `monitor.py`) removed; `shares.py`, `push.py`, `clipboard.py` and `federation.py` are used by the core.
- [x] `HANDOFF.md` and `CLAUDE.md` remain private.

## 2. Secrets

- [x] `state/release_key.json` is **not** in the repo or any zip.
- [x] `state/vapid_private.pem` is not in the repo.
- [x] OctoPrint/API keys are not in the repo.
- [x] `.env` is not tracked.
- [x] `site/data.json` (tailnet email/IPs) is not tracked.
- [ ] Rotate any key that may have been exposed in chat, screenshots, or temporary files.
- [x] `tools/export_public.py` scans the exported tree for this installation's private names and generic secrets (also in CI).
- [x] `gitleaks dir` on the exported tree: clean (`.gitleaks.toml` skips the minified vendored xterm.js, whose `FourKeyMap`/`TwoKeyMap` names look like keys).
- [x] Confirm `git ls-files` contains no `state/` or `.env`.

## 3. Legal and assets

- [x] `LICENSE` (MIT).
- [x] `NOTICE` for Tailscale trademark and third-party assets (Geist, Departure Mono, Phosphor, xterm.js).
- [x] Geist font license (SIL OFL 1.1) included in `site/vendor/fonts/`.
- [x] Phosphor icons license (MIT) included in `site/vendor/phosphor/`.
- [x] No Tailscale logo or official branding used.
- [x] "Not affiliated with Tailscale Inc." in both READMEs and `NOTICE`.
- [x] Review any copied code for attribution (vendored xterm.js, fonts and icons are listed in NOTICE).

## 4. Documentation

- [x] English `README.md` with tagline, screenshots/GIF, features, architecture, quick start, security, modules, limitations, license.
- [x] `README.es.md`.
- [x] `docs/ARCHITECTURE.md` (with diagrams), `SECURITY.md`, `RELEASES.md`, `I18N.md`, `MODULES.md`, `AI.md`, `ROADMAP.md`, `DEPLOYMENT.md`.
- [x] `CONTRIBUTING.md`.
- [x] Issue/PR templates.
- [x] Screenshots with fake data from `python tools/demo.py`; no real tailnet identity.
- [x] A demo GIF, recorded from `python tools/demo.py` (fake devices only).

## 5. Internationalization

- [x] All UI strings in `site/i18n/en.json` and `site/i18n/es.json` (no visible keys or `{placeholders}` on any page).
- [x] Key parity test passes.
- [x] English is the default; Spanish selectable.
- [x] Both languages reviewed at 390 px and 1440 px, in the three themes (Chrome via CDP, demo mesh).
- [x] No hardcoded Spanish in HTML/JS except catalogs (only the language's own name, "Español").
- [x] Server logs/errors are English (a unit test scans every string literal; Architect still translates old Spanish log lines).

## 6. Modules

- [x] OctoPrint extracted from core adapters into `core/modules/octoprint`.
- [x] Architect implemented as `core/modules/advisor`, read-only, disabled by default.
- [x] Manifest/config schema/validation tested.
- [x] Secrets masked in API responses.
- [x] Migration from legacy `config.json` `printer` block tested.
- [x] Module page renders both languages.
- [x] Modules load only from `core/modules/` in the signed code (one folder each, discovered at startup); never from the data folder or downloads.

## 7. Security review

- [x] Auth on every route, including static assets and module routes (every HTTP method passes the gate before routing).
- [x] `X-Hub-Auth` nonce replay protection (unit-tested).
- [x] Path traversal checks on static files, file APIs and blob ids (unit-tested).
- [x] SSRF review of the federated catalogue/proxy and Architect base URL (hub URLs limited to the tailnet, no redirects, keys only over https; see SECURITY.md).
- [x] Full-tree review before publishing: paired tailnets bound to the federated APIs, Origin checked with its port, update bundles limited to signed files, mesh data escaped in the UI, the hub's own folders behind the terminal switch (all unit-tested).
- [x] Funnel gate returns 404 for everything except share links and the login page; login body capped at 4 KB.
- [x] Share tokens expire, are high entropy, and use secure cookies.
- [x] Remote terminal default off, logged, quorum-capable.
- [x] Release key offline; TOFU, rotation and rollback documented in `docs/RELEASES.md`.
- [x] Custom ed25519 disclaimer and RFC 8032 vectors.
- [x] Prompt injection and Architect data handling documented.
- [x] Rate limits for public share (per-visitor login brake, bounded, capped PBKDF2 concurrency) and Architect (daily budget).
- [ ] Logs do not contain secrets.
- [x] `SECURITY.md` with private reporting instructions.

## 8. Tests and CI

- [x] `python tests/test_unit.py` passes.
- [x] `python tests/test_mesh.py` passes with isolated hubs.
- [x] `python -m compileall -q core` passes.
- [x] `node --check` passes for every `site/*.js` and module script.
- [x] GitHub Actions runs on Windows, Linux and macOS, Python 3.9/3.12/3.13, plus the PTY test, the two-hub mesh on all three, installer syntax and the export scan.
- [x] Tests never touch production state or ports 8790.
- [x] Hardening is unit-tested: Funnel login limits and brake, first-boot access, disk reserve, opt-in dependencies, English logs.
- [x] Release bundle verification test passes.

## 9. Real hardware

- [ ] Physical phone + real browser.
- [ ] Raspberry Pi `install.sh` (a clean Ubuntu install, enrollment and uninstall pass in WSL; Arch runs in production).
- [ ] macOS launchd.
- [ ] New device enrollment.
- [ ] `outbox.json` relay with a second capable hub.
- [ ] Real cross-tailnet peering (if claiming it).
- [ ] Funnel share expiry and stop.
- [ ] Rollback from a deliberately broken release in a test hub (unit-tested; try it once on real hubs).

## 10. Release

- [x] Tag `v0.1.0-alpha`.
- [x] `CHANGELOG.md`.
- [ ] GitHub release notes with known limitations.
- [ ] No signed production bundle attached unless the signing process is documented and the key is protected.
- [ ] Announce as alpha/personal, not production-ready.
- [ ] Monitor issues for security reports.

## Definition of done for v0.1

A new user can clone the repo, run a hub, expose it with `tailscale serve`, enable one module, switch languages, and understand the security model without reading the source.
