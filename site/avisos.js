// Per-device Web Push notifications. The hub stores the subscription in state/subscriptions.json.
(async () => {
  "use strict";
  await I18N.ready;
  const T = (k, v) => I18N.t(k, v);
  const TN = (k, n, v) => I18N.tn(k, n, v);
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let cfg = null, reg = null, mine = null;

  const toast = (m) => { const t = $("#toast"); t.textContent = m; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 1800); };
  const note = (m, err = false) => { const n = $("#note"); n.textContent = m; n.classList.toggle("err", err); };
  const post = async (url, body) => {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  };
  const b64ToBytes = (b64) => {
    const pad = "=".repeat((4 - (b64.length % 4)) % 4);
    const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  };
  const label = () => {
    const ua = navigator.userAgent;
    const os = /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Windows/.test(ua) ? "Windows" : /Mac/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : T("common.device");
    const br = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : T("common.browser");
    return `${os} · ${br}`;
  };
  const sid = async (endpoint) => {
    const h = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint));
    return [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
  };
  const eventLabel = (e) => {
    const key = `notifications.event.${e.key}`;
    const translated = T(key);
    return translated === key ? e.label : translated;
  };
  const eventAbout = (e) => {
    const key = `notifications.event.${e.key}.about`;
    const translated = T(key);
    return translated === key ? e.about : translated;
  };

  // Events of an optional module show only while it is enabled on some hub of the mesh.
  let running = null;
  async function modulesInMesh() {
    const caps = new Set((window.__hubCaps && window.__hubCaps.caps) || []);
    try {
      const j = await (await fetch("api/nodes", { cache: "no-store" })).json();
      for (const n of j.nodes || []) for (const c of n.caps || []) caps.add(c);
      const mods = await (await fetch("api/modules", { cache: "no-store" })).json();
      return new Set((mods.modules || []).filter((m) => (m.caps || []).some((c) => caps.has(c))).map((m) => m.id));
    } catch { return null; } // unknown: show everything
  }
  function renderToggles() {
    const on = mine?.events || {};
    $("#toggles").innerHTML = cfg.events.filter((e) => !e.module || !running || running.has(e.module)).map((e) => `
      <li class="toggle frame"><div class="in">
        <span class="t-name" id="t-${esc(e.key)}">${esc(eventLabel(e))}</span>
        <span class="t-about">${esc(eventAbout(e))}</span>
        <button class="switch" type="button" role="switch" data-ev="${esc(e.key)}" aria-labelledby="t-${esc(e.key)}"
          aria-checked="${mine ? !!on[e.key] : e.default}" ${mine ? "" : "disabled"}></button>
      </div></li>`).join("");
  }
  function renderLog() {
    const fmt = (ts) => new Date(ts * 1000).toLocaleString(I18N.locale, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
    $("#log").innerHTML = cfg.log.length
      ? cfg.log.map((l) => `<li><time>${esc(fmt(l.ts))}</time><span><b>${esc(l.title)}</b> ${esc(l.body)} (${esc(TN("notifications.sentCount", l.sent))})</span></li>`).join("")
      : `<li><time>-</time><span>${T("notifications.emptyLog")}</span></li>`;
    $("#devices").textContent = TN("notifications.device", cfg.devices.length, { n: cfg.devices.length });
  }
  function renderButtons() {
    $("#enable").hidden = !!mine;
    $("#test").hidden = $("#disable").hidden = !mine;
  }

  async function init() {
    cfg = await (await fetch("api/notify", { cache: "no-store" })).json();
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      note(T("notifications.unsupported"), true);
      $("#enable").disabled = true;
    } else {
      reg = await navigator.serviceWorker.register("sw.js", { scope: "./" });
      const sub = await reg.pushManager.getSubscription();
      if (sub) mine = (await (await fetch(`api/notify/sub/${await sid(sub.endpoint)}`, { cache: "no-store" })).json()) || null;
      if (mine && !mine.id) mine = null;
      if (!cfg.owner) { note(T("notifications.ownerOnly"), true); $("#enable").disabled = true; }
      else if (Notification.permission === "denied") note(T("notifications.blocked"), true);
      else if (mine) note(T("notifications.enabled", { label: mine.label }));
    }
    renderToggles(); renderLog(); renderButtons();
    if (!running) modulesInMesh().then((r) => { if (r) { running = r; renderToggles(); } });
  }

  $("#enable").addEventListener("click", async () => {
    try {
      if ((await Notification.requestPermission()) !== "granted") return note(T("notifications.noPermission"), true);
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(cfg.publicKey) });
      mine = await post("api/notify/subscribe", { subscription: sub.toJSON(), label: label() });
      note(T("notifications.enabled", { label: mine.label }));
      cfg = await (await fetch("api/notify", { cache: "no-store" })).json();
      renderToggles(); renderLog(); renderButtons(); toast(T("notifications.activated"));
    } catch (e) { note(T("notifications.loadError", { error: e.message }), true); }
  });
  $("#test").addEventListener("click", async () => {
    try { const r = await post("api/notify/test", { id: mine.id }); toast(r.sent ? T("notifications.testSent") : T("notifications.testFailed")); }
    catch (e) { note(e.message, true); }
  });
  $("#disable").addEventListener("click", async () => {
    try {
      await post("api/notify/unsubscribe", { id: mine.id });
      const sub = await reg.pushManager.getSubscription(); if (sub) await sub.unsubscribe();
      mine = null; note(T("notifications.disabled"));
      cfg = await (await fetch("api/notify", { cache: "no-store" })).json();
      renderToggles(); renderLog(); renderButtons();
    } catch (e) { note(e.message, true); }
  });
  $("#toggles").addEventListener("click", async (e) => {
    const sw = e.target.closest(".switch");
    if (!sw || sw.disabled) return;
    const on = sw.getAttribute("aria-checked") !== "true";
    sw.setAttribute("aria-checked", on);
    try { mine = await post("api/notify/event", { id: mine.id, event: sw.dataset.ev, on }); }
    catch (err) { sw.setAttribute("aria-checked", !on); note(err.message, true); }
  });

  init().catch((e) => note(T("notifications.loadError", { error: e.message }), true));
})();
