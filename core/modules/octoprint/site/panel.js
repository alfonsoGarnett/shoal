// Printer panel: reads api/modules/octoprint/* (the hub proxies OctoPrint; the API key never reaches the browser).
(async () => {
  "use strict";
  await I18N.ready;
  const T = (k, v) => I18N.t(k, v);
  const $ = (s) => document.querySelector(s);
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const STATES = {
    Printing: "printer.state.Printing", Operational: "printer.state.Operational", Paused: "printer.state.Paused",
    Pausing: "printer.state.Pausing", Resuming: "printer.state.Resuming", Cancelling: "printer.state.Cancelling",
    Finishing: "printer.state.Finishing", Starting: "printer.state.Starting", Connecting: "printer.state.Connecting",
    Offline: "printer.state.Offline", Closed: "printer.state.Closed", "Offline after error": "printer.state.Offline after error",
    Error: "printer.state.Error",
  };
  const dur = (s) => {
    if (s == null) return "-";
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m} min`;
  };

  (window.__hubCaps ? Promise.resolve(window.__hubCaps) : fetch("api/caps", { cache: "no-store" }).then((r) => r.json())).then((c) => {
    if (c.name) document.title = T("printer.title", { name: c.name });
  }).catch(() => {});

  async function load() {
    let st;
    try {
      st = await (await fetch("api/modules/octoprint/state", { cache: "no-store" })).json();
    } catch {
      $("#lede").textContent = T("printer.noHub");
      return;
    }
    if (!st.reachable) {
      $("#state").textContent = T("printer.noOcto");
      $("#state").classList.add("err");
      $("#lede").textContent = T("printer.octoDown");
      return;
    }
    const s = st.state || T("printer.unknown");
    const busy = ["Printing", "Paused", "Pausing", "Resuming", "Cancelling", "Finishing"].includes(s);
    const isErr = s.startsWith("Error") || s.startsWith("Offline after");
    $("#state").textContent = STATES[s] ? T(STATES[s]) : s;
    $("#state").classList.toggle("err", isErr);
    $("#file").textContent = st.file ? st.file : T("printer.noFile");
    $("#lede").textContent = busy ? T("printer.busy") : isErr ? T("printer.errorStop") : s === "Operational" ? T("printer.ready") : T("printer.disconnected");

    const pct = busy && st.completion != null ? st.completion : null;
    $("#pct").innerHTML = `${pct == null ? "-" : pct.toFixed(pct < 10 ? 1 : 0)}<small>%</small>`;
    $("#bar").style.width = `${pct ?? 0}%`;
    $("#bar").setAttribute("aria-valuenow", pct ?? 0);
    $("#t-el").textContent = busy ? dur(st.printTime) : "-";
    $("#t-left").textContent = busy ? dur(st.printTimeLeft) : "-";
    $("#t-eta").textContent = busy && st.printTimeLeft
      ? new Date(Date.now() + st.printTimeLeft * 1000).toLocaleTimeString(I18N.locale, { hour: "2-digit", minute: "2-digit" }) : "-";
    const err = $("#err");
    err.hidden = !st.error;
    err.textContent = st.error || "";

    const t = st.tool || {};
    $("#temp").innerHTML = `${t.actual == null ? "-" : t.actual.toFixed(1)}<small>°C</small>`;
    $("#target").textContent = t.target ? T("printer.target", { temp: t.target.toFixed(0) }) : T("printer.heaterOff");

    $("#k-conn").textContent = st.port ? `${st.port}` : T("printer.disconnected");
    $("#k-est").textContent = st.estimated ? dur(st.estimated) : "-";
    $("#k-fil").textContent = st.filament_mm ? T("printer.filamentValue", { n: (st.filament_mm / 1000).toFixed(2) }) : "-";
    $("#k-upd").textContent = new Date((st.at || Date.now() / 1000) * 1000).toLocaleTimeString(I18N.locale);
  }

  // Temperature history: solid actual line, dashed target line
  async function spark() {
    let pts = [];
    try { pts = (await (await fetch("api/modules/octoprint/history", { cache: "no-store" })).json()).points || []; } catch { return; }
    const cv = $("#spark"), dpr = Math.min(2, devicePixelRatio || 1);
    const w = cv.clientWidth, h = cv.clientHeight;
    cv.width = w * dpr; cv.height = h * dpr;
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (pts.length < 2) {
      ctx.fillStyle = css("--ink-3"); ctx.font = `500 12px ${css("--mono")}`;
      ctx.fillText(T("printer.collecting"), 0, h / 2);
      return;
    }
    const vals = pts.flatMap((p) => [p[1], p[2] || p[1]]);
    const lo = Math.min(...vals) - 3, hi = Math.max(...vals) + 3;
    const x = (i) => (i / (pts.length - 1)) * w, y = (v) => h - ((v - lo) / (hi - lo)) * (h - 8) - 4;
    ctx.lineWidth = 1; ctx.strokeStyle = css("--ink-3"); ctx.setLineDash([4, 4]);
    if (pts.some((p) => p[2])) { // target (0 = off: drawn on the bottom edge)
      ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo : ctx.moveTo).call(ctx, x(i), y(p[2] || lo))); ctx.stroke();
    }
    ctx.setLineDash([]); ctx.lineWidth = 2; ctx.strokeStyle = css("--accent");
    ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo : ctx.moveTo).call(ctx, x(i), y(p[1]))); ctx.stroke();
    ctx.fillStyle = css("--ink-3"); ctx.font = `500 11px ${css("--mono")}`;
    ctx.fillText(`${hi.toFixed(0)}°`, 4, 12); ctx.fillText(`${lo.toFixed(0)}°`, 4, h - 4);
  }

  // Camera: snapshot every 5 s when go2rtc sends an image
  let camUrl;
  async function cam() {
    try {
      const r = await fetch("api/modules/octoprint/snapshot", { cache: "no-store" });
      if (r.status !== 200) return;
      const blob = await r.blob();
      if (camUrl) URL.revokeObjectURL(camUrl);
      camUrl = URL.createObjectURL(blob);
      $("#cam").innerHTML = `<img alt="${T("printer.cameraAlt")}" src="${camUrl}">`;
    } catch { /* the camera is optional */ }
  }

  // Actions (no heating: that requires being next to the printer)
  const toast = (m) => { const t = $("#toast"); t.textContent = m; t.classList.add("show"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 1800); };
  $("#pp-actions").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-pa]");
    if (!b) return;
    if (b.dataset.confirmI18n && !confirm(T(b.dataset.confirmI18n))) return;
    b.disabled = true;
    try {
      const r = await fetch(`api/modules/octoprint/actions/${b.dataset.pa}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      toast(T("printer.sent"));
      setTimeout(load, 800);
    } catch (err) { toast(err.message); }
    b.disabled = false;
  });

  load(); spark(); cam();
  setInterval(() => { if (!document.hidden) load(); }, 3000);
  setInterval(() => { if (!document.hidden) spark(); }, 15000);
  setInterval(() => { if (!document.hidden) cam(); }, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { load(); spark(); } });
})();
