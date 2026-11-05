# Changelog

## Unreleased

### Fixed

- **Renaming a hub no longer leaves its old name on the other hubs.** The renamed hub dropped the old directory key instead of marking it removed, so the rest of the mesh kept showing the old name as an offline hub (and gossip could bring it back). The old name now stays as a tombstone that spreads like any other removal.
- **Removing a hub always goes through the approval quorum.** Whether the quorum can be skipped is decided only by the hub's own code after the approvals agree, never by the request.

## 0.1.0-alpha (2026-11-03)

First public alpha of **Shoal**.

### Highlights

- **Leaderless hub mesh (`core/mesh.py`, `core/crdt.py`).** Every device keeps the full directory and syncs over signed gossip using hybrid logical clocks (HLC). An ephemeral coordinator is elected by priority for jobs that need a single node (Web Push, network clipboard) and fails over automatically.
- **Per-device service index and fleet catalog.** Discovers routes from `tailscale serve`, Docker containers and `services.json`, checks whether they respond, and lets you start/stop services, inspect logs, edit start hooks, rename or hide routes, and drag cards to reorder them across the mesh.
- **Signed peer-to-peer updates (`federation.py`, `docs/RELEASES.md`).** Your first hub signs releases with your own Ed25519 key (`release_key.json`), tests new code for 45 seconds before announcing it healthy, and rolls back to `app.prev` automatically if startup fails. Other hubs pin your key on first contact (TOFU), verify bundles before applying, and support per-hub pause (`--pause-updates`) and rollback (`--rollback`).
- **Interactive browser terminal (`core/ptyhost.py`, `core/ws.py`).** Full interactive shell over WebSocket (xterm.js with WebGL renderer and touch key bar) backed by a POSIX PTY on Linux, macOS and Android/Termux, and a ConPTY pseudoconsole on Windows. Sessions survive dropped connections for 15 minutes and can be relayed hub-to-hub (`api/term/pty?node=<hub>`).
- **Quorum approvals (`core/approvals.py`).** Sensitive actions (enabling the remote terminal, removing a hub, shutting down a device) can require confirmation from 1 to 3 hubs.
- **Shared clipboard, files and Taildrop.** CRDT-backed clipboard history with configurable mesh-wide retention (count and age) and Windows clipboard sync, a multi-root file browser, content-addressed replicated blobs (`core/blobs.py`), and Taildrop integration.
- **Expiring public links via Tailscale Funnel (`shares.py`, `core/adapters/funnel.py`).** Publishes up to two services or route groups on ports 8443 and 10000 while keeping port 443 private, with expiring bearer tokens and an optional PBKDF2 login gate, shown in the visitor's language (English or Spanish).
- **Plug-and-play modules (`core/modules/`, `docs/MODULES.md`).** Any `core/modules/<id>/` folder with `module.json` and `__init__.py` is discovered automatically, signed, and distributed with releases. Ships with two optional modules (disabled by default):
  - **OctoPrint** (`core/modules/octoprint/`): 3D printer status, temperature history, camera snapshot and safe job controls.
  - **Architect** (`core/modules/advisor/`): read-only analyst for OpenAI-compatible or local models using a sanitized, redacted fleet snapshot.
- **Installers, demo mode and export scanner.** One-line installers with `--uninstall [--purge]` for Linux, macOS, Raspberry Pi, Windows and Android/Termux, an offline four-hub demo (`python tools/demo.py`), and a pre-publish privacy/secret scanner (`python tools/export_public.py`).

### Security hardening

- **SSRF protection (`core/tailnet.py`).** Hub URLs from gossip, enrollment and peering are restricted to `http(s)` on Tailscale IPs (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) or MagicDNS names in the hub's tailnet. Hub-to-hub requests, local service probes, Architect requests and OctoPrint calls never follow HTTP redirects.
- **First-boot CGNAT guard.** A hub with no known devices yet only admits loopback until enrolled, rather than trusting arbitrary addresses in `100.64.0.0/10` (which is also used by ISP carrier-grade NAT).
- **Funnel and upload limits.** Negative or malformed `Content-Length` headers are rejected; Funnel login bodies are capped at 4 KB with per-visitor lockout (using the last `X-Forwarded-For` hop written by `tailscaled`) and bounded PBKDF2 concurrency; proxied uploads stream in 1 MB chunks (up to 1 GB); file and blob uploads keep a 2 % disk reserve (256 MB to 2 GB) and return HTTP 507 when full.
- **Identity and CSRF checks.** `Tailscale-User-Login` and `X-Forwarded-For` are trusted only when the TCP peer is loopback; browser requests and the terminal WebSocket must come from the hub's own origin, port included, so a page on another port of the same name (a Funnel share, another service) cannot drive the API; hub-to-hub endpoints verify Ed25519 signatures and nonces (`X-Hub-Auth`).
- **Paired tailnets stay outside.** Hubs of a paired tailnet are never trusted as the owner and reach only the federated APIs; the directory entries and relayed messages they send are bound to their own tailnet, they have no say over quorum, releases or bad versions, and what they may share is decided by the accepting hub, not by the invitation.
- **Signed files only.** An update bundle with any file outside the signed fingerprint (a `.pyc`, a native module) is refused, and applying never copies one. The unused single-master updater, which applied bundles without checks, is gone.
- **Mesh data in the interface.** Everything that arrives by gossip (device and service names, notifications, monitor data, priorities) is escaped; links from mesh data must be `http(s)`; notifications open only pages of the hub; directory fields of the wrong type are dropped instead of breaking the coordinator election.
- **Services at their own address.** The palette and Control open a service by its own URL, with `api/goto` as the fallback, so a service's scripts do not run inside the hub's origin. `api/goto` keeps the query string and never goes to a paired tailnet.
- **Smaller doors.** The hub's data and code folders stay shut in the files browser until the remote terminal is on; Funnel never publishes the hub itself; enrollment without the Tailscale CLI cannot take over another hub's name; blobs from other hubs are streamed to disk.

### Known limits

- Used daily on Windows, Arch Linux and Android; Ubuntu is tested in CI/WSL, while Raspberry Pi and macOS still need broader real-hardware testing.
- Cross-tailnet peering is tested in isolation but not yet between two separate production tailnets.
- The pure-Python Ed25519 fallback passes RFC 8032 test vectors but has not been independently audited (`cryptography` is used when installed).
