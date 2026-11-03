// Device page with an agent: everything goes through api/nodes/<name>/... (the hub proxy with a token).
(async () => {
  "use strict";
  await I18N.ready;
  const T = (k, v) => I18N.t(k, v);
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = (m) => { const t = $("#toast"); t.textContent = m; t.classList.add("show"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 1800); };
  // On the node's own hub (agent, port 8790) we talk to it directly;
  // on the PC hub, through its api/nodes/<name> proxy.
  const AGENT = !new URLSearchParams(location.search).get("n"); // no ?n= -> this hub's own device
  let name = new URLSearchParams(location.search).get("n") || "";
  const api = (p) => AGENT ? p.slice(1) : `api/nodes/${encodeURIComponent(name)}${p}`; // relative: works with or without /hub/
  // Header links are hidden by fleet.js according to api/caps (data-cap)
  const post = async (p, body) => {
    const r = await fetch(api(p), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  };
  const size = (b) => b < 1024 ? `${b} B` : b < 1 << 20 ? `${(b / 1024).toFixed(0)} KB` : b < 1 << 30 ? `${(b / (1 << 20)).toFixed(1)} MB` : `${(b / (1 << 30)).toFixed(1)} GB`;

  async function load() {
    let i;
    try { i = await (await fetch(api("/node"), { cache: "no-store" })).json(); } catch { i = { error: "no response" }; }
    if (AGENT && i.name) {
      name = i.name;
      document.querySelector(".mark").innerHTML = `<span>shoal</span><span class="slash">/</span><span>${esc(name.toLowerCase())}</span>`;
    }
    $("#name").textContent = name;
    document.title = T("nodo.title", { name });
    const android = !!(i.platform && i.platform.termux);
    if (i.error) { $("#lede").textContent = `${T("nodo.noResponse")} (${i.error}).`; return; }
    // Only what this device can do (its own caps), with messages for its own platform.
    const caps = new Set(i.caps || []);
    $("#sec-clip").hidden = !caps.has("clipboard_device");
    $("#clip-note").hidden = !android;
    const pings = ["notify", "toast", "vibrate"].filter((c) => caps.has(c));
    document.querySelectorAll("[data-ping]").forEach((b) => { b.hidden = !pings.includes(b.dataset.ping); });
    $("#sec-ping").hidden = !pings.length;
    readEmpty = android ? T("nodo.readEmptyAndroid") : T("nodo.readEmpty");
    const model = [i.brand ? i.brand[0].toUpperCase() + i.brand.slice(1) : "", i.model].filter(Boolean).join(" ") || T("nodo.thisDevice");
    const osName = i.os || (i.android ? `Android ${i.android}` : "");
    $("#lede").textContent = `${model}${osName ? ` · ${osName}` : ""}. ${T("nodo.hubActive")}`;
    const b = i.battery;
    $("#bat-box").hidden = !b && !android;  // a desktop without a battery has nothing to show
    $("#bat").innerHTML = `${esc(b ? b.percentage : "-")}<small>%</small>`;
    $("#bat-sub").textContent = b ? [b.status === "CHARGING" ? T("nodo.charging") : b.status === "FULL" ? T("nodo.full") : T("nodo.discharging"),
      b.temperature != null ? `${b.temperature.toFixed(1)} °C` : ""].filter(Boolean).join(" · ") : T("nodo.termuxApi");
    $("#disk").textContent = i.storage ? `${size(i.storage.free || 0)} / ${size(i.storage.total || 0)}` : T("nodo.noData");
    $("#foot").textContent = T("nodo.hubVersion", { version: String(i.version || "").slice(0, 8), state: i.exec ? T("nodo.terminalOn") : T("nodo.terminalOff") });
    if (!i.shared) { $("#files").innerHTML = `<li class="clip-empty">${T(android ? "nodo.storageAccess" : "nodo.noFiles")}</li>`; return; }
    if (!fb.loaded) { fb.loaded = true; await loadRoots(); files(""); }
  }

  let readEmpty = "";
  $("#clip-get").addEventListener("click", async () => {
    try { const j = await (await fetch(api("/clip"), { cache: "no-store" })).json(); $("#clip").value = j.text || ""; toast(j.text ? T("nodo.readDone") : j.error || readEmpty); }
    catch (e) { toast(e.message); }
  });
  $("#clip-set").addEventListener("click", async () => {
    try { await post("/clip", { text: $("#clip").value }); toast(T("nodo.putDone")); } catch (e) { toast(e.message); }
  });
  document.querySelectorAll("[data-ping]").forEach((b) => b.addEventListener("click", async () => {
    const m = $("#msg").value || T("nodo.hello");
    try {
      if (b.dataset.ping === "notify") await post("/notify", { title: document.title.split(" · ")[0] || "Hub", body: m });
      if (b.dataset.ping === "toast") await post("/toast", { text: m });
      if (b.dataset.ping === "vibrate") await post("/vibrate", { ms: 500 });
      toast(T("nodo.sent"));
    } catch (e) { toast(e.message); }
  }));

  // ---------- files: locations, folder tree and listing ----------
  // The home location is always open; disks and the system root unlock while this hub's
  // remote terminal is enabled (the hub enforces it; here it only explains why).
  const fb = { root: "home", path: "", hidden: false, roots: [], open: new Set([""]) };
  const qs = (root, path) => `root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}${fb.hidden ? "&hidden=1" : ""}`;
  const join = (a, b) => (a ? `${a}/` : "") + b;
  const rootIcon = (r) => r.locked ? "ph-lock-simple" : r.kind === "home" ? "ph-house" : r.kind === "system" ? "ph-tree-structure" : "ph-hard-drives";
  const termHref = () => AGENT ? "terminal.html" : `terminal.html?n=${encodeURIComponent(name)}`;

  async function listDir(root, path) {
    const j = await (await fetch(api(`/files?${qs(root, path)}`), { cache: "no-store" })).json();
    if (j.error) throw new Error(j.error);
    return j;
  }

  async function loadRoots() {
    try {
      const j = await (await fetch(api("/files/roots"), { cache: "no-store" })).json();
      if (!Array.isArray(j.roots)) throw new Error(j.error || "no roots");
      fb.roots = j.roots;
    } catch { // a hub from before locations: only its home folder
      fb.roots = [{ id: "home", name: T("nodo.storageRoot"), kind: "home", locked: false }];
    }
    $("#fb-roots").innerHTML = fb.roots.map((r) => `
      <li><button type="button" class="fb-root${r.id === fb.root ? " on" : ""}${r.locked ? " locked" : ""}" data-root="${esc(r.id)}"
        ${r.id === fb.root ? 'aria-current="true"' : ""} title="${esc(r.locked ? T("nodo.lockedRoot", { name: r.name }) : r.path || r.name)}">
        <i class="ph ${rootIcon(r)}" aria-hidden="true"></i><span>${esc(r.kind === "home" ? T("nodo.home") : r.name)}</span></button></li>`).join("");
  }

  function crumbs() {
    const r = fb.roots.find((x) => x.id === fb.root);
    const parts = fb.path ? fb.path.split("/") : [];
    $("#crumbs").innerHTML = `<button type="button" data-dir="">${esc(r ? (r.kind === "home" ? T("nodo.home") : r.name) : T("nodo.storageRoot"))}</button>` +
      parts.map((p, k) => `<span aria-hidden="true">/</span><button type="button" data-dir="${esc(parts.slice(0, k + 1).join("/"))}"${k === parts.length - 1 ? ' aria-current="location"' : ""}>${esc(p)}</button>`).join("");
    $("#fb-up").disabled = !fb.path;
  }

  async function files(path, root = fb.root) {
    const r = fb.roots.find((x) => x.id === root);
    const note = $("#fb-note");
    if (r && r.locked) {
      note.innerHTML = `<i class="ph ph-lock-simple" aria-hidden="true"></i><span>${esc(T("nodo.lockedRoot", { name: r.name }))}</span> <a href="${esc(termHref())}">${esc(T("nodo.openTerminal"))}</a>`;
      note.hidden = false;
      return;
    }
    try {
      const j = await listDir(root, path);
      const rootChanged = root !== fb.root;
      fb.root = j.root || root;
      fb.path = j.path;
      note.hidden = true;
      if (rootChanged) { fb.open = new Set([""]); loadRoots(); }
      crumbs();
      $("#files").innerHTML = j.items.length ? j.items.map((f) => {
        const full = join(fb.path, f.name);
        return f.dir
          ? `<li><button type="button" class="f-dir" data-dir="${esc(full)}"><i class="ph ph-folder" aria-hidden="true"></i><span>${esc(f.name)}</span></button></li>`
          : `<li><a class="f-file" href="${api(`/file?${qs(fb.root, full)}`)}" download="${esc(f.name)}"><i class="ph ph-file" aria-hidden="true"></i><span>${esc(f.name)}</span><small>${size(f.size)}</small></a></li>`;
      }).join("") : `<li class="clip-empty">${T("nodo.emptyFolder")}</li>`;
      // the tree follows the listing: every ancestor of the current folder is expanded
      const parts = fb.path ? fb.path.split("/") : [];
      parts.forEach((_, k) => fb.open.add(parts.slice(0, k + 1).join("/")));
      tree();
    } catch (e) {
      note.textContent = e.message;
      note.hidden = false;
    }
  }

  // Lazy folder tree of the current location; each level is fetched when it is expanded.
  async function branch(path, depth) {
    let j;
    try { j = await listDir(fb.root, path); } catch { return ""; }
    const dirs = j.items.filter((f) => f.dir);
    const rows = await Promise.all(dirs.map(async (f) => {
      const full = join(path, f.name), open = fb.open.has(full);
      return `<li role="treeitem" aria-expanded="${open}"${full === fb.path ? ' aria-current="true"' : ""}>
        <div class="fb-node${full === fb.path ? " on" : ""}" style="--d:${depth}">
          <button type="button" class="fb-twist" data-twist="${esc(full)}" aria-label="${esc(T(open ? "nodo.collapse" : "nodo.expand", { name: f.name }))}"><i class="ph ph-caret-right" aria-hidden="true"></i></button>
          <button type="button" class="fb-name" data-dir="${esc(full)}"><i class="ph ${open ? "ph-folder-open" : "ph-folder-simple"}" aria-hidden="true"></i><span>${esc(f.name)}</span></button>
        </div>
        ${open ? `<ul role="group">${await branch(full, depth + 1)}</ul>` : ""}</li>`;
    }));
    return rows.join("");
  }
  async function tree() {
    const html = await branch("", 0);
    $("#fb-tree").innerHTML = html || `<li class="fb-empty">${esc(T("nodo.noFolders"))}</li>`;
  }

  document.addEventListener("click", (e) => {
    const tw = e.target.closest("[data-twist]");
    if (tw) {
      const p = tw.dataset.twist;
      if (fb.open.has(p)) fb.open.delete(p); else fb.open.add(p);
      tree();
      return;
    }
    const root = e.target.closest("[data-root]");
    if (root) { files("", root.dataset.root); return; }
    const d = e.target.closest("[data-dir]");
    if (d) files(d.dataset.dir);
  });
  $("#fb-up").addEventListener("click", () => files(fb.path.split("/").slice(0, -1).join("/")));
  $("#fb-hidden").addEventListener("click", (e) => {
    fb.hidden = !fb.hidden;
    e.currentTarget.setAttribute("aria-pressed", String(fb.hidden));
    e.currentTarget.querySelector("i").className = `ph ${fb.hidden ? "ph-eye" : "ph-eye-slash"}`;
    files(fb.path);
  });
  $("#up").addEventListener("change", async (e) => {
    for (const f of e.target.files) {
      try {
        const r = await fetch(api(`/file?${qs(fb.root, join(fb.path, f.name))}`), { method: "POST", body: f });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
        toast(T("nodo.uploaded", { name: f.name }));
      } catch (err) { toast(T("nodo.uploadFailed", { name: f.name, error: err.message })); }
    }
    e.target.value = ""; files(fb.path);
  });

  load();
  setInterval(() => { if (!document.hidden) load(); }, 60000);
})();
