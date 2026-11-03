# Deployment

## Requirements

- Python 3.9+ on every hub.
- Tailscale connected on the device.
- HTTPS/MagicDNS enabled in the tailnet for `tailscale serve` and Funnel.
- Optional: `pywebpush` and `cryptography` for local Web Push. Without them, notifications can be relayed by a capable coordinator.
- `cryptography` also gives native Ed25519 (faster signature checks); without it a pure-Python implementation is used. The installers add it and start the hub with `--auto-deps`, which retries the installation once a day if it is missing. A hub started by hand never installs anything; it logs the command to run instead.

## Data directory

A hub keeps its mutable state in its data folder (`~/.hub-core` on Linux, macOS and Android; `%LOCALAPPDATA%\Shoal\data` on Windows; or `--home` / `$HUB_CORE_HOME`):

```
<data>/
  config.json
  directory.json
  labels.json             (custom service titles/descriptions)
  route_order.json
  identity.json
  identity_pins.json
  release_keys.json
  subscriptions.json
  vapid_private.pem
  release_key.json        (first hub only; back it up offline)
  modules/
    octoprint.json
    advisor.json
  app.prev/               rollback snapshot of the previous version
```

Updates replace the code tree (`core/`, `site/`, top-level scripts) while leaving the data folder untouched.

## Run manually

```bash
python -m core --detect
python -m core --home ~/.hub-core --port 8790 --bind 127.0.0.1
```

## Publish inside the tailnet

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8790
```

Open `https://<device>.<tailnet>.ts.net/hub/`.

If you use a subpath, make sure the hub is reachable at `/hub/` and relative asset URLs still work.

## Windows

The installer creates a scheduled task. The task should run:

```
pythonw.exe -m core --home <data-dir> --port 8790 --bind 127.0.0.1
```

Use an absolute Python path. Do not run two supervisors for the same hub; they compete for the port and can trigger rollback loops.

## Linux / Raspberry Pi / macOS

`install.sh` creates a systemd unit or launchd job and publishes the hub with `tailscale serve` when possible. If the device should coordinate, set:

```bash
HUB_PRIORITY=100
```

Test the installer on the target hardware before relying on it. The code is portable, but the service manager paths differ.

## Android / Termux

Termux has no `tailscale serve`, so the hub is served over HTTP on the tailnet. Use Termux:API and Termux:Boot for notifications and startup.

Run the hub under a single supervisor loop. Do not kill the supervisor when stopping the hub; use an exact PID or a pattern that matches only the child process.

## Funnel

Enable Funnel once in the Tailscale admin console. Then use the share controls on the index to publish a route or a group of routes.

- Publishes up to two public shares from a hub on ports **8443** and **10000**; port **443** stays the private hub.
- Traffic is gated at `/s/<token>`, with an optional PBKDF2 login in front.
- Durations range from 15 minutes to no expiry.
- Stopping a share closes its Funnel port cleanly.
- Public links are bearer tokens; treat them like passwords.

## Release signing

On your first hub (the installer does this automatically):

```bash
python -m core --home <data> --signing-key
```

This creates `<data>/release_key.json` with the private seed and prints the public key. Back it up offline and never commit it. When code changes on that hub (or when you run `python -m core --apply-update`), it signs `RELEASE.json` and serves the bundle at `/dist/hub.zip`.

Every other hub pins the first author key it sees (TOFU). Once a key is pinned, unsigned or differently signed releases are rejected. See [RELEASES.md](RELEASES.md).

## Backup and restore

- Back up `$HUB_CORE_HOME/` encrypted.
- Back up `release_key.json` separately, offline.
- Keep at least one `app.prev` snapshot.
- Test restore on a disposable hub before an emergency.

## Troubleshooting

| Symptom | Check |
|---|---|
| `Address already in use` | Another hub/supervisor is running; stop it by exact PID |
| Hub not reachable | `tailscale serve status`, bind address, firewall |
| Modules missing | `api/caps`, module state in **Control → Modules** |
| Architect says needs config | API key/base URL in the module settings |
| Update loops | `state/update_pending.json`, `state/bad_versions.json`, logs |
| Funnel 404 | You are outside `/s/<token>`; that is expected |
| Notifications queued | No capable coordinator; check `outbox.json` and relay |

## Test isolation

Use temporary homes, ports `8791/8792`, `--bind 127.0.0.1`, and `HUB_NO_RESTART=1`. Never point a test at the production data directory.

```bash
python tests/test_mesh.py
```
