# Architecture

Every device runs the same single Python process, serves the same web UI, and keeps its own data
folder. There is no server in the middle: the hubs are peers, and any of them can answer for the
whole network.

The diagrams below are [Mermaid](https://mermaid.js.org/); GitHub renders them in place.

## The big picture

Every device in the tailnet runs the same hub. You open whichever one is closest; it knows about
all the others because the hubs gossip a shared directory among themselves.

```mermaid
flowchart TB
    you(["You, in a browser on any device of the tailnet"])

    subgraph tailnet["Your tailnet (WireGuard, Tailscale identity)"]
        direction LR
        subgraph pc["Windows PC"]
            hpc["Shoal hub"]
        end
        subgraph srv["Linux server / Raspberry Pi"]
            hsrv["Shoal hub"]
            svc[("local services<br/>NAS, camera, ...")]
        end
        subgraph phone["Android phone (Termux)"]
            hph["Shoal hub"]
        end
        hpc <-. "gossip (signed)" .-> hsrv
        hsrv <-. "gossip" .-> hph
        hsrv --> svc
    end

    you -- "https://device/hub/ (tailscale serve)" --> hpc

    visitor(["Visitor on the Internet"]) -- "Funnel :8443 / :10000<br/>share link + login" --> hsrv
    other["Paired tailnet<br/>(a friend's hubs)"] <-- "signed federation<br/>(catalogue, shared files)" --> hph
```

- **Same hub everywhere.** The same `core/` and `site/` run on Windows, Linux, macOS, Raspberry Pi
  and Android/Termux. What differs is what each device *can* do, which the hub detects at start.
- **No fixed master.** Every hub stores the full directory. A *coordinator* is elected for the few
  jobs that need one place (Web Push, the network clipboard); if it disappears another takes over.
- **Tailscale is the front door.** Access comes from Tailscale identity. The hub adds no accounts
  of its own, except the optional login in front of public (Funnel) links.

## Inside one hub

```mermaid
flowchart TB
    browser(["Browser UI<br/>site/: HTML, CSS, vanilla JS"]) --> http

    subgraph hub["One process: python -m core"]
        http["HTTP server and gate<br/>core/server.py<br/>who may enter, routes, static files"]
        ws["WebSocket<br/>core/ws.py"]

        subgraph caps["What this device can do"]
            adapters["Adapters<br/>core/adapters/<br/>tailscale, files, terminal, services,<br/>clipboard, notify, docker, funnel, ..."]
            pty["Interactive shells<br/>core/ptyhost.py<br/>PTY on POSIX, ConPTY on Windows"]
            modules["Optional modules<br/>core/modules/<br/>OctoPrint, Architect (AI)"]
        end

        subgraph meshbox["The mesh"]
            mesh["Mesh<br/>core/mesh.py<br/>directory, gossip, coordinator,<br/>releases, catalogue"]
            crdt["CRDTs + HLC clocks<br/>core/crdt.py"]
            relay["Store-and-forward relay<br/>core/forward.py"]
            blobs["Replicated files<br/>core/blobs.py"]
            quorum["Approvals (quorum)<br/>core/approvals.py"]
        end

        subgraph trust["Trust"]
            identity["Hub identity and signed requests<br/>core/identity.py"]
            crypto["Ed25519 (RFC 8032)<br/>core/crypto.py"]
            fed["Signed releases and bundles<br/>federation.py"]
        end
    end

    http --> adapters
    http --> modules
    http --> mesh
    http --> ws --> pty
    mesh --> crdt
    mesh --> relay
    mesh --> blobs
    mesh --> quorum
    mesh --> fed
    mesh --> identity --> crypto
    fed --> crypto
    hub --- data[("Data folder (state/)<br/>config, directory, keys,<br/>clipboard, logs, app.prev")]
```

```
core/
  server.py       HTTP server, auth gate, routes, static files
  mesh.py         directory CRDT, gossip, coordinator, releases, catalogue
  identity.py     hub keys and signed hub-to-hub requests
  crypto.py       ed25519 (RFC 8032), native when `cryptography` is installed
  crdt.py         HLC and merge primitives
  approvals.py    quorum requests and execution at the origin
  forward.py      store-and-forward relay
  blobs.py        content-addressed replicated files
  peering.py      signed invitations and allowlists between tailnets
  ptyhost.py      interactive shells (PTY / ConPTY) that survive reconnections
  ws.py           WebSocket server and hub-to-hub WebSocket relay
  deps.py         optional native dependencies (opt-in)
  adapters/       platform capabilities (Tailscale, files, terminal, ...)
  modules/        optional product features (OctoPrint, Architect)
federation.py     code fingerprint, signed releases, bundles, snapshots
site/             vanilla HTML/CSS/JS UI and i18n catalogs
```

## Principles

1. **Same hub everywhere.** One code base, one bundle, every platform.
2. **Only show what works on that device.** Adapters declare what the device can do. The UI hides
   unavailable sections via `api/caps`, so a phone never shows a button only a PC can run.
3. **Local first, delegate when needed.** If a device cannot do something locally, a capable hub
   can do it on its behalf.
4. **No fixed master.** Every hub stores the full directory. The coordinator is an ephemeral role.
5. **Signed code only.** A local edit can run on that hub, but it does not spread until it is part
   of a signed release.
6. **Safe defaults.** Remote terminal, Funnel, and optional modules are off until you enable them.
   Nothing is installed on a device without an explicit opt-in.

## Who gets in

Every request goes through the same gate before any route runs.

```mermaid
flowchart TD
    req([Request]) --> funnelq{"Arrived on a Funnel port<br/>(8443 / 10000)?"}
    funnelq -- yes --> share{"Valid share link<br/>and login?"}
    share -- yes --> proxy["Proxy to the shared service<br/>(body streamed, size-capped)"]
    share -- no --> nf["404 / login page"]

    funnelq -- no --> serveq{"Through tailscale serve?<br/>(TCP peer is loopback and<br/>sends identity headers)"}
    serveq -- yes --> owner{"Tailscale-User-Login<br/>is the tailnet owner?"}
    owner -- yes --> ok([Allowed])
    owner -- no --> deny([403])

    serveq -- no --> loop{"From this device?<br/>(loopback)"}
    loop -- yes --> ok
    loop -- no --> known{"Tailnet address of an owner<br/>device or a hub in the directory?"}
    known -- yes --> ok
    known -- no --> signed{"Signed by a hub of a<br/>paired tailnet (X-Hub-Auth)?"}
    signed -- yes --> fedonly(["Federated APIs only<br/>(gossip, relay, catalogue, blobs)"])
    signed -- no --> deny
```

- Identity headers (`Tailscale-User-Login`, `X-Forwarded-For`) are believed only when the TCP
  peer is loopback, i.e. `tailscaled` itself. A tailnet client cannot forge them.
- A tailnet address alone proves nothing: `100.64.0.0/10` is also carrier-grade NAT on many ISPs
  and mobile carriers. Before a hub knows any device (first boot without the Tailscale CLI, before
  enrolling) only the device itself gets in.
- "Hub in the directory" means a hub of this tailnet. Hubs of a paired tailnet are in the directory
  too, but they only reach the federated APIs, and what they send speaks only for their own tailnet.
- Browser requests must come from the hub's own origin, port included (CSRF, cross-site WebSocket).

## Mesh and directory

The directory (`directory.json`) is a map of hub records. Every field carries a hybrid logical
clock (HLC), so two hubs editing different fields never overwrite each other, and the newest edit of
the same field wins on every hub.

```mermaid
sequenceDiagram
    participant A as Hub A
    participant B as Hub B (random peer)
    Note over A: every HUB_MESH_EVERY s (60),<br/>to HUB_GOSSIP_K (3) random hubs
    A->>B: POST /api/mesh, signed with A's key<br/>directory, release, bad versions,<br/>labels, UI settings, replica hash
    B->>B: verify the signature (pinned key)<br/>merge field by field (HLC)
    B-->>A: B's own view
    A->>A: merge the answer
    Note over A,B: every HUB_MESH_ANTI rounds (5): full sweep of all hubs.<br/>Silent for HUB_MESH_ALIVE s (180): offline.
```

- Removal is a tombstone (`removed`), not a delete, so a removed hub cannot come back by gossip.
- Hub keys are pinned on first contact (TOFU); a changed key is refused.
- Service labels, the clipboard, Web Push subscriptions and the mesh-wide UI settings (theme,
  mascots, pixel type) are CRDTs too, replicated by the same gossip.
- Enrollment works against any hub and then spreads by gossip.
- Messages to an offline hub wait in a store-and-forward outbox and are delivered when it returns.

## Coordinator

A hub can coordinate only if it has the Tailscale CLI and Web Push. The coordinator is chosen by
priority, then age, then name. It sends notifications, owns the network clipboard, and announces
new devices. Other capable hubs keep a CRDT replica and take over after a partition. Without a
coordinator each hub keeps working; notifications wait in the outbox and are relayed later.

## Quorum

Sensitive actions can require approval from 1 to 3 hubs:

- enabling the remote terminal;
- removing a hub from the mesh;
- shutting down a device.

```mermaid
sequenceDiagram
    participant U as You (on hub A)
    participant A as Hub A (origin)
    participant B as Hub B
    U->>A: enable the remote terminal
    A->>A: create the request (pending)
    A-->>B: replicated by gossip
    Note over B: you approve it from hub B<br/>(another device)
    B-->>A: approval, signed by B
    A->>A: quorum reached: execute at the origin
    A-->>B: result replicated
```

If fewer hubs are online than the configured quorum, the quorum is lowered automatically. Funnel
is deliberately outside the quorum: stopping public access must be a single, immediate decision.

## Releases

A release is the code fingerprint plus a signature: `RELEASE.json` holds
`{version, stamp, seq, pub, sig}`. Only the hub that holds the author key signs; every other hub
verifies and adopts.

```mermaid
flowchart TB
    edit["Code changes<br/>on the author hub"] --> watch["Watchdog: stable for 120 s<br/>and compiles"]
    watch --> sign["Restart and sign<br/>seq + 1 (ed25519)"]
    sign --> announce["Gossip announces<br/>the new release"]
    announce --> canary["First hub downloads<br/>/dist/hub.zip and verifies it"]
    canary --> apply["Snapshot to app.prev,<br/>apply, restart"]
    apply --> healthy{"Healthy for<br/>45 s?"}
    healthy -- yes --> confirm["Confirmed: announces ok"]
    confirm --> rest["The other hubs adopt it<br/>(or after 900 s without<br/>confirmation)"]
    healthy -- "no: fails to boot twice" --> back["Restore app.prev,<br/>mark the version bad"]
    back --> spread["'bad' spreads by gossip:<br/>no hub adopts it again"]
```

- `version` is a fingerprint over root `*.py`, `README.md`, `site/` and `core/`.
- `seq` is monotonic: a hub never moves to a release with a lower or equal sequence.
- The first author key is pinned (TOFU); after that an unsigned or differently signed release is
  ignored.
- A bundle is verified before it is applied: signature, matching fingerprint, safe paths and
  Python that compiles. A guardian in `core/__main__.py` restores `app.prev` even when the new code
  cannot be imported at all.

### Pause and rollback (per hub)

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Following
    Following --> Paused: pause updates
    Paused --> Following: resume updates
    Following --> RolledBack: roll back
    Paused --> RolledBack: roll back
    RolledBack --> Following: the author publishes something<br/>newer (and updates are not paused)

    note right of Paused
        This hub keeps its code; the mesh moves on.
        Local edits still apply here.
    end note
    note right of RolledBack
        On the author hub: the old code is re-signed
        as seq + 1 and the whole mesh goes back.
        On any other hub: only this hub goes back.
        Either way the version is marked bad mesh-wide.
    end note
```

Hubs on different versions keep working together: what they exchange is versioned (`PROTOCOL`,
today 2) and new fields are additive, so a paused or rolled-back hub still syncs the directory,
the clipboard and the settings. What it misses are the features and fixes of
the newer code. Details and commands: [RELEASES.md](RELEASES.md).

## Remote terminal

```mermaid
sequenceDiagram
    participant Br as Browser (xterm.js)
    participant A as Hub A (the one you opened)
    participant B as Hub B (the target device)
    Br->>A: WebSocket /api/term/pty?node=B
    A->>B: WebSocket, signed hub request
    Note over A: A only relays the bytes<br/>and stores nothing
    B->>B: start the shell (PTY / ConPTY)<br/>or reattach an existing session
    Br-->>B: keystrokes and resizes
    B-->>Br: output (recent output replayed on reconnect)
```

Sessions survive a closed tab or a dropped connection for 15 minutes. The terminal is off until
enabled (subject to the quorum), and the start and end of every session are logged with who opened it.

## Public sharing (Funnel)

Funnel publishes up to two services from one hub on the public ports **8443** and **10000**;
port 443 stays the private hub. The hub is the gate:

- a share link `/s/<token>` sets a cookie (`Secure`, `HttpOnly`, `SameSite=Lax`); the token is
  high-entropy and expires;
- an optional login (PBKDF2 password) sits in front, with a per-visitor failure brake;
- everything else answers 404;
- request bodies are streamed to the service and capped; a login form bigger than 4 KB is refused
  unread.

## Modules

Modules are optional product features, not platform adapters. Each one is a folder in
`core/modules/<id>/` (its Python, manifest, strings and panel), found at startup and shipped inside
the signed bundle, so adding a folder on the first hub adds the module to every hub. A module
declares its manifest, configuration schema, routes, panel, events and permissions. See
[MODULES.md](MODULES.md).

The Architect is a module by design: the hub must work without an LLM, without internet, and
without API keys.

## Internationalization

English is the source language and the default. Spanish is a full UI translation. All user-facing
strings live in `site/i18n/{en,es}.json` (and each module's `i18n/`); server errors and logs are
English, and a test keeps them that way. See [I18N.md](I18N.md).

## What this is not

- Not a replacement for the Tailscale control plane or Headscale.
- Not a multi-tenant SaaS.
- Not a general-purpose RMM.
- Not a sandbox for untrusted modules. A module runs with the hub's rights; install only modules you have read.
