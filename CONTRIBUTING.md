# Contributing

Thanks for taking a look. This is a personal alpha project; keep changes focused and explain the tradeoffs.

## Development setup

```bash
git clone <repo>
cd <repo>
python -m compileall -q core
python tests/test_unit.py
node --check site/hub.js
```

No Python package installation is required for the core. Optional Web Push needs `pywebpush` and `cryptography`.

## Rules

- **English source.** Code, comments, docstrings, tests, logs, API errors, and commit messages are English.
- **UI strings live in catalogs.** Add both `site/i18n/en.json` and `site/i18n/es.json` (a module uses its own `i18n/`); run the parity test.
- **No secrets.** Never commit `state/`, `.env`, `services.json`, `site/data.json`, release keys, or screenshots with real tailnet data.
- **Test isolated.** Use temporary homes, ports `8791/8792`, and `HUB_NO_RESTART=1`. Do not touch production.
- **No fixed master.** Do not reintroduce a central controller or a `role: master`.
- **Modules load only from `core/modules/`.** That folder is signed code. Never load Python from the data folder, a download, or anything a browser or another hub can write.
- **The Architect is read-only.** Do not give it (module `advisor`) tools that change state without a separate security design.
- **Keep the core dependency-free.** Optional dependencies must fail gracefully.

## Pull requests

- Explain the problem and the design.
- Include tests for behavior changes.
- Update docs and both language catalogs.
- Run the unit and integration tests.
- Do not mix large refactors with behavior changes.
- Call out security implications, especially auth, Funnel, terminal, updates, and modules.

## Commit style

Short imperative subject in English, for example:

```
Add optional module registry
Fix Funnel gate for expired shares
Translate module settings
```

## Security

Do not open a public issue for a vulnerability. Use GitHub's private vulnerability reporting / Security Advisory feature. See [docs/SECURITY.md](docs/SECURITY.md).

## License

By contributing, you agree that your contribution is licensed under the MIT License.
