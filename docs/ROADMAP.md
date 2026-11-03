# Roadmap

## v0.1 (`0.1.0-alpha`)

First public alpha:

- [x] Leaderless mesh, CRDT directory, priority-based coordinator election.
- [x] Signed releases, TOFU key pinning, canary check, automatic and manual rollback, per-hub pause.
- [x] Quorum approvals for remote terminal, hub removal, and device shutdown.
- [x] Interactive browser terminal (POSIX PTY and Windows ConPTY) with hub-to-hub WebSocket relay.
- [x] Funnel gate with expiring public links, per-visitor login rate limiting, and streamed uploads.
- [x] Federated service catalog, custom labels, drag-and-drop route ordering, replicated blobs, and store-and-forward relay.
- [x] English and Spanish UI; English codebase and logs enforced by tests.
- [x] Self-contained one-folder module system (`core/modules/<id>/`), with built-in OctoPrint and read-only Architect modules plus `docs/examples/hello/`.
- [x] Offline four-hub demo (`python tools/demo.py`), screenshots and GIF.
- [x] Public export scanner (`tools/export_public.py`) and CI across Linux, Windows and macOS (Python 3.9, 3.12, 3.13).

## Next (`v0.2`)

- Test installers and service managers on more physical hardware (Raspberry Pi, macOS launchd, additional Linux distributions).
- Real cross-tailnet peering between two separate production tailnets.
- End-to-end automated test for Funnel share expiry and login lockout.
- Run community modules in a separate subprocess with restricted permissions instead of inside the hub process.

## Later (`v0.3+`)

- "Explain this alert" shortcut and on-demand daily brief in the Architect module.
- Local model presets (Ollama, LM Studio) in Architect settings.
- Automatic browser failover across hub URLs if the entry hub is offline.

## Non-goals

- Multi-tenant hosting.
- Replacing Tailscale's control plane or Headscale.
- Letting an LLM execute commands or modify state without human action.
- An automated third-party plugin downloader before subprocess isolation exists.
