# Security

## Status

Shoal is an alpha project for personal tailnets. It has not been independently audited. Treat it as powerful self-hosted software: review the code and restrict exposure.

## Boundaries

1. **Tailscale identity** is the primary boundary.
   - With `tailscale serve`, only the tailnet owner (or a trusted hub identity) is accepted.
   - Direct tailnet traffic is limited to known devices and hubs: the owner's devices (from
     Tailscale) and the hubs in the directory. Being inside `100.64.0.0/10` is not enough, because
     that range is also carrier-grade NAT on many ISPs and mobile carriers. A hub that knows no
     device yet (first boot without the Tailscale CLI, before enrolling) admits only itself.
   - Loopback bypass is intentional for local administration.
2. **Hub-to-hub requests** carry `X-Hub-Auth`, a signed token bound to method, path, body hash, and a nonce. Cross-tailnet federation is explicit and signed.
3. **Identity headers are trusted only from loopback.** `Tailscale-User-Login` and `X-Forwarded-For` are accepted only when the TCP peer is the local `tailscaled`/Serve proxy. A direct tailnet client cannot forge them.
4. **Browser requests must come from the hub's own origin.** State-changing requests, `/api/` reads
   and the terminal WebSocket compare the `Origin` with the hub's scheme, host **and port**: a page on
   another port of the same name (a Funnel share on 8443, any other service on the device) is another
   origin. `Sec-Fetch-Site: cross-site` is refused, and so is `same-site` (every device of the
   tailnet shares the site) unless it is a link being followed. The UI is same-origin; CLI and hub
   clients send no Origin. Funnel public requests are handled by the share gate first.
5. **Funnel** is a public boundary. Only the share gate is reachable from the internet; every other path returns 404 without a Tailscale identity.
6. **Modules** are code you trust like the core: they run in the hub process with its rights, with no sandbox. They are loaded only from `core/modules/`, inside your signed code, never from the data folder or anything a browser or another hub can write. A module reaches your other hubs only because your first hub signed it. Read a module before you install it.
7. **Blob IDs are strict SHA-256 hex.** Downloads and deletes cannot traverse outside the blob store.
   A blob fetched from another hub is streamed to disk and checked against its hash.
8. **Request sizes are bounded.** A malformed or negative `Content-Length` is refused (Python's
   `read(-1)` would otherwise read until the client hangs up). JSON API bodies are capped at
   64 MB; files, blobs and proxied uploads are streamed in 1 MB pieces, never held whole in memory.
9. **Uploads leave the disk usable.** Before writing an upload (files, replicated blobs, Taildrop)
   the hub checks free space and keeps a reserve (2 % of the disk, between 256 MB and 2 GB), so a
   large upload cannot fill the SD card of a Raspberry Pi or a phone. It answers 507 instead.
10. **A paired tailnet stays outside.** Its hubs are in the directory, but they never count as the
    owner: only hubs of this tailnet are trusted addresses, and a paired hub reaches only the
    federated APIs. Directory entries it sends must belong to its own tailnet; it cannot speak as one
    of this tailnet's hubs, and a relayed message from it carries its tailnet whatever it claims, so it
    cannot approve, change the quorum, mark versions bad or pin a release key. What it may share is
    decided by the hub that accepts the invitation, never by the invitation. `api/goto` only goes to
    hubs of this tailnet.
11. **Updates carry only signed files.** A bundle with any file outside the signed fingerprint (a
    `__pycache__/*.pyc`, a native `.so`/`.pyd` next to a module, which Python would load before the
    `.py`) is refused, and applying a bundle never copies such a file.
12. **The hub's own folders follow the terminal switch.** The files browser's home root is always
    open, but the hub's data folder (keys, identity, the terminal switch) and code folder stay shut
    until the remote terminal is on, even when they live inside the home folder.
13. **Services open at their own address.** The palette and Control open a service by its own URL;
    `api/goto` is the fallback when there is none. A service shown through `api/goto` runs inside the
    hub's origin, so its scripts could use the hub's API: proxy only services you trust.
14. **Mesh data is escaped.** Names, labels, notifications and anything else that arrives by gossip
    is escaped before it reaches the page, links from mesh data must be `http(s)`, and directory
    fields of the wrong type are dropped (a malformed priority cannot stop the coordinator election).

## Local access

