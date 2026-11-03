// Control page: shared clipboard, Taildrop, device actions, exit node, hub mesh and monitor.
(async () => {
  "use strict";
  await I18N.ready;
  const T = (k, v) => I18N.t(k, v);
  const TN = (k, n, v) => I18N.tn(k, n, v);
  const caps = (window.__hubCaps && window.__hubCaps.caps) || [];
  const webUrl = (u) => /^https?:\/\//i.test(String(u || ""));  // a link from mesh data: never javascript: and the like
  const $ = (s) => document.querySelector(s);
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = (m) => { const t = $("#toast"); t.textContent = m; t.classList.add("show"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 1800); };
  const getJ = async (u) => (await fetch(u, { cache: "no-store" })).json();
  const post = async (u, body) => {
    const r = await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || j.output || `HTTP ${r.status}`);
    return j;
  };
  const ago = (ts) => I18N.agoSeconds(Date.now() / 1000 - ts);
  const device = () => {
    const ua = navigator.userAgent;
    return /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iPhone" : /Windows/.test(ua) ? "Windows" : /Mac/.test(ua) ? "Mac" : T("common.browser");
  };
  async function copyText(t) {
    try { await navigator.clipboard.writeText(t); }
    catch { const a = Object.assign(document.createElement("textarea"), { value: t }); document.body.append(a); a.select(); document.execCommand("copy"); a.remove(); }
  }
  const base = new URL(".", location.href).href.replace(/\/$/, "");
  // Install commands run on *another* device, so they must name this hub by its tailnet address:
  // localhost or 127.0.0.1 would point the new device at itself. The address the browser used is
  // kept when it is already a tailnet name; otherwise this hub's published URL (MagicDNS) is used.
  function installBase(here) {
    const h = location.hostname, named = /[a-z]/i.test(h) && h !== "localhost" && !h.endsWith(".localhost");
    if (named || !here) return base;
    if (here.url) return here.url.replace(/\/$/, "");
    return here.ip ? `http://${here.ip}:${location.port || 8790}/hub` : base;
  }
  function installCmds(here) {
    const b = installBase(here), u = new URL(b + "/"), sh = `curl -fsSL ${b}/install.sh | sh`;
    return {
      win: `irm ${b}/install.ps1 | iex`, sh,
      // Termux has no MagicDNS: curl gets the name resolved by hand to this hub's tailnet IP
      tx: here && here.ip && /[a-z]/i.test(u.hostname) ? `curl -fsSL --resolve ${u.hostname}:${u.port || (u.protocol === "https:" ? 443 : 80)}:${here.ip} ${b}/install.sh | sh` : sh,
    };
  }
  let netMe = "";
  let viewCache = null, viewCacheAt = 0, viewInFlight = null;
  function getMeshView() {
    if (viewCache && Date.now() - viewCacheAt < 5000) return Promise.resolve(viewCache);
    if (viewInFlight) return viewInFlight;
    viewInFlight = getJ("api/mesh/view").then((v) => {
      viewCache = v;
      viewCacheAt = Date.now();
      return v;
    }).finally(() => { viewInFlight = null; });
    return viewInFlight;
  }
  function invalidateView() { viewCache = null; viewCacheAt = 0; }

  // Hub name + Tailscale DNS. The API recreates Serve routes and returns the
  // new URL only after the new hostname answers.
  const nameForm = $("#hub-name-form"), nameInput = $("#hub-name");
  let currentHubName = "";
  if (nameForm) {
    getMeshView().then((v) => {
      currentHubName = v.me || "";
      if (nameInput) nameInput.value = currentHubName;
    }).catch(() => {});
    nameForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const name = (nameInput.value || "").trim();
      if (!name) return;
      if (name !== currentHubName && !confirm(T("control.hubNameConfirm", { name }))) return;
      try {
        const j = await post("api/hub/name", { name, change_dns: true });
        if (j.dns_changed && j.url) {
          location.assign(j.url.replace(/\/?$/, "/") + "control.html");
          return;
        }
        currentHubName = j.name;
        if (nameInput) nameInput.value = j.name;
        toast(T("control.hubNameSaved"));
        invalidateView();
        loadNet().catch(() => {});
      } catch (err) { toast(err.message); }
    });
  }

  // Per-hub public login for Funnel magic links.
  const authForm = $("#public-auth-form");
  if (authForm) {
    const authUser = $("#public-auth-user"), authPassword = $("#public-auth-password"), authNote = $("#public-auth-note");
    const loadPublicAuth = async () => {
      try {
        const a = await getJ("api/hub/public-auth");
        if (authUser) authUser.value = a.user || "";
        if (authNote) authNote.textContent = a.configured ? "" : T("control.publicLoginMissing");
      } catch (err) { if (authNote) authNote.textContent = err.message; }
    };
    loadPublicAuth();
    authForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!authUser.value.trim()) return;
      try {
        const j = await post("api/hub/public-auth", {
          user: authUser.value.trim(),
          password: authPassword.value,
        });
        if (authUser) authUser.value = j.user;
        if (authPassword) authPassword.value = "";
        if (authNote) authNote.textContent = "";
        toast(T("control.publicLoginSaved"));
      } catch (err) { toast(err.message); }
    });
  }

  // Language selector (the header toggle is global; this one is explicit in Control).
  const langSelect = $("#lang-select");
  if (langSelect) {
    langSelect.value = I18N.lang;
    langSelect.addEventListener("change", () => {
      I18N.setLang(langSelect.value).then(() => location.reload()).catch(() => location.reload());
    });
  }

  // Theme for this device (site/theme.js); the palette (⌘K) offers the same choice.
  const picks = document.querySelectorAll("[data-theme-pick]");
  const showTheme = () => picks.forEach((b) => b.setAttribute("aria-checked", String(b.dataset.themePick === HubTheme.get())));
  picks.forEach((b) => b.addEventListener("click", () => HubTheme.set(b.dataset.themePick)));
  addEventListener("hub-theme", showTheme);
  showTheme();
  // Mascots on or off for the whole mesh; off means plain nodes and icons.
  $("#pixel").setAttribute("aria-checked", String(HubTheme.pixel()));
  $("#pixel").addEventListener("click", (e) => {  // type changes in place, no reload needed
    HubTheme.setPixel(!HubTheme.pixel());
    e.currentTarget.setAttribute("aria-checked", String(HubTheme.pixel()));
  });
  $("#mascots").setAttribute("aria-checked", String(HubTheme.mascots()));
  $("#mascots").addEventListener("click", () => {
    HubTheme.setMascots(!HubTheme.mascots());
    setTimeout(() => location.reload(), 300);  // every view redraws its faces or its nodes
  });
  // A link to a section inside a fold (e.g. #aprobaciones from a notification) opens it.
  function openFold() {
    const el = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
    const fold = el && el.closest("details.ct-fold");
    if (fold) { fold.open = true; requestAnimationFrame(() => el.scrollIntoView({ behavior: "instant", block: "start" })); }
  }
  addEventListener("hashchange", openFold);
  openFold();

  // Panels of the enabled modules (each module names and draws its own).
  getJ("api/modules").then((d) => {
    $("#mod-panels").innerHTML = (d.modules || []).filter((m) => m.enabled && m.panel).map((m) => {
      const name = T(`module.${m.id}.name`) === `module.${m.id}.name` ? m.name : T(`module.${m.id}.name`);
      return `<a class="btn btn-ghost" href="${esc(m.panel)}"><i class="ph ${esc((m.nav && m.nav.icon) || "ph-arrow-up-right")}" aria-hidden="true"></i><span>${esc(name)}</span></a>`;
    }).join("");
  }).catch(() => {});

  // Version and updates: the owner's brake if a release turns out bad.
  async function loadRelease() {
    let r;
    try { r = await getJ("api/hub/release"); } catch { return; }
    const v = (x) => String(x || "").slice(0, 8);
    const newer = r.on_disk && r.on_disk !== r.version;  // e.g. right after a git pull
    $("#rel-info").textContent = [
      T("control.releaseNow", { shoal: r.shoal || "?", version: v(r.version), seq: r.seq ?? "-" }) + (r.signed ? "" : ` · ${T("control.releaseUnsigned")}`),
      r.previous ? T("control.releasePrev", { version: v(r.previous) }) : T("control.releaseNoPrev"),
      r.author ? T("control.releaseAuthor") : T("control.releaseLocal"),
      newer ? T("control.applyInfo", { version: v(r.on_disk) }) : "",
    ].filter(Boolean).join(" ");
    $("#rel-apply").hidden = !newer;
    $("#rel-apply").dataset.to = v(r.on_disk);
    $("#rel-apply").dataset.mesh = r.author ? "1" : "";
    $("#rel-rollback").hidden = !r.previous;
    $("#rel-rollback").dataset.to = v(r.previous);
    $("#rel-rollback").dataset.mesh = r.author ? "1" : "";
    $("#rel-pause").setAttribute("aria-checked", String(!!r.paused));
  }
  $("#rel-rollback").addEventListener("click", async (e) => {
    const b = e.currentTarget;
    if (!confirm(T(b.dataset.mesh ? "control.rollbackConfirmMesh" : "control.rollbackConfirm", { version: b.dataset.to }))) return;
    try { await post("api/hub/rollback", {}); toast(T("control.rollbackDone")); setTimeout(() => location.reload(), 12000); }
    catch (err) { toast(err.message); }
  });
  $("#rel-apply").addEventListener("click", async (e) => {
    const b = e.currentTarget;
    if (!confirm(T(b.dataset.mesh ? "control.applyConfirmMesh" : "control.applyConfirm", { version: b.dataset.to }))) return;
    try { await post("api/hub/apply", {}); toast(T("control.applyDone")); setTimeout(() => location.reload(), 15000); }
    catch (err) { toast(err.message); }
  });
  $("#rel-pause").addEventListener("click", async (e) => {
    const on = e.currentTarget.getAttribute("aria-checked") !== "true";
    try { await post("api/hub/updates", { paused: on }); loadRelease(); toast(T(on ? "control.updatesPausedToast" : "control.updatesResumedToast")); }
    catch (err) { toast(err.message); }
  });
  loadRelease();

  // ---------- clipboard ----------
  let clipRev = null;
  async function loadClip(force) {
    const c = await getJ("api/clip");
    $("#clip-sync").setAttribute("aria-checked", c.sync);
    $("#clip-sync-label").textContent = T("control.clipSync");
    showKeep(c.policy);
    if (!force && c.rev === clipRev) return;
    clipRev = c.rev;
    $("#clip-list").innerHTML = c.items.length ? c.items.map((i) => `
      <li class="clip-item">
        <pre>${esc(i.text.length > 600 ? i.text.slice(0, 600) + "…" : i.text)}</pre>
        <div class="clip-meta"><span>${esc(i.from)} · ${esc(ago(i.ts))}</span>
          <span class="act-mini">
            <button class="icon-btn" type="button" data-copy-id="${esc(i.id)}" aria-label="${esc(T("common.copy"))}"><i class="ph ph-copy"></i></button>
            <button class="icon-btn" type="button" data-del-id="${esc(i.id)}" aria-label="${esc(T("common.delete"))}"><i class="ph ph-trash"></i></button>
          </span></div>
      </li>`).join("") : `<li class="clip-empty">${T("control.clipEmpty")}</li>`;
    loadClip.items = c.items;
  }
  // Retention for the whole network: how many entries and for how long (0 = forever).
  const KEEP_ITEMS = [10, 25, 60, 200], KEEP_AGES = [0, 3600, 86400, 7 * 86400, 30 * 86400];
  $("#clip-max").innerHTML = KEEP_ITEMS.map((n) => `<option value="${n}">${T("control.clipItems", { n })}</option>`).join("");
  $("#clip-age").innerHTML = KEEP_AGES.map((s) => `<option value="${s}">${T("control.clipAge" + s)}</option>`).join("");
  let keepNow = null;
  function showKeep(p) {
    if (!p) { $("#clip-keep").hidden = true; return; }  // an older coordinator: no retention yet
    keepNow = p;
    $("#clip-keep").hidden = false;
    for (const [sel, list, v] of [["#clip-max", KEEP_ITEMS, p.max_items], ["#clip-age", KEEP_AGES, p.max_age]]) {
      const el = $(sel);
      if (document.activeElement === el) continue;
      if (!list.includes(v) && ![...el.options].some((o) => o.value === String(v))) el.add(new Option(String(v), String(v)));
      el.value = String(v);
    }
  }
  $("#clip-keep").addEventListener("change", async () => {
    const max = +$("#clip-max").value, age = +$("#clip-age").value, now = Date.now() / 1000;
    // Expired entries are gone from every hub for good: say how many before doing it.
    const lost = (loadClip.items || []).filter((it, i) => i >= max || (age && it.ts && it.ts < now - age)).length;
    if (lost && !confirm(T("control.clipKeepConfirm", { n: lost }))) { document.activeElement?.blur(); showKeep(keepNow); return; }
    try {
      await post("api/clip/policy", { max_items: max, max_age: age });
      toast(T("control.clipKept")); loadClip(true);
    } catch (err) { toast(err.message); }
  });
  $("#clip-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const t = $("#clip-text").value;
    if (!t.trim()) return;
    try { await post("api/clip", { text: t, from: device() }); $("#clip-text").value = ""; toast(T("control.clipShared")); loadClip(true); }
    catch (err) { toast(err.message); }
  });
  $("#clip-paste").addEventListener("click", async () => {
    try { $("#clip-text").value = await navigator.clipboard.readText(); $("#clip-text").focus(); }
    catch { toast(T("control.clipReadError")); $("#clip-text").focus(); }
  });
  $("#clip-sync").addEventListener("click", async (e) => {
    const on = e.currentTarget.getAttribute("aria-checked") !== "true";
    try { await post("api/clip/sync", { on }); e.currentTarget.setAttribute("aria-checked", on); toast(on ? T("control.syncOn") : T("control.syncOff")); }
    catch (err) { toast(err.message); }
  });
  $("#clip-list").addEventListener("click", async (e) => {
    const c = e.target.closest("[data-copy-id]"), d = e.target.closest("[data-del-id]");
    if (c) { const it = loadClip.items.find((i) => i.id === c.dataset.copyId); if (it) { await copyText(it.text); toast(T("control.clipCopied")); } }
    if (d) { try { await post("api/clip/delete", { id: d.dataset.delId }); loadClip(true); } catch (err) { toast(err.message); } }
  });

  // ---------- taildrop ----------
  let files = [];
  async function loadTargets() {
    const t = await getJ("api/taildrop/targets");
    const sel = $("#drop-to");
    sel.innerHTML = t.targets.length ? t.targets.map((x) => `<option value="${esc(x.ip)}">${esc(x.name)} ${x.ip === "host" ? T("control.taildropHost") : `(${esc(x.ip)})`}</option>`).join("")
      : `<option value="">${T("control.noTarget")}</option>`;
    const visitorName = t.visitor && (t.visitor.hostName || t.visitor.name);
    if (visitorName) $("#drop-h").innerHTML = `<i class="ph ph-paper-plane-tilt" aria-hidden="true"></i>${T("control.dropFrom", { name: esc(visitorName) })}`;
    updateDrop();
  }
  function updateDrop() {
    $("#drop-label").textContent = files.length ? TN("control.dropFile", files.length, { names: files.map((f) => f.name).join(", ") }) : T("control.drop");
    $("#drop-send").disabled = !files.length || !$("#drop-to").value;
  }
  $("#drop-input").addEventListener("change", (e) => { files = [...e.target.files]; updateDrop(); });
  const drop = $("#drop");
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => { files = [...e.dataTransfer.files]; updateDrop(); });
  $("#drop-send").addEventListener("click", async () => {
    const to = $("#drop-to").value, name = $("#drop-to").selectedOptions[0].textContent.split(" ")[0];
    $("#drop-send").disabled = true;
    for (const f of files) {
      const li = document.createElement("li");
      li.innerHTML = `<span>${esc(f.name)}</span><span class="dl-state">${T("control.sending", { name: esc(name) })}</span>`;
      $("#drop-list").prepend(li);
      try {
        const r = await fetch(`api/taildrop?to=${encodeURIComponent(to)}&name=${encodeURIComponent(f.name)}`, { method: "POST", body: f });
        const j = await r.json();
        if (!r.ok || !j.ok) throw new Error(j.output || j.error || `HTTP ${r.status}`);
        li.querySelector(".dl-state").textContent = T("control.delivered", { name });
        li.classList.add("ok");
      } catch (err) { li.querySelector(".dl-state").textContent = T("control.failed", { error: err.message }); li.classList.add("bad"); }
    }
    files = []; $("#drop-input").value = ""; updateDrop();
  });

  // ---------- actions + exit node ----------
  const pcLabels = { lock: "control.pc.lock", screen_off: "control.pc.screen_off", shutdown: "control.pc.shutdown" };
  async function loadActions() {
    const a = await getJ("api/actions");
    const icons = { lock: "ph-lock", screen_off: "ph-monitor" };
    $("#pc-actions").innerHTML = Object.entries(a.pc).map(([k, label]) =>
      `<button class="btn btn-ghost" type="button" data-pc="${k}"><i class="ph ${icons[k] || "ph-lightning"}" aria-hidden="true"></i>${esc(T(pcLabels[k] || label))}</button>`).join("");
  }
  $("#pc-actions").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-pc]");
    if (!b) return;
    const label = T(pcLabels[b.dataset.pc] || b.textContent.trim());
    if (!confirm(T("control.actionConfirm", { action: label }))) return;
    try {
      const r = await post(`api/actions/pc/${b.dataset.pc}`);
      toast(r.pending ? T("control.actionPending") : T("control.actionDone"));
      if (r.pending) loadApprovals().catch(() => {});
    } catch (err) { toast(err.message); }
  });
  async function loadExit() {
    const x = await getJ("api/exitnode");
    $("#exit").setAttribute("aria-checked", x.advertised);
    $("#exit-note").textContent = !x.advertised ? T("control.exitNote")
      : x.approved ? T("control.exitApproved")
      : T("control.exitPending");
  }
  $("#exit").addEventListener("click", async (e) => {
    const sw = e.currentTarget, on = sw.getAttribute("aria-checked") !== "true";
    sw.disabled = true;
    try { await post("api/exitnode", { on }); toast(on ? T("control.exitOffered") : T("control.exitRemoved")); }
    catch (err) { toast(T("control.exitFailed", { error: err.message })); }
    sw.disabled = false; loadExit();
  });

  // ---------- monitor ----------
  async function loadMonitor() {
    const [m, fed] = await Promise.all([
      getJ("api/monitor"),
      getJ("api/monitor/federation").catch(() => ({ peers: {} })),
    ]);
    const seen = fed.peers || {};
    const names = Object.keys(m);
    if (!names.length) { $("#monitor").innerHTML = `<p class="clip-empty">${T("control.monitorWait")}</p>`; return; }
    $("#monitor").innerHTML = names.map((n, i) => {
      const s = m[n].samples, last = s[s.length - 1] || [];
      const up = s.filter((x) => x[1]).length, pct = s.length ? Math.round((up / s.length) * 100) : 0;
      const lat = s.filter((x) => x[2] != null).map((x) => x[2]);
      const med = lat.length ? lat.slice().sort((a, b) => a - b)[Math.floor(lat.length / 2)] : null;
      const multi = seen[n] && seen[n].hubs > 1 ? T("control.monitorHubs", { n: seen[n].hubs }) : "";
      const isDirect = last[3] === "direct" || last[3] === "directa";
      const detail = last[1] ? T("control.monitorOnline", { detail: isDirect ? T("control.direct") : last[3] === "self" ? T("control.monitorSelf") : (last[3] || ""), latency: last[2] != null ? ` · ${Math.round(last[2])} ms` : "" }) : T("control.monitorOffline");
      return `<div class="mon-row">
        <div class="mon-name"><strong>${esc(n)}</strong><span>${esc(detail)}${esc(multi)}</span></div>
        <canvas class="mon-canvas" data-i="${i}" aria-label="${esc(T("control.monitorAvailability", { name: n }))}"></canvas>
        <div class="mon-stats"><b>${T("control.monitorPct", { pct })}</b>${med != null ? T("control.monitorMedian", { ms: Math.round(med) }) : ""}</div>
      </div>`;
    }).join("");
    names.forEach((n, i) => drawMon(document.querySelector(`.mon-canvas[data-i="${i}"]`), m[n].samples));
  }
  function drawMon(cv, s) {
    const dpr = Math.min(2, devicePixelRatio || 1), w = cv.clientWidth, h = cv.clientHeight;
    cv.width = w * dpr; cv.height = h * dpr;
    const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const now = Date.now() / 1000, span = 86400, x = (t) => w - ((now - t) / span) * w;
    ctx.fillStyle = css("--line"); ctx.fillRect(0, h - 10, w, 2);
    const lat = s.map((p) => p[2]).filter((v) => v != null), hi = Math.max(50, ...lat);
    for (const p of s) {
      const px = x(p[0]);
      ctx.fillStyle = p[1] ? css("--accent") : css("--down");
      ctx.fillRect(px, h - 10, Math.max(1.5, w / 1440), 2);
      if (p[2] != null) {
        ctx.fillStyle = (p[3] === "direct" || p[3] === "directa") ? css("--accent") : css("--ink-3");
        const bh = Math.max(1, (p[2] / hi) * (h - 18));
        ctx.globalAlpha = 0.65; ctx.fillRect(px, h - 14 - bh, Math.max(1.5, w / 1440), bh); ctx.globalAlpha = 1;
      }
    }
  }

  // ---------- hub mesh (masterless) ----------
  const hubIcon = (h) => {
    const k = `${h.platform || ""} ${h.os || ""}`.toLowerCase();
    return k.includes("android") ? "ph-device-mobile" : k.includes("raspberry") ? "ph-cpu" : k.includes("server") ? "ph-hard-drives"
      : k.includes("mac") ? "ph-apple-logo" : k.includes("linux") ? "ph-linux-logo" : "ph-desktop-tower";
  };
  const PRIOS = [[100, "control.priority.100"], [50, "control.priority.50"], [10, "control.priority.10"], [0, "control.priority.0"]];
  async function loadNet() {
    const v = await getMeshView();
    netMe = v.me || "";
    if (netMe) document.title = T("control.title", { name: netMe });
    const live = v.hubs.filter((h) => !h.removed), on = live.filter((h) => h.online).length;
    const here = v.hubs.find((h) => h.here);
    showInstallCmds(here);
    const ver = v.release && v.release.version;
    const stale = ver ? live.filter((h) => h.online && h.version && h.version !== ver).length : 0;
    const code = ver ? T("control.netCode", { version: ver.slice(0, 7) }) : "";
    const staleText = stale ? T("control.netUpdating", { n: stale }) : ver ? T("control.netAllCurrent") : "";
    $("#net-meta").textContent = T("control.netMeta", { on, total: live.length, code, stale: staleText });
    $("#net").innerHTML = v.hubs.map((h) => {
      const tags = [h.coordinator && `<span class="net-tag coord">${T("control.netCoord")}</span>`, h.here && `<span class="net-tag">${T("control.netHere")}</span>`,
        h.removed ? `<span class="net-tag down">${T("control.netRemoved")}</span>` : !h.online && `<span class="net-tag down">${T("control.netOffline")}</span>`,
        h.capable && !h.coordinator && `<span class="net-tag">${T("control.netRelay")}</span>`].filter(Boolean).join("");
      const hv = h.version ? (h.version === (v.release && v.release.version) ? T("control.netUpToDate") : T("control.netCodeShort", { version: h.version.slice(0, 7) })) : "—";
      const opts = PRIOS.some(([n]) => n === h.priority) ? PRIOS : [[h.priority, String(h.priority)], ...PRIOS];
      return `<div class="net-row${h.online && !h.removed ? "" : " off"}">
        ${window.HubMascot && HubMascot.enabled() ? HubMascot.svg(h.pub || h.name, { size: 30, state: !h.online || h.removed ? "off" : h.coordinator ? "coord" : "ok" }) : `<i class="ph ${hubIcon(h)}" aria-hidden="true"></i>`}
        <div class="net-name">${webUrl(h.url) ? `<a href="${esc(h.url)}">${esc(h.name)}</a>` : `<b>${esc(h.name)}</b>`}<div class="net-tags">${tags}</div></div>
        <div class="net-info"><b>${esc(h.platform || h.kind || "")}</b> · ${esc(h.ip || "")}<br>${h.capable ? T("control.netCanCoordinate") : T("control.netNode")} · ${esc(hv)}${h.online || !h.seen ? "" : T("control.netSeen", { ago: ago(h.seen) })}</div>
        <label class="net-prio">${T("common.priority")}
          <select class="select" data-prio="${esc(h.name)}" ${h.removed ? "disabled" : ""} aria-label="${esc(T("control.netPriorityAria", { name: h.name }))}">
            ${opts.map(([n, t]) => `<option value="${esc(n)}"${n === h.priority ? " selected" : ""}>${esc(T(t))}</option>`).join("")}
          </select></label>
        ${h.here ? "<span></span>" : `<button class="icon-btn" type="button" data-hub="${esc(h.name)}" data-act="${h.removed ? "restore" : "remove"}"
            aria-label="${esc(T(h.removed ? "control.netRestoreAria" : "control.netRemoveAria", { name: h.name }))}"><i class="ph ${h.removed ? "ph-arrow-counter-clockwise" : "ph-trash"}" aria-hidden="true"></i></button>`}
      </div>`;
    }).join("");
  }
  $("#net").addEventListener("change", async (e) => {
    const s = e.target.closest("[data-prio]");
    if (!s) return;
    try { await post("api/mesh/hub", { name: s.dataset.prio, priority: Number(s.value) }); toast(T("control.netPrioritySaved")); }
    catch (err) { toast(T("control.exitFailed", { error: err.message })); }
    invalidateView();
    loadNet().catch(() => {});
  });
  $("#net").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-hub]");
    if (!b) return;
    const remove = b.dataset.act === "remove";
    if (remove && !confirm(T("control.netRemoveConfirm", { name: b.dataset.hub }))) return;
    try {
      const res = await post("api/mesh/hub", { name: b.dataset.hub, [remove ? "remove" : "restore"]: true });
      toast(res && res.pending ? T("control.netRemovePending") : remove ? T("control.netRemovedToast") : T("control.netRestored"));
    }
    catch (err) { toast(T("control.exitFailed", { error: err.message })); }
    invalidateView();
    loadNet().catch(() => {});
    loadApprovals().catch(() => {});
  });
  function showInstallCmds(here) {
    const c = installCmds(here);
    $("#cmd-win").textContent = c.win; $("#cmd-sh").textContent = c.sh; $("#cmd-tx").textContent = c.tx;
  }
  showInstallCmds(null);  // refined with this hub's tailnet address once the mesh view arrives
  document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    await copyText($("#" + b.dataset.copy).textContent); toast(T("control.copiedCommand"));
  }));

  // ---------- quorum approvals ----------
  const actionLabel = (a) => T("control.approval." + a) === "control.approval." + a ? a : T("control.approval." + a);
  async function loadApprovals() {
    const a = await getJ("api/mesh/approvals");
    const v = a.view || {};
    const pend = v.pending || [];
    const open = pend.filter((p) => !p.result && !p.done && !(p.denied || []).length);
    const meta = $("#appr-meta");
    meta.textContent = open.length ? TN("control.approvalsPending", open.length) : T("control.approvalsNothing");
    meta.classList.toggle("appr-warn", !!open.length);
    if (open.length) $("#fold-advanced").open = true; // something waits for a decision: do not hide it
    $("#appr-list").innerHTML = pend.length ? pend.map((p) => {
      const res = p.result, args = p.args || {};
      const done = p.done || res;
      const badge = res ? (res.ok ? T("control.approvalDone") : T("control.approvalError")) : p.done ? T("control.approvalQuorum") : `${(p.approvals || []).length}/${p.need}`;
      const what = p.action === "mesh_remove" ? `hub ${args.name || ""}` : args.hub || p.origin;
      const detail = res ? (res.ok ? T("control.approvalExecuted") : (res.error || T("control.approvalNotExecuted")))
        : p.done ? T("control.approvalWaiting")
          : T("control.approvalDetail", { what, by: p.by || p.origin, n: (p.approvals || []).length, need: p.need });
      const can = !res && !p.done && p.origin !== netMe;
      return `<div class="appr">
        <div class="appr-main"><b>${esc(actionLabel(p.action))}</b><small>${esc(detail)}</small></div>
        <span class="appr-pill${done ? " done" : ""}">${esc(badge)}</span>
        ${can ? `<div class="appr-actions">
          <button class="btn btn-primary btn-xs" type="button" data-appr="${esc(p.id)}" data-do="approve"><i class="ph ph-check" aria-hidden="true"></i>${T("control.approvalConfirm")}</button>
          <button class="btn btn-ghost btn-xs" type="button" data-appr="${esc(p.id)}" data-do="deny">${T("control.approvalReject")}</button></div>` : ""}
      </div>`;
    }).join("") : `<p class="empty-line">${T("control.approvalEmpty")}</p>`;
    const q = v.quorum || {};
    $("#quorum").innerHTML = ["terminal", "mesh_remove", "shutdown"].map((k) => `
      <label class="quorum-row"><span>${esc(actionLabel(k))}</span>
        <select class="select" data-quorum="${esc(k)}" aria-label="${esc(T("control.approvalQuorumAria", { action: actionLabel(k) }))}">
          ${[1, 2, 3].map((n) => `<option value="${n}"${Number(q[k] || 1) === n ? " selected" : ""}>${n === 1 ? T("control.approvalOne") : T("control.approvalHubs", { n })}</option>`).join("")}
        </select></label>`).join("");
  }
  $("#appr-list").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-appr]");
    if (!b) return;
    try {
      await post(`api/mesh/approvals/${encodeURIComponent(b.dataset.appr)}/${b.dataset.do}`, {});
      toast(b.dataset.do === "approve" ? T("control.approvalConfirmed") : T("control.approvalRejected"));
    } catch (err) { toast(err.message); }
    invalidateView();
    loadApprovals().catch(() => {}); loadNet().catch(() => {});
  });
  $("#quorum").addEventListener("change", async (e) => {
    const s = e.target.closest("[data-quorum]");
    if (!s) return;
    try { await post("api/mesh/settings", { quorum: { [s.dataset.quorum]: Number(s.value) } }); toast(T("control.approvalUpdated")); }
    catch (err) { toast(err.message); }
    loadApprovals().catch(() => {});
  });

  // ---------- other tailnets ----------
  async function loadPeers() {
    const v = await getMeshView();
    netMe = v.me || netMe;
    const ps = v.peers || [];
    $("#peers").innerHTML = ps.length ? ps.map((p) => `<div class="peer">
      <div class="peer-main"><b>${esc(p.name || p.domain)}</b>
        <small>${esc(p.domain)} · ${esc(p.url || "")}<br>${esc(T("control.peerShares", { list: (p.allow || []).join(", ") }))}</small></div>
      <button class="icon-btn" type="button" data-peer="${esc(p.domain)}" aria-label="${esc(T("control.peerRemoveAria", { domain: p.domain }))}"><i class="ph ph-trash" aria-hidden="true"></i></button>
    </div>`).join("") : `<p class="empty-line">${T("control.peerEmpty")}</p>`;
  }
  $("#peer-add").addEventListener("click", async () => {
    const text = $("#peer-invite").value.trim();
    if (!text) return toast(T("control.peerPasteFirst"));
    let raw;
    try { raw = JSON.parse(text); } catch { return toast(T("control.peerInvalidJson")); }
    try {
      await post("api/mesh/peer", { invite: raw.invite || raw });
      $("#peer-invite").value = "";
      toast(T("control.peerPaired"));
      invalidateView();
      loadPeers().catch(() => {}); loadNet().catch(() => {});
    } catch (err) { toast(T("control.exitFailed", { error: err.message })); }
  });
  $("#peer-copy").addEventListener("click", async () => {
    try {
      const j = await getJ("api/mesh/invite");
      if (!j.invite) throw new Error(T("control.peerNoUrl"));
      await copyText(JSON.stringify(j.invite, null, 1));
      toast(T("control.peerCopied"));
    } catch (err) { toast(T("control.exitFailed", { error: err.message })); }
  });
  $("#peers").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-peer]");
    if (!b || !confirm(T("control.peerUnpairConfirm", { domain: b.dataset.peer }))) return;
    try { await post("api/mesh/peer/remove", { domain: b.dataset.peer }); toast(T("control.peerUnpaired")); }
    catch (err) { toast(T("control.exitFailed", { error: err.message })); }
    invalidateView();
    loadPeers().catch(() => {}); loadNet().catch(() => {});
  });

  // ---------- federated catalog ----------
  let catalog = [];
  function paintCatalog() {
    $("#catalog").innerHTML = catalog.length ? catalog.map((s) => {
      const key = String(s.key || "");
      // the service's own address when it has one (option a): opened there, not inside the hub's origin
      const direct = webUrl(s.url);
      const href = direct ? s.url : `api/goto/${key}${key.endsWith("/") ? "" : "/"}`;
      const ms = s.ms != null ? ` · ${Math.round(s.ms)} ms` : "";
      return `<div class="cat-row">
      <div class="cat-main"><div class="cat-title"><a href="${esc(href)}"${direct ? ' target="_blank" rel="noopener"' : ""}>${esc(s.name)}</a>${s.custom ? `<span class="cat-tag">${esc(T("control.catalogCustom"))}</span>` : ""}</div>
        ${s.about ? `<span class="cat-about">${esc(s.about)}</span>` : ""}
        <small>${esc(T("control.catalogMeta", { key, up: s.up_hubs, total: (s.hubs || []).length, hub: s.hub, ms }))}</small></div>
      <span class="appr-pill${s.online ? " done" : ""}">${s.online ? T("control.catalogOnline") : T("control.catalogOffline")}</span>
      ${caps.includes("labels") ? `<button class="icon-btn" type="button" data-label-edit="${esc(key)}" aria-label="${esc(T("labels.editFor", { name: s.name }))}" title="${esc(T("labels.edit"))}"><i class="ph ph-pencil-simple"></i></button>` : ""}
    </div>`;
    }).join("") : `<p class="empty-line">${T("control.catalogEmpty")}</p>`;
  }
  async function loadCatalog() {
    const c = await getJ("api/catalog");
    catalog = c.services || [];
    paintCatalog();
  }
  $("#catalog").addEventListener("click", (e) => {
    const b = e.target.closest("[data-label-edit]");
    if (!b) return;
    const s = catalog.find((x) => String(x.key) === b.dataset.labelEdit);
    if (s && window.HUB_LABELS) HUB_LABELS.open({ id: s.key, name: s.name, about: s.about }, (j) => {
      const cur = catalog.find((x) => String(x.key) === String(j.id));
      if (cur && j.service) {
        Object.assign(cur, j.service);
        paintCatalog();
      }
      loadCatalog().catch(() => {});
    });
  });

  // ---------- replicated files ----------
  const bytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
  async function loadBlobs() {
    const b = await getJ("api/blobs");
    const list = b.blobs || [];
    $("#blob-meta").textContent = TN("control.blobMeta", list.length, { rf: b.rf || 2 });
    $("#blobs").innerHTML = list.length ? list.map((x) => `<li class="blob-row">
      <i class="ph ph-file" aria-hidden="true" style="color:var(--accent);font-size:19px"></i>
      <div class="blob-main"><a href="api/blob/${esc(x.sha)}">${esc(x.name || x.sha.slice(0, 12))}</a>
        <small>${esc(bytes(Number(x.size) || 0))} · ${(x.hubs || []).length} hub${(x.hubs || []).length === 1 ? "" : "s"} · ${esc((x.hubs || []).join(", "))}</small></div>
      <button class="icon-btn" type="button" data-blob-del="${esc(x.sha)}" aria-label="${esc(T("control.blobDeleteAria"))}"><i class="ph ph-trash" aria-hidden="true"></i></button>
    </li>`).join("") : `<li class="empty-line">${T("control.blobEmpty")}</li>`;
  }
  $("#blob-input").addEventListener("change", async (e) => {
    for (const f of e.target.files) {
      toast(T("control.blobUploading", { name: f.name }));
      try {
        const r = await fetch(`api/blob?name=${encodeURIComponent(f.name)}`, {
          method: "POST", body: f, headers: { "X-Hub-Blob-Name": encodeURIComponent(f.name), "Content-Type": "application/octet-stream" },
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
      } catch (err) { toast(T("control.blobUploadFailed", { name: f.name, error: err.message })); }
    }
    e.target.value = "";
    toast(T("control.blobUploaded"));
    loadBlobs().catch(() => {});
  });
  $("#blobs").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-blob-del]");
    if (!b || !confirm(T("control.blobDeleteConfirm"))) return;
    try { await post("api/blobs/delete", { sha: b.dataset.blobDel }); toast(T("control.blobDeleted")); }
    catch (err) { toast(err.message); }
    loadBlobs().catch(() => {});
  });

  // ---------- startup ----------
  loadClip(true).catch(() => {});
  if (caps.includes("taildrop")) loadTargets().catch(() => { $("#drop-to").innerHTML = `<option value="">${T("control.noTarget")}</option>`; });
  loadActions().catch(() => {});
  if (caps.includes("exitnode")) loadExit().catch(() => {});
  loadMonitor().catch(() => {});
  loadNet().catch(() => { $("#net").innerHTML = `<p class="clip-empty">${T("control.network")}</p>`; });
  loadApprovals().catch(() => { $("#appr-list").innerHTML = `<p class="empty-line">${T("control.approvalEmpty")}</p>`; });
  loadPeers().catch(() => {});
  loadCatalog().catch(() => {});
  loadBlobs().catch(() => {});
  setInterval(() => { if (!document.hidden) loadNet().catch(() => {}); }, 30000);
  setInterval(() => { if (!document.hidden) loadApprovals().catch(() => {}); }, 10000);
  setInterval(() => { if (!document.hidden) loadClip(false).catch(() => {}); }, 3000);
  setInterval(() => { if (!document.hidden) loadMonitor().catch(() => {}); }, 60000);
  setInterval(() => { if (!document.hidden) loadCatalog().catch(() => {}); }, 60000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { loadClip(true).catch(() => {}); if (caps.includes("exitnode")) loadExit().catch(() => {}); loadApprovals().catch(() => {}); loadBlobs().catch(() => {}); }
  });
  addEventListener("resize", () => loadMonitor().catch(() => {}));
})();
