# Updates, signing and recovery

Every hub in your mesh runs the same code, and the hubs update each other. This page covers how to
update your mesh when a new version of Shoal comes out, who is allowed to publish code to your
hubs, and what to do when an update goes wrong.

Paths below use `<data>` for a hub's data folder: `~/.hub-core` on Linux, macOS and Android,
`%LOCALAPPDATA%\Shoal\data` on Windows (or whatever you passed to `--home`).

## Updating from GitHub

Do this on **one** device: your first hub, the one you installed from your clone of the
repository (it holds your signing key).

```sh
cd shoal
git pull
python -m core --apply-update     # optional: apply now instead of after two quiet minutes
```

That is all. What happens next:

1. The hub notices that the code on disk changed. It waits until the files have been quiet for two
   minutes (or not at all, with `--apply-update` or the button in Control › Settings), checks that
   every Python file compiles, keeps a copy of the running code and restarts.
2. On startup it signs the new code with your key, as the next release of your mesh.
3. For 45 seconds it watches itself. If the new code does not start, it puts the old code back and
   marks that version bad, so no other hub ever installs it.
4. Once it is healthy it says so, and your other hubs download the new code from it, check the
   signature, keep their own copy of the old code and restart. In a few minutes every hub is on the
   new version.

Your hubs never download code from GitHub, or from anyone outside your tailnet. If this project's
repository were ever compromised, nothing would reach your devices until you pulled it yourself, and
you can read every change before you do (`git log -p`). That is on purpose.

### Version numbers

There are two numbers, and they mean different things:

- **The Shoal version**, like `0.1.0-alpha`. That is the project's release, the same for everybody,
  tagged `v0.1.0-alpha` on GitHub. `python -m core --version` prints it, and Control › Settings shows
  it.
- **Your mesh's release number** (`seq`). It counts the signed releases *your* hubs have published,
  starting at 1 on your first install. It goes up by one every time your first hub signs new code:
  after a `git pull`, after you edit a file, after a rollback. Someone else's mesh has its own count;
  the two are not related.

So after installing `0.1.0-alpha` your mesh is at release 1. Pull `0.2.0` later and it might be at
release 2, or 7 if you edited files in between. Both are fine.

## The signing key

The installer creates it on your first hub. You can also do it by hand:

```sh
python -m core --home <data> --signing-key   # creates <data>/release_key.json once, prints the public key
```

(Without `--home` it uses `~/.hub-core`, which is right on Linux, macOS and Android but not where the
Windows installer keeps its data.)

- The private key is `<data>/release_key.json`. It never leaves that hub: it is not in the update
  bundle, not in git, and not in any API.
- **Back it up somewhere offline.** Whoever holds it can push code to every hub of your mesh, and
  without it you cannot publish updates to them.
- A fresh clone contains no key and no `RELEASE.json`. Your hubs never trust the project's key or
  anyone else's: they trust the first key they see from a hub of their own mesh and pin it in
  `<data>/release_keys.json`.
- Create the key before you add a second device. Until a key is pinned, hubs accept unsigned code
  from their own mesh.

### Losing or replacing the key

There is no automatic rotation. To move to a new key:

1. On your first hub, delete `<data>/release_key.json` and run `--signing-key` again.
2. On **every** hub, delete `<data>/release_keys.json` so the next signed release is pinned again.
3. Change any file on your first hub (or run a rollback) so a new signed release goes out.

Do it while all hubs are online; a hub that was offline keeps refusing the new key until you clear
its pin by hand.

## When an update is bad

From Control › Settings › *Version and updates*, or on the device itself:

```sh
python -m core --release           # what runs, what ran before, whether updates are paused
python -m core --rollback          # go back to the previous version
python -m core --pause-updates     # stop adopting new versions on this hub
python -m core --resume-updates
python -m core --apply-update      # apply the code on disk now
```

(These talk to the hub running on `127.0.0.1`; add `--port` if it is not 8790.)

### What each action does

| | Where it applies | Spreads to other hubs? |
|---|---|---|
| **Pause updates** | Only this hub. A local setting (`updates_paused` in its config). | No. |
| **Resume updates** | Only this hub. It catches up with the newest good release. | No. |
| **Roll back** on your first hub | It goes back, and the restored code is signed as a *new* release (`seq + 1`). | Yes: every hub adopts it like any update, within minutes. |
| **Roll back** on any other hub | Only this hub goes back. | Partly: the abandoned version is marked *bad* for the whole mesh, so no hub that has not adopted it yet will. Hubs already running it stay on it. |

- **Pause** freezes one hub's code, for example while you investigate a problem on another one, or
  on a device you do not want touched today. Local edits still apply on that hub.
- **Rollback on another hub** stays local until your first hub publishes something newer; then that
  hub follows the mesh again (unless its updates are also paused).
- A version marked bad is never adopted again. You fix forward: the next change becomes a new
  release with a higher `seq`.

### Can hubs run different versions?

Yes, for a while. What hubs exchange (the directory, the clipboard, labels, settings, approvals)
is versioned by `PROTOCOL` (today 2), and new fields are only ever added, so an older hub ignores
what it does not understand and keeps syncing the rest. Control › Network marks every hub that is
not on the current code, and Control › Settings says whether this hub's updates are paused.

What a hub on older code misses is the newer code itself: its fixes, its new screens, its security
hardening. Pausing is a brake for hours or days, not a way to run a mixed mesh for good. If a
release ever had to change the protocol in a way older hubs cannot follow, it would raise
`PROTOCOL`, and hubs on the old number would stop merging with the new ones until they update.

Limits: a hub keeps only one previous version. A release that breaks the update channel itself
(gossip, bundle download) cannot be fixed from the mesh: restore that hub by hand from
`<data>/app.prev`, or reinstall it.

## For contributors: keeping updates safe

During a rollout, hubs on version N talk to hubs on N+1 for a few minutes. So:

- Only add fields to what hubs exchange and to the files in `<data>`. Never rename a field or change
  its type; read new fields with a default (`.get("field", default)`).
- When a stored file changes shape, the new code must still read the old shape (see
  `Mesh._migrate` or the OctoPrint module's `legacy_config`).
- Work on branches; `main` only gets what passed CI on Linux, Windows and macOS. Releases are tags
  (`v0.1.0-alpha`), and `core/__init__.py` and `pyproject.toml` carry the same version (a test
  checks it).

## Publishing the project (maintainers)

The public tree is built with `tools/export_public.py`: it takes only what git would publish and
refuses to export if it finds this installation's tailnet, device names, addresses, e-mail, keys or
other secrets. See [PUBLISHING.md](PUBLISHING.md).