Loopback access is intentionally trusted for local administration and for `tailscale serve`/Funnel. On a shared multi-user machine, any local process that can reach `127.0.0.1:8790` can use the hub API. Run the hub under a dedicated OS account, keep the bind address loopback, and use OS-level isolation if the machine has untrusted local users.

## Secrets

Never commit:

- `state/` (VAPID private key, release key, subscriptions, clipboard, service labels, logs);
- `.env` (API keys, OctoPrint credentials);
- `services.json` (personal paths and commands);
- `site/data.json` (tailnet identity and IPs);
- `RELEASE.json` (production signature);
- screenshots or recordings with real names, domains, IPs, or tokens.

The release signing key is the most sensitive artifact. Keep it offline, back it up, and never put it in a module, container image, or public repository. Losing it means no further signed updates for that key; leaking it means an attacker can sign releases.

Rotate any key that may have been exposed. Use `state/modules/<id>.json` for module secrets; it is private, but the UI masks secret values and the API never returns them.

## Remote terminal

- Disabled by default.
- Two modes: one-shot commands on every device, and an interactive shell on a PTY over a
  WebSocket (`GET api/term/pty`): a PTY on Linux, macOS and Android/Termux, a ConPTY pseudo console
  on Windows 10 1809+. Windows shells start without inheriting the hub's own standard handles.
- One-shot commands are logged one by one; interactive sessions are logged when they open and
  close, not keystroke by keystroke.
- The WebSocket goes through the same owner check as every API and its `Origin` must match the
  hub (no cross-site WebSocket). Switching the terminal off closes every open shell.
- Another hub's shell (`api/term/pty?node=<hub>`) is carried by the hub the browser is on: it
  checks the owner and the `Origin` as above, then opens the same request to the target hub
  signed with `X-Hub-Auth` and the owner's login in `X-Hub-User`, and splices the two
  WebSockets without decoding them. The target applies its own checks (owner or directory hub,
  terminal enabled, quorum) exactly as for a direct connection.
- An interactive shell survives a dropped connection for 15 minutes so a phone can reattach;
  at most four sessions per hub.
- Enabling it can require quorum approval.
- Do not expose it through Funnel. The Funnel gate does not forward terminal routes.
- Treat it as equivalent to shell access on the device.

## Releases and updates

- ed25519 signatures with a monotonic sequence.
- TOFU key pinning; a release without a valid signature is rejected once a key is pinned.
- Bundle verification checks the fingerprint, safe paths, and compilability before applying.
- Canary confirmation and `app.prev` rollback.
- The custom ed25519 implementation passes RFC 8032 vectors but has not been audited. For high-assurance deployments, replace it with a vetted library.

## Funnel

- Funnel publishes up to two services from a single hub, on the public ports 8443 and 10000.
  Port 443 stays the private hub and is never made public.
- The hub does not expose service ports directly: it is the gate and the proxy.
- The optional login (user and PBKDF2-SHA256 password, 210 000 iterations) refuses a form body
  bigger than 4 KB unread, and brakes failures **per visitor**: five wrong passwords lock that
  Internet address out for five minutes. The visitor's address is the last `X-Forwarded-For` entry,
  the one `tailscaled` writes; earlier entries come from the client and are ignored, so they
  cannot be used to dodge the brake. Before this, every visitor shared one counter (Funnel traffic
  arrives from loopback), so a bot could lock the owner out.
- The failure table is bounded (4096 addresses) and at most two password checks run at a time,
  so a scan from many addresses cannot exhaust memory or CPU.
- Request bodies to a shared service are streamed and capped at 1 GB; chunked uploads are refused
  (411), since they cannot be forwarded safely without a length.
- The hub itself is never published through Funnel, only the services it proxies.
- Public links are bearer tokens. Anyone with the link can use it until it expires or is stopped.
- Use the shortest practical duration. "No expiry" is available but should be rare.
- The Funnel gate is outside the quorum by design; do not add an asynchronous approval step to an emergency stop.

## Requests a hub makes (SSRF)

A hub calls other hubs, the services you list, and the model provider of the Architect. Each kind is
limited so that nothing another party controls can point it somewhere else:

- **Hub URLs** arrive by gossip, enrollment and peering invitations, so a compromised hub could try
  to announce a router admin page, a cloud metadata address or a local port as its own URL. A hub
  only calls a hub URL that is `http(s)` to a Tailscale address (`100.64.0.0/10`,
  `fd7a:115c:a1e0::/48`) or a MagicDNS name of that hub's tailnet, with no credentials in it
  (`core/tailnet.py`). Loopback is accepted only on test meshes where the hub's own URL is loopback.
  Enrollment falls back to the device's tailnet name when it offers anything else, and a peering
  invitation whose URL is outside its tailnet is refused.
- **Hub-to-hub calls never follow redirects**, so an allowed hub cannot bounce a request to an
  address outside the tailnet.
- **Services** are proxied only from this hub's own route list (from `tailscale serve`, Docker or
  your `services.json`). The shared catalogue carries service keys, never targets: a request for a
  service on another hub goes to that hub, which proxies its own route.
- **The Architect** sends its API key only over `https`, or over plain `http` to this machine, the
  LAN or the tailnet (a local model). It does not follow redirects, because urllib would carry the
  `Authorization` header along. The OctoPrint client does not follow redirects either (it sends
  `X-Api-Key`). Module URLs must be `http(s)`; `file:` and other schemes are refused.

## Dependencies

The core is Python standard library only. The one optional native dependency, `cryptography`
(native Ed25519), is installed by the installers. A hub started with `--auto-deps` (the installers
add it) retries the installation in the background if it is missing. A hub started by hand
(`python -m core` from a clone) never changes the Python environment: it only logs the command
to run.

## Architect / AI

The Architect is read-only and optional:

- It receives a sanitized snapshot (hub names, online state, versions, service health).
- Recent logs are opt-in and redacted before being sent.
- It has no tools and cannot execute actions.
- Daily request budgets and timeouts bound cost.
- Provider API keys stay in `state/modules/advisor.json` and are masked in the UI.
- Prompt injection is still possible in any text sent to the model. Never grant the model tool access or shell access without a separate, explicit design.

## Threat model (summary)

| Threat | Mitigation |
|---|---|
| Unauthorized tailnet user | Tailscale identity, owner/allowlist checks |
| Public internet scanning | Funnel gate, 404 for everything else |
| Password guessing on a public link | PBKDF2, per-visitor brake, bounded table, capped concurrency |
| Memory or disk exhaustion | Size-checked and streamed bodies, disk reserve before uploads |
| Carrier-grade NAT neighbour (`100.64.0.0/10`) | Only known devices and hubs, never "anything in the range" |
| A hub announcing an internal address as its URL (SSRF) | Hub URLs limited to tailnet addresses and names, no redirects |
| API keys leaking through redirects or plain http | No redirects with credentials; keys only over https off the LAN |
| Stolen public link | Expiry, token entropy, one active route |
| Malicious hub in the same tailnet | `X-Hub-Auth` signatures, directory pins, nonce replay cache |
| Malicious peer tailnet | Explicit signed peering; never trusted as the owner; its entries and messages bound to its own tailnet; no say over quorum, releases or bad versions |
| Page on another port of the same name (CSRF) | Origin checked with its port; same-site subrequests refused |
| Code smuggled into an update bundle | Only files covered by the signed fingerprint are accepted and applied |
| Scripts in a proxied service | Services open at their own address; `api/goto` is a fallback |
| Tampered update | ed25519 signature, fingerprint check, safe extraction, rollback |
| Compromised release key | Offline key, limited distribution, key rotation procedure (future) |
| Malicious module | Loaded only from your signed code; you install it by hand on your first hub after reading it; no marketplace, no downloads |
| Prompt injection via logs | Read-only Architect, sanitized context, no tools |
| Secret leakage in logs | Redaction, no API keys in browser responses, never commit state |

## Reporting

Do not open a public issue for a vulnerability. Use GitHub's private vulnerability reporting / Security Advisory feature for this repository. Include reproduction steps, affected version, and impact. Avoid including real secrets or personal tailnet data.

## Hardening checklist

- [ ] Bind the hub to loopback behind `tailscale serve`.
- [ ] Enable HTTPS in the tailnet.
- [ ] Keep the remote terminal disabled unless needed.
- [ ] Keep Funnel off by default; use short expiries.
- [ ] Use a dedicated, offline release key.
- [ ] Review ACLs and tailnet sharing.
- [ ] Keep `state/` out of backups that are not encrypted.
- [ ] Test restore/rollback before relying on updates.
- [ ] Review module configuration and logs for secrets.
