// Tailnet hub index: read data.json from the core and render the live view.
(async () => {
  "use strict";
  await I18N.ready;
  const T = (k, v) => I18N.t(k, v);
  const TN = (k, n, v) => I18N.tn(k, n, v);
  const $ = (s, el = document) => el.querySelector(s);
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  let data = null;
  let lastFetch = 0;
  const selected = new Set();

  function updatePublishBar() {
    const bar = $("#publish-bar"), count = $("#publish-count"), go = $("#publish-open span");
    if (!bar) return;
    bar.classList.toggle("show", selected.size > 0);
    bar.inert = selected.size === 0;
    document.body.classList.toggle("has-selection", selected.size > 0);
    if (count) count.textContent = TN("index.publishSelected", selected.size);
    if (go) go.textContent = selected.size === 1 ? T("index.publishActionOne") : T("index.publishAction");
  }

  function syncSelection() {
    if (data) {
      const ids = new Set((data.routes || []).map((r) => String(r.id)));
      for (const id of [...selected]) if (!ids.has(id)) selected.delete(id);
    }
    document.querySelectorAll("#route-grid [data-route]").forEach((card) => {
      const on = selected.has(String(card.dataset.route));
      card.classList.toggle("selected", on);
      if (card.hasAttribute("aria-pressed")) card.setAttribute("aria-pressed", String(on));
      card.querySelector("[data-share]")?.setAttribute("aria-pressed", String(on));
    });
    updatePublishBar();
  }

  function toggleSelected(id) {
    id = String(id);
    if (selected.has(id)) selected.delete(id);
    else if (selected.size >= 2) { toast(T("index.publishLimit")); return; }
    else selected.add(id);
    syncSelection();
  }

  // ---------- utilities ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const ago = (iso) => I18N.ago(iso);
  const osIcon = (os) => ({ windows: "ph-windows-logo", android: "ph-android-logo", ios: "ph-apple-logo", macos: "ph-apple-logo", linux: "ph-linux-logo" }[(os || "").toLowerCase()] || "ph-cpu");

  let toastT;
  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastT);
    toastT = setTimeout(() => t.classList.remove("show"), 1600);
  }
  async function copy(text, label = T("index.copied")) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = Object.assign(document.createElement("textarea"), { value: text });
      document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
    }
    toast(label);
  }

  // ---------- render ----------
  function render(d) {
    const live = d.routes.filter((r) => r.up && r.path !== "/hub" && !r.hidden).length;
    const total = d.routes.filter((r) => r.path !== "/hub" && !r.hidden).length;
    const online = [d.self, ...d.peers].filter((p) => p.online).length;

    const host = d.self.name || T("common.device");
    document.title = T("index.title", { name: host });
    const parts = host.match(/^([A-Z]?[a-z0-9]+)([A-Z].*)$/); // CamelCaseName -> Camel / CaseName
    $("#host").innerHTML = parts ? `${esc(parts[1])}<span class="dim">${esc(parts[2])}</span>` : esc(host);
    $("#lede").innerHTML = T("index.lede", { live, total, online, devices: TN("index.device", online) });
    $("#tailnet").textContent = d.tailnet;
    const ver = String(d.version || "");
    const verLabel = /^(core|agent)/i.test(ver) ? ver : ver ? `Tailscale ${ver}` : "";
    $("#foot-ver").textContent = [d.self.dns, verLabel].filter(Boolean).join(" · ");
    const ip = (d.self.ips || [])[0] || "";
    const ipBtn = $("#copy-ip");
    ipBtn.querySelector("span").textContent = ip;
    ipBtn.onclick = () => copy(ip, T("index.copyIpToast", { ip }));

    $("#radar-cap").textContent = T("index.radarCaption", { devices: d.peers.length + 1, routes: total });

    paintRoutes(d);
    $("#device-list").innerHTML = [d.self, ...d.peers].map((p, i) => deviceRow(p, i)).join("");
    observe();
    painted = true;
    if (typeof loadServices === "function") loadServices();
    drawRadar.data = d;
    if (reduce) drawRadar();
  }

  // Cards painted after the first load appear in place: the entrance plays once per visit.
  let painted = false;
  let showHidden = false;
  try { showHidden = localStorage.getItem("hub.showHidden") === "1"; } catch {}

  function paintRoutes(d) {
    if (gesture || savingOrder) { routesStale = true; return; }
    routesStale = false;
    const grid = $("#route-grid");
    const focusId = document.activeElement?.matches?.("#route-grid .route[data-route]") ? document.activeElement.dataset.route : null;
    const hiddenCount = d.routes.filter((r) => r.hidden && r.path !== "/hub").length;
    const toggle = $("#show-hidden");
    if (toggle) {
      toggle.hidden = !hiddenCount;
      toggle.setAttribute("aria-pressed", String(showHidden));
      toggle.querySelector("i").className = `ph ${showHidden ? "ph-eye" : "ph-eye-slash"}`;
      toggle.querySelector("span").textContent = T(showHidden ? "index.hideHidden" : "index.showHidden", { n: hiddenCount });
    }
    const visible = d.routes.filter((r) => !r.hidden || (showHidden && r.path !== "/hub"));
    visible.sort((a, b) => (b.path === "/hub") - (a.path === "/hub"));
    grid.innerHTML = visible.length ? visible.map((r, i) => routeCard(r, i)).join("")
      : `<p class="empty">${T("index.emptyRoutes")}</p>`;
    updateLead();
    syncSelection();
    if (focusId) [...grid.querySelectorAll(".route[data-route]")].find((c) => c.dataset.route === focusId)?.focus({ preventScroll: true });
  }

  function routeCard(r, i) {
    const isMeta = r.path === "/hub";
    const selectable = caps.includes("funnel") && r.kind === "proxy" && r.up && !isMeta && !r.path.startsWith("/hub");
    const cls = ["route", r.up ? "" : "down", isMeta ? "meta" : "", selectable ? "selectable" : "", r.hidden ? "is-hidden" : "",
      svcState[r.id]?.hook ? "has-hook" : "", painted ? "in-view settled" : ""].join(" ");
    const state = r.up
      ? `<span class="state">${r.kind === "proxy" ? `${r.ms} ms` : T("index.static")}</span>`
      : `<span class="state off">${T("index.noResponse")}</span>`;
    const target = r.kind === "proxy" ? r.target.replace(/^https?:\/\//, "") : r.target;
    const portLabel = `${r.port}${r.path !== "/" ? `<small>${esc(r.path)}</small>` : ""}`;
    if (isMeta) {
      return `<article class="${cls}" style="--i:${i}"><div class="frame"><div class="in">
        <i class="ph ${esc(r.icon)} r-icon" aria-hidden="true"></i>
        <h3 class="r-name">${esc(r.name)}</h3>
        <a class="r-url" href="${esc(r.url)}" title="${esc(r.url)}">${esc(r.url.replace("https://", ""))}</a>
        <div class="r-foot"><span class="r-target">${esc(target)}</span>
          ${labelButton(r)}
        </div>
      </div></div></article>`;
    }
    const ctl = r.control ? `
        <span class="r-ctl" data-svc="${esc(r.id)}">
          ${hookMark(r)}
          <button class="switch" type="button" role="switch" aria-checked="${r.up}" data-svc-toggle="${esc(r.id)}" aria-label="${esc(T("index.switchAria", { name: r.name }))}"></button>
          <button class="icon-btn" type="button" data-term="${esc(r.id)}" data-name="${esc(r.name)}" aria-label="${esc(T("index.terminalFor", { name: r.name }))}"><i class="ph ph-terminal-window"></i></button>
        </span>` : `<i class="ph ${esc(r.icon)} r-icon" aria-hidden="true"></i>`;
    const on = selected.has(String(r.id));
    return `<article class="${cls}" style="--i:${i}" data-route="${esc(r.id)}" tabindex="0" aria-labelledby="rn-${esc(r.id)}" aria-describedby="${selectable ? "route-hint-select" : "route-hint-move"}"${selectable ? ` aria-pressed="${on}"` : ""}><div class="frame"><div class="in">
      <div class="r-top">
        <div class="r-port" aria-label="${esc(T("index.portAria", { port: r.port }))}">${portLabel}</div>
        ${r.hidden ? `<span class="r-tag"><i class="ph ph-eye-slash" aria-hidden="true"></i>${esc(T("index.hiddenTag"))}</span>` : ""}
        ${ctl}
      </div>
      <h3 class="r-name" id="rn-${esc(r.id)}">${esc(r.name)}</h3>
      ${r.about ? `<p class="r-about">${esc(r.about)}</p>` : ""}
      <a class="r-url" href="${esc(r.url)}" target="_blank" rel="noopener" title="${esc(r.url)}">${esc(r.url.replace("https://", ""))}</a>
      <div class="r-foot">
        ${state}
        <span class="r-target" title="${esc(r.project)}">→ ${esc(target)}</span>
        <span class="r-actions">
          ${r.panel ? `<a class="pill-btn" href="${esc(r.panel)}"><i class="ph ph-gauge" aria-hidden="true"></i>${T("index.panel")}</a>` : ""}
          ${selectable ? `<button class="icon-btn share-btn" type="button" data-share="${esc(r.id)}" aria-pressed="${on}" aria-label="${esc(T("index.selectRoute", { name: r.name }))}" title="${esc(T("index.sharePublic", { name: r.name }))}"><i class="ph ph-globe-hemisphere-west"></i></button>` : ""}
          ${labelButton(r)}
          <button class="icon-btn" type="button" data-copy="${esc(r.url)}" aria-label="${esc(T("index.copyUrl", { name: r.name }))}"><i class="ph ph-copy"></i></button>
          <a class="icon-btn" href="${esc(r.url)}" target="_blank" rel="noopener" aria-label="${esc(T("index.open", { name: r.name }))}"><i class="ph ph-arrow-up-right"></i></a>
        </span>
      </div>
    </div></div></article>`;
  }

  // ---------- served routes: click selects, drag reorders (pointer events + FLIP) ----------
  // A press on a card is pending until it proves to be a drag: 6 px of mouse travel, or a
  // still finger held for HOLD_MS (a finger that moves first is scrolling and is let go).
  // Cards reorder live; the order is saved on drop and reverted if the hub refuses it.
  const INTERACTIVE = "a,button,input,select,textarea,label,summary,[role=switch],[contenteditable]";
  const FLIP_MS = 280, FLIP_EASE = "cubic-bezier(.2,.8,.2,1)";
  const HOLD_MS = 170, SLOP_MOUSE = 6, SLOP_TOUCH = 8, REORDER_SLOP = 8, EDGE = 64;
  const routeGrid = $("#route-grid");
  const routeCards = () => [...routeGrid.querySelectorAll(".route[data-route]:not(.meta)")];
  let gesture = null;
  let savingOrder = false, routesStale = false, swallowUntil = 0;
  let lastOrder = null; // {ids, at}: survives a data.json that was generated before the save

  // Bento roles follow DOM order: the first card leads and the last ones close their row.
  function updateLead() {
    const cards = routeCards(), n = cards.length, rest = n - 3;
    cards.forEach((card, i) => {
      card.classList.toggle("lead", i === 0);
      card.classList.toggle("solo", n === 1);
      card.classList.toggle("duo", n === 2 && i === 0);
      card.classList.toggle("side", i > 0 && i < 3);
      card.classList.toggle("third", rest > 1 && rest % 2 === 1 && i >= n - 3);
      card.classList.toggle("wide-lg", rest === 1 && i === n - 1);
      card.classList.toggle("wide-md", n > 1 && (n - 1) % 2 === 1 && i === n - 1);
    });
  }

  // Layout box without transforms: offsets ignore translate, scale and running animations.
  function layoutBox(el) {
    const g = routeGrid.getBoundingClientRect();
    const left = g.left + el.offsetLeft, top = g.top + el.offsetTop;
    return { left, top, width: el.offsetWidth, height: el.offsetHeight, right: left + el.offsetWidth, bottom: top + el.offsetHeight };
  }

  // Moving a node restarts its CSS animations: end the entrance before any card moves.
  const settleCards = () => routeCards().forEach((card) => card.classList.add("in-view", "settled"));

  // FLIP from the rects on screen before a DOM change (mid-animation included) to the new layout.
  function flip(before, skip) {
    if (reduce) return;
    const els = [...before.keys()].filter((el) => el !== skip && el.isConnected);
    els.forEach((el) => el.getAnimations().forEach((a) => { if (a.id === "flip") a.cancel(); }));
    const after = els.map((el) => el.getBoundingClientRect());
    els.forEach((el, i) => {
      const old = before.get(el), now = after[i];
      const dx = old.left - now.left, dy = old.top - now.top;
      const sx = now.width ? old.width / now.width : 1, sy = now.height ? old.height / now.height : 1;
      if (Math.abs(dx) < .5 && Math.abs(dy) < .5 && Math.abs(sx - 1) < .005 && Math.abs(sy - 1) < .005) return;
      el.animate([
        { transformOrigin: "top left", transform: `translate(${dx}px, ${dy}px) scale(${sx}, ${sy})` },
        { transformOrigin: "top left", transform: "none" },
      ], { duration: FLIP_MS, easing: FLIP_EASE, id: "flip" });
    });
  }
  const rectsOf = (els) => new Map(els.map((el) => [el, el.getBoundingClientRect()]));

  function restoreOrder(cards) {
    const before = rectsOf(routeCards());
    cards.forEach((card) => card.isConnected && routeGrid.appendChild(card));
    updateLead();
    flip(before);
  }

  function applyOrder(d, ids) {
    const pos = new Map(ids.map((id, i) => [String(id), i]));
    const at = (r) => (r.path === "/hub" ? -1 : pos.has(String(r.id)) ? pos.get(String(r.id)) : ids.length);
    d.routes = d.routes.map((r, i) => [r, i]).sort((a, b) => at(a[0]) - at(b[0]) || a[1] - b[1]).map(([r]) => r);
  }

  async function saveRouteOrder(initial) {
    const ids = routeCards().map((card) => card.dataset.route);
    savingOrder = true;
    try {
      const r = await fetch("api/routes/order", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
      lastOrder = { ids, at: Date.now() };
      if (data) applyOrder(data, ids);
    } catch {
      restoreOrder(initial);
      toast(T("index.orderFailed"));
    } finally {
      savingOrder = false;
      if (routesStale && !gesture) paintRoutes(data);
    }
  }

  // The card under the pointer, by layout box; above the cards means first, below means last.
  function targetAt(x, y) {
    const cards = routeCards(), me = gesture.card;
    for (const card of cards) {
      if (card === me) continue;
      const b = layoutBox(card);
      if (x >= b.left && x <= b.right && y >= b.top && y <= b.bottom) return card;
    }
    const first = cards[0], last = cards[cards.length - 1];
    if (first && first !== me && y < layoutBox(first).top) return first;
    if (last && last !== me && y > routeGrid.getBoundingClientRect().bottom) return last;
    return null;
  }

  function follow() {
    const g = gesture, b = layoutBox(g.card);
    g.card.style.translate = `${g.x - g.fx * b.width - b.left}px ${g.y - g.fy * b.height - b.top}px`;
  }

  function reorder(target) {
    const g = gesture, cards = routeCards();
    const from = cards.indexOf(g.card), to = cards.indexOf(target);
    if (from < 0 || to < 0 || from === to) return;
    const before = rectsOf(cards), was = layoutBox(g.card);
    if (from < to) target.after(g.card); else target.before(g.card);
    updateLead();
    flip(before, g.card);
    const now = layoutBox(g.card);
    if (!reduce && (Math.abs(was.width - now.width) > 1 || Math.abs(was.height - now.height) > 1)) {
      // The dragged card changes size (it became or stopped being the lead): grow around the grab point.
      const origin = `${g.fx * 100}% ${g.fy * 100}%`;
      g.card.getAnimations().forEach((a) => { if (a.id === "flip") a.cancel(); });
      g.card.animate([
        { transformOrigin: origin, transform: `scale(${was.width / now.width}, ${was.height / now.height})` },
        { transformOrigin: origin, transform: "none" },
      ], { duration: FLIP_MS, easing: FLIP_EASE, id: "flip" });
    }
    g.lx = g.x + scrollX; g.ly = g.y + scrollY;
  }

  function dragFrame() {
    const g = gesture;
    if (!g || g.phase !== "drag") return;
    const v = g.y < EDGE ? -Math.ceil((EDGE - g.y) / 4) : g.y > innerHeight - EDGE ? Math.ceil((g.y - innerHeight + EDGE) / 4) : 0;
    if (v) { scrollBy({ top: v, behavior: "instant" }); g.dirty = true; }
    if (g.dirty) {
      g.dirty = false;
      if (Math.hypot(g.x + scrollX - g.lx, g.y + scrollY - g.ly) > REORDER_SLOP) {
        const target = targetAt(g.x, g.y);
        if (target) reorder(target);
      }
      follow();
    }
    requestAnimationFrame(dragFrame);
  }

  function startDrag() {
    const g = gesture;
    if (!g || g.phase !== "pending") return;
    clearTimeout(g.timer);
    g.phase = "drag";
    g.dirty = true;
    g.lx = g.x + scrollX; g.ly = g.y + scrollY;
    try { routeGrid.setPointerCapture(g.pointerId); } catch {}
    settleCards();
    g.card.classList.remove("pressing");
    g.card.classList.add("dragging");
    document.documentElement.classList.add("route-dragging");
    getSelection()?.removeAllRanges();
    if (g.touch) navigator.vibrate?.(8);
    requestAnimationFrame(dragFrame);
  }

  function endGesture(commit) {
    const g = gesture;
    if (!g) return;
    gesture = null;
    clearTimeout(g.timer);
    g.card.classList.remove("pressing");
    if (g.phase === "drag") {
      try { routeGrid.releasePointerCapture(g.pointerId); } catch {}
      document.documentElement.classList.remove("route-dragging");
      // The native click is unreliable after a captured drag: decide here. A short hold that
      // never moved still selects, like a tap; a drag or a long press does not.
      swallowUntil = performance.now() + 400;
      if (commit && g.touch && !g.moved && performance.now() - g.t0 < 500 && g.card.classList.contains("selectable")) toggleSelected(g.card.dataset.route);
      const from = g.card.style.translate;
      g.card.style.translate = "";
      g.card.classList.remove("dragging");
      if (!reduce && from) {
        g.card.classList.add("settling");
        const settle = g.card.animate([{ translate: from }, { translate: "0px 0px" }], { duration: 240, easing: FLIP_EASE });
        settle.finished.catch(() => {}).finally(() => g.card.classList.remove("settling"));
      }
      const changed = routeCards().some((card, i) => card !== g.initial[i]);
      if (changed && !commit) restoreOrder(g.initial);
      else if (changed) { saveRouteOrder(g.initial); return; }
    }
    if (routesStale && !savingOrder) paintRoutes(data);
  }

  routeGrid.addEventListener("pointerdown", (e) => {
    if (gesture && e.pointerId === gesture.pointerId) endGesture(false); // its pointerup was lost
    if (gesture) {
      // A second finger means pinch or scroll: a press that is still pending gives way.
      if (gesture.phase === "pending") endGesture(false);
      return;
    }
    if (!e.isPrimary || e.button !== 0 || savingOrder) return;
    const card = e.target.closest(".route[data-route]:not(.meta)");
    if (!card || e.target.closest(INTERACTIVE)) return;
    const b = layoutBox(card);
    gesture = {
      card, pointerId: e.pointerId, touch: e.pointerType !== "mouse", phase: "pending",
      x0: e.clientX, y0: e.clientY, x: e.clientX, y: e.clientY, t0: performance.now(), moved: false,
      fx: (e.clientX - b.left) / b.width, fy: (e.clientY - b.top) / b.height,
      initial: routeCards(), timer: 0,
    };
    card.classList.add("pressing");
    if (gesture.touch) gesture.timer = setTimeout(startDrag, HOLD_MS);
  });

  document.addEventListener("pointermove", (e) => {
    const g = gesture;
    if (!g || e.pointerId !== g.pointerId) return;
    g.x = e.clientX; g.y = e.clientY;
    const travel = Math.hypot(g.x - g.x0, g.y - g.y0);
    if (g.phase === "pending") {
      if (g.touch) { if (travel > SLOP_TOUCH) endGesture(false); } // the finger is scrolling the page
      else if (travel > SLOP_MOUSE) startDrag();
      return;
    }
    if (travel > 4) g.moved = true;
    g.dirty = true;
  });
  document.addEventListener("pointerup", (e) => { if (gesture && e.pointerId === gesture.pointerId) endGesture(true); });
  document.addEventListener("pointercancel", (e) => { if (gesture && e.pointerId === gesture.pointerId) endGesture(false); });
  routeGrid.addEventListener("lostpointercapture", (e) => {
    if (gesture && gesture.phase === "drag" && e.pointerId === gesture.pointerId) endGesture(false);
  });
  addEventListener("blur", () => endGesture(false));
  document.addEventListener("visibilitychange", () => { if (document.hidden) endGesture(false); });
  // Once a touch drag is confirmed the page must not scroll under it. Only touches that start
  // on the grid pay for this non-passive listener.
  routeGrid.addEventListener("touchmove", (e) => {
    if (gesture && gesture.phase === "drag" && e.cancelable) e.preventDefault();
  }, { passive: false });
  routeGrid.addEventListener("contextmenu", (e) => { if (gesture) e.preventDefault(); });
  routeGrid.addEventListener("dragstart", (e) => { if (gesture) e.preventDefault(); });

  routeGrid.addEventListener("click", (e) => {
    if (performance.now() < swallowUntil) {
      swallowUntil = 0;
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);
  routeGrid.addEventListener("click", (e) => {
    const card = e.target.closest(".route.selectable[data-route]");
    if (!card || e.target.closest(INTERACTIVE)) return;
    toggleSelected(card.dataset.route);
  });

  // Keyboard: Enter/Space selects; Alt+arrows move the card. Controls inside keep their keys.
  routeGrid.addEventListener("keydown", (e) => {
    const card = e.target.closest(".route[data-route]:not(.meta)");
    if (!card || e.target !== card) return;
    if ((e.key === "Enter" || e.key === " ") && !e.altKey) {
      e.preventDefault();
      if (card.classList.contains("selectable")) toggleSelected(card.dataset.route);
      return;
    }
    const delta = { ArrowUp: -1, ArrowLeft: -1, ArrowDown: 1, ArrowRight: 1 }[e.key];
    if (!delta || !e.altKey || gesture || savingOrder) return;
    e.preventDefault();
    const cards = routeCards(), i = cards.indexOf(card), j = i + delta;
    if (j < 0 || j >= cards.length) return;
    settleCards();
    const before = rectsOf(cards);
    // Move the neighbour, not the focused card: moving it would drop the focus.
    if (delta < 0) card.after(cards[j]); else card.before(cards[j]);
    updateLead();
    flip(before);
    card.scrollIntoView({ block: "nearest", behavior: reduce ? "auto" : "smooth" });
    toast(T("index.movedTo", { n: j + 1, total: cards.length }));
    saveRouteOrder(cards);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && gesture) endGesture(false); });

  // A start hook (the command behind the switch) is marked with a quiet icon; the label editor shows it.
  function hookMark(r) {
    return `<i class="ph ph-file-code r-hook" role="img" aria-label="${esc(T("index.hookMark", { name: r.name }))}" title="${esc(T("index.hookMark", { name: r.name }))}"></i>`;
  }

  function labelButton(r) {
    if (!caps.includes("labels") || !r.id) return "";
    return `<button class="icon-btn" type="button" data-label-edit="${esc(r.id)}" aria-label="${esc(T("labels.editFor", { name: r.name }))}" title="${esc(T("labels.edit"))}"><i class="ph ph-pencil-simple"></i></button>`;
  }

  let visitor = null, agents = {}, caps = [];
  (window.__hubCaps ? Promise.resolve(window.__hubCaps) : fetch("api/caps", { cache: "no-store" }).then((r) => r.json()))
    .then((c) => { caps = c.caps || []; if (data) render(data); loadShare(); }).catch(() => {});

  // ---------- public sharing (grouped Funnel) ----------
  let funnel = null, shareTimer = null;
  function shortTime(seconds) {
    const m = Math.max(0, Math.floor(seconds / 60));
    return m >= 60 ? T("time.inHours", { h: Math.floor(m / 60), m: m % 60 }) : T("time.inMinutes", { n: m });
  }

  async function loadShare() {
    if (!caps.includes("funnel")) return;
    try { funnel = await (await fetch("api/funnel", { cache: "no-store" })).json(); } catch { return; }
    const pub = $("#published"), list = $("#published-list"), bar = $("#share-bar");
    if (bar) bar.hidden = true;
    clearInterval(shareTimer);
    const groups = funnel.groups || [];
    if (!groups.length) { if (pub) pub.hidden = true; return; }
    if (pub) pub.hidden = false;
    const card = (a) => {
      const forever = a.expires == null;
      const left = forever ? Infinity : Math.max(0, a.expires - Date.now() / 1000);
      const when = forever ? T("index.shareForever", { date: new Date(a.created * 1000).toLocaleString(I18N.locale, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) })
        : T("index.shareExpires", { time: shortTime(left) });
      const urls = (a.urls || (a.url ? [{ url: a.url, name: a.name }] : [])).map((u, i) => `
        <div class="published-url"><span>${esc(u.name || (i ? T("index.shareLink", { n: i + 1 }) : T("index.sharePortal")))}</span><code>${esc(u.url)}</code>
          <button class="icon-btn" type="button" data-copy="${esc(u.url)}" aria-label="${esc(T("index.shareCopy"))}"><i class="ph ph-copy"></i></button></div>`).join("");
      return { left, html: `<article class="published-group frame"><div class="in">
        <div class="published-head"><div><strong>${esc(a.name)}</strong><span>${esc(when)}</span></div>
          <button class="btn btn-ghost btn-xs" type="button" data-share-stop="${esc(a.token)}"><i class="ph ph-stop" aria-hidden="true"></i>${T("index.shareStop")}</button></div>
        ${urls}
        <p class="av-note">${esc(T("index.publishedAuth"))}</p>
      </div></article>` };
    };
    const paint = () => {
      const cards = groups.map(card);
      list.innerHTML = cards.map((c) => c.html).join("");
      if (cards.some((c) => c.left <= 0)) loadShare();
    };
    paint();
    shareTimer = setInterval(paint, 1000);
  }

  async function startGroup(minutes) {
    const name = ($("#group-name")?.value || "").trim();
    if (!name) { toast(T("index.publishNameRequired")); $("#group-name")?.focus(); return; }
    const ids = [...selected];
    if (!ids.length) return;
    try {
      const r = await fetch("api/funnel/share", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids, name, minutes }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      const first = (j.urls && j.urls[0] && j.urls[0].url) || j.url;
      if (first) copy(first, T("index.shareCopied"));
      selected.clear(); syncSelection();
      closeTerm();
      loadShare();
    } catch (e) { toast(e.message); }
  }

  function showPublish() {
    if (!selected.size) return;
    const names = [...selected].map((id) => (data?.routes || []).find((r) => String(r.id) === id)?.name || id);
    const el = $("#term");
    $("#term-title").textContent = T("index.publishGroup");
    $("#term-state").textContent = TN("index.publishSelected", selected.size);
    term.id = null; clearInterval(term.timer);
    const out = $("#term-out");
    if (!funnel || !funnel.allowed) {
      out.innerHTML = `<p>${T("index.shareNotEnabled")}</p>
        <p><a class="pill-btn" href="${esc(funnel?.enable_url || "https://login.tailscale.com/admin/acls")}" target="_blank" rel="noopener"><i class="ph ph-arrow-up-right" aria-hidden="true"></i>${T("index.shareEnable")}</a></p>
        <p>${T("index.shareRetry")}</p>`;
    } else {
      out.innerHTML = `<p>${esc(T("index.publishHint", { names: names.join(" + ") }))}</p>
        <label class="group-label" for="group-name">${T("index.groupName")}</label>
        <input id="group-name" class="group-input" maxlength="64" autocomplete="off" value="${esc(names.join(" + "))}">
        <div class="ct-row">${[[15, "index.share15"], [60, "index.share60"], [240, "index.share240"], [1440, "index.share1440"], [0, "index.share0"]].map(([m, key]) =>
          `<button class="btn ${m === 60 ? "btn-primary" : "btn-ghost"} btn-xs" type="button" data-group-min="${m}">${T(key)}</button>`).join("")}</div>
        <p class="av-note">${T("index.publishForeverNote")}</p>`;
    }
    el.hidden = false; requestAnimationFrame(() => el.classList.add("open"));
    setTimeout(() => $("#group-name")?.focus(), 150);
  }

  fetch("api/whoami", { cache: "no-store" }).then((r) => r.json()).then((v) => { visitor = v; if (data) render(data); }).catch(() => {});
  // Hub directory (available on every hub): name -> {url, online}
  fetch("api/fleet", { cache: "no-store" }).then((r) => r.json()).then((j) => {
    agents = {};
    for (const h of (j.hubs || [])) {
      agents[h.name] = h;
      if (h.name) agents[String(h.name).toLowerCase()] = h;
      if (h.dns) agents[String(h.dns).toLowerCase()] = h;
      if (h.ip) agents[h.ip] = h;
      // The host in the hub's URL is its device's MagicDNS name: it matches the device even when the
      // hub has another name, and with hubs that do not send ip/dns yet.
      try {
        const host = new URL(h.url).hostname.toLowerCase();
        if (host && !/^[0-9.:\[\]]+$/.test(host)) { agents[host] = agents[host] || h; agents[host.split(".")[0]] = agents[host.split(".")[0]] || h; }
      } catch (e) { /* no URL */ }
    }
    if (data) render(data);
  }).catch(() => {});

  function deviceRow(p, i) {
    const ip = (p.ips || [])[0] || "";
    const you = visitor ? String(visitor.name || "").toLowerCase() === String(p.name || "").toLowerCase() : p.self;
    let st;
    const hubOf = agents[p.name] || agents[String(p.name || "").toLowerCase()] || agents[String(p.dns || "").toLowerCase()] || (p.ips || []).map((x) => agents[x]).find(Boolean);
    const displayName = hubOf ? hubOf.name : p.name;
    if (p.self) st = you ? T("index.thisHubHere") : T("index.thisHub");
    else if (p.online) st = p.direct ? T("index.onlineDirect") : T("index.onlineRelay", { relay: esc(p.relay || "tailnet") });
    else st = p.lastSeen ? T("index.seen", { ago: ago(p.lastSeen) }) : T("index.offline");
    return `<li class="device frame ${p.online ? "on" : ""}${painted ? " in-view settled" : ""}" style="--i:${i}"><div class="in">
      <i class="ph ${osIcon(p.os)} d-os" aria-hidden="true"></i>
      <div class="d-name">${esc(displayName)}${you ? `<span class="you">${T("index.roleYou")}</span>` : ""}${p.self && !you ? `<span class="you hub">${T("index.roleHub")}</span>` : ""}</div>
      <button class="d-ip" type="button" data-copy="${esc(ip)}" aria-label="${esc(T("index.copyIpToast", { ip }))}">${esc(ip)}</button>
      <div class="d-state">${st}${hubOf && !p.self ? ` · ${hubOf.online ? T("index.hubActive") : T("index.hubDown")}` : ""}</div>
      ${p.self ? "" : hubOf ? `<a class="pill-btn d-open" href="${esc(/^https?:\/\//i.test(String(hubOf.url || "")) ? hubOf.url : "#")}"><i class="ph ph-arrow-up-right" aria-hidden="true"></i>${T("index.roleHub")}</a>`
        : caps.includes("nodes") ? `<button class="pill-btn d-open d-install" type="button" data-install="${esc(p.name)}" data-os="${esc(p.os)}"><i class="ph ph-plus" aria-hidden="true"></i>${T("index.installHub")}</button>` : ""}
    </div></li>`;
  }

  // ---------- staggered entrance ----------
  const io = "IntersectionObserver" in window && !reduce
    ? new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in-view"); io.unobserve(e.target); } }), { threshold: .12 })
    : null;
  function observe() {
    document.querySelectorAll(".reveal, .route, .device").forEach((el) => {
      if (el.classList.contains("in-view")) return;
      if (io) io.observe(el); else el.classList.add("in-view");
    });
  }

  // ---------- radar (real data: routes on the inner ring, devices outside) ----------
  const cv = $("#radar");
  const ctx = cv.getContext("2d");
  function sizeCanvas() {
    const r = cv.getBoundingClientRect(), dpr = Math.min(2, devicePixelRatio || 1);
    cv.width = Math.round(r.width * dpr); cv.height = Math.round(r.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  const hashAngle = (s) => { let h = 2166136261; for (const c of String(s)) h = Math.imul(h ^ c.charCodeAt(0), 16777619); return ((h >>> 0) % 360) * Math.PI / 180; };

  // ---------- live mesh: hubs as their mascots, real gossip contacts as packets ----------
  // api/fleet says how long ago this hub last heard from each one; when that number drops, a
  // gossip exchange just happened and a packet travels out from the hub and back.
  const mesh = { hubs: [], me: null, seen: {}, packets: [], bump: {} };
  const hostKey = (h) => { try { return new URL(h.url).hostname.split(".")[0].toLowerCase(); } catch { return h.name.toLowerCase(); } };
  // Spread angles that would overlap (two names hashing close together) at least `gap` apart.
  function spread(angles, gap = 0.32) {
    const order = Object.keys(angles).sort((a, b) => angles[a] - angles[b]);
    for (let pass = 0; pass < 3; pass++) {
      for (let i = 1; i < order.length; i++) {
        const d = angles[order[i]] - angles[order[i - 1]];
        if (d < gap) angles[order[i]] += gap - d;
      }
    }
    return angles;
  }
  async function loadMesh() {
    if (document.hidden) return;
    try {
      const f = await (await fetch("api/fleet", { cache: "no-store" })).json();
      const now = performance.now();
      mesh.hubs = (f.hubs || []).filter((h) => !h.here);
      mesh.me = (f.hubs || []).find((h) => h.here) || mesh.me;
      for (const h of mesh.hubs) {
        const prev = mesh.seen[h.name];
        if (h.online && h.seen != null && prev != null && h.seen + 1 < prev && !reduce) {
          mesh.packets.push({ hub: h.name, t0: now, out: true }, { hub: h.name, t0: now + 900, out: false });
        }
        mesh.seen[h.name] = h.seen;
      }
      if (reduce) drawRadar();
    } catch { /* an old hub without the mesh: plain radar */ }
  }
  const blinkAt = (name, t) => (t + (hashAngle(name) * 997)) % 4700 < 130;

  function drawRadar(t = 0) {
    if (document.hidden) return;
    const d = drawRadar.data;
    const w = cv.clientWidth, h = cv.clientHeight, cx = w / 2, cy = h / 2, R = Math.min(w, h) / 2 - 18;
    const C = { line: css("--line-2"), ink: css("--ink"), ink3: css("--ink-3"), acc: css("--accent"), down: css("--down"), bg: css("--bg") };
    ctx.clearRect(0, 0, w, h);
    ctx.lineWidth = 1;
    // rings and axes
    ctx.strokeStyle = C.line;
    [0.32, 0.62, 1].forEach((k) => { ctx.beginPath(); ctx.arc(cx, cy, R * k, 0, Math.PI * 2); ctx.stroke(); });
    ctx.setLineDash([2, 6]);
    for (let a = 0; a < 4; a++) { ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + Math.cos(a * Math.PI / 2) * R, cy + Math.sin(a * Math.PI / 2) * R); ctx.stroke(); }
    ctx.setLineDash([]);
    // sweep: communicates that the state is live
    if (!reduce) {
      const ang = (t / 4200) * Math.PI * 2;
      const g = ctx.createConicGradient ? ctx.createConicGradient(ang - 0.9, cx, cy) : null;
      if (g) {
        g.addColorStop(0, "transparent"); g.addColorStop(0.14, C.acc + "33"); g.addColorStop(0.15, "transparent");
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.fill();
      }
    }
    if (!d) { if (!reduce && !document.hidden) requestAnimationFrame(drawRadar); return; }
    ctx.font = `500 11px ${css("--mono")}`;
    ctx.textBaseline = "middle";
    // routes
    const routes = d.routes.filter((r) => r.path !== "/hub" && !r.hidden);
    routes.forEach((r, i) => {
      const a = -Math.PI / 2 + (i / Math.max(1, routes.length)) * Math.PI * 2 + 0.35;
      const x = cx + Math.cos(a) * R * 0.32, y = cy + Math.sin(a) * R * 0.32;
      ctx.strokeStyle = r.up ? C.acc : C.down; ctx.globalAlpha = r.up ? 0.55 : 0.35;
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(x, y); ctx.stroke(); ctx.globalAlpha = 1;
      ctx.fillStyle = r.up ? C.acc : C.down;
      ctx.fillRect(x - 3.5, y - 3.5, 7, 7);
      ctx.fillStyle = C.ink3; ctx.textAlign = Math.cos(a) >= 0 ? "left" : "right";
      ctx.fillText(`:${r.port}`, x + (Math.cos(a) >= 0 ? 10 : -10), y);
    });
    // hubs are drawn as mascots; every other device keeps its dot
    const Mascot = window.HubMascot, faces = !!(Mascot && Mascot.enabled());
    const hubOf = new Map(mesh.hubs.flatMap((h) => [[hostKey(h), h], [h.name.toLowerCase(), h]]));
    const pos = {};
    const peers = d.peers.filter((p) => {
      const h = hubOf.get(((p.dns || p.name).split(".")[0]).toLowerCase());
      if (h) { pos[h.name] = { p, h }; return false; }
      return true;
    });
    for (const h of mesh.hubs) if (!pos[h.name]) pos[h.name] = { p: { name: h.name, online: h.online }, h };
    const angle = spread(Object.fromEntries([...Object.keys(pos), ...peers.map((p) => p.name)].map((n) => [n, hashAngle(n)])));
    for (const { p, h } of Object.values(pos)) {
      const a = angle[h.name], k = h.online ? 0.62 : 1;
      const x = cx + Math.cos(a) * R * k, y = cy + Math.sin(a) * R * k;
      pos[h.name].x = x; pos[h.name].y = y;
      if (h.online) {
        ctx.strokeStyle = C.acc; ctx.globalAlpha = 0.35; ctx.setLineDash([2, 5]);
        ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(x, y); ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha = 1;
      }
      const b = mesh.bump[h.name], lift = b && t - b < 420 ? Math.sin(((t - b) / 420) * Math.PI) * 5 : 0;
      if (faces) Mascot.draw(ctx, h.pub || h.name, x, y, 2.65, { state: !h.online ? "off" : h.coordinator ? "coord" : "ok", blink: !reduce && blinkAt(h.name, t), lift });
      else {  // plain node: a square, ringed while it coordinates
        ctx.fillStyle = h.online ? C.acc : C.ink3;
        ctx.fillRect(Math.round(x - 5), Math.round(y - 5 - lift), 10, 10);
        if (h.coordinator) { ctx.strokeStyle = C.acc; ctx.strokeRect(Math.round(x - 9) + 0.5, Math.round(y - 9 - lift) + 0.5, 18, 18); }
      }
      ctx.fillStyle = h.online ? C.ink : C.ink3; ctx.textAlign = Math.cos(a) >= 0 ? "left" : "right";
      ctx.fillText(h.name, x + (Math.cos(a) >= 0 ? (faces ? 25 : 16) : (faces ? -25 : -16)), y);
    }
    // gossip packets: a pixel and its trail, out to the hub and back to this one
    mesh.packets = mesh.packets.filter((pk) => t - pk.t0 < 1100);
    for (const pk of mesh.packets) {
      const at = pos[pk.hub];
      if (!at || t < pk.t0) continue;
      const q = Math.min(1, (t - pk.t0) / 1000), e = q < 0.5 ? 2 * q * q : 1 - (-2 * q + 2) ** 2 / 2;
      const [fx, fy, tx, ty] = pk.out ? [cx, cy, at.x, at.y] : [at.x, at.y, cx, cy];
      for (let i = 0; i < 4; i++) {
        const ee = Math.max(0, e - i * 0.05);
        ctx.globalAlpha = 1 - i * 0.24;
        ctx.fillStyle = pk.out ? C.acc : C.ink;
        ctx.fillRect(Math.round(fx + (tx - fx) * ee) - 2, Math.round(fy + (ty - fy) * ee) - 2, 4, 4);
      }
      ctx.globalAlpha = 1;
      if (q >= 1 && !pk.done) { pk.done = true; mesh.bump[pk.out ? pk.hub : "@me"] = t; }
    }
    // devices
    peers.forEach((p) => {
      const a = angle[p.name], k = p.online ? 0.62 : 1;
      const x = cx + Math.cos(a) * R * k, y = cy + Math.sin(a) * R * k;
      if (p.online) {
        ctx.strokeStyle = C.acc; ctx.globalAlpha = 0.25; ctx.setLineDash([3, 4]);
        ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(x, y); ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha = 1;
        const pulse = reduce ? 0 : (Math.sin(t / 500 + a) + 1) / 2;
        ctx.strokeStyle = C.acc; ctx.globalAlpha = 0.5 - pulse * 0.4;
        ctx.beginPath(); ctx.arc(x, y, 7 + pulse * 6, 0, Math.PI * 2); ctx.stroke(); ctx.globalAlpha = 1;
      }
      ctx.fillStyle = p.online ? C.acc : C.ink3;
      ctx.beginPath(); ctx.arc(x, y, 4.5, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = p.online ? C.ink : C.ink3; ctx.textAlign = Math.cos(a) >= 0 ? "left" : "right";
      ctx.fillText(p.name, x + (Math.cos(a) >= 0 ? 12 : -12), y);
    });
    // center: this hub (its mascot once the mesh answered)
    if (faces && mesh.me) {
      const b = mesh.bump["@me"], lift = b && t - b < 420 ? Math.sin(((t - b) / 420) * Math.PI) * 6 : 0;
      Mascot.draw(ctx, mesh.me.pub || mesh.me.name, cx, cy, 3.5, { state: mesh.me.coordinator ? "coord" : "ok", blink: !reduce && blinkAt(mesh.me.name, t), lift });
    } else {
      ctx.fillStyle = C.bg; ctx.strokeStyle = C.acc; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(cx, cy, 12, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      ctx.fillStyle = C.acc; ctx.beginPath(); ctx.arc(cx, cy, 4, 0, Math.PI * 2); ctx.fill();
      ctx.lineWidth = 1;
    }
    if (!reduce && !document.hidden) requestAnimationFrame(drawRadar);
  }

  // ---------- data ----------
  async function load(manual = false) {
    const btn = $("#refresh");
    if (manual) { btn.classList.remove("spin"); void btn.offsetWidth; btn.classList.add("spin"); }
    try {
      const asked = Date.now();
      const res = await fetch(`data.json?t=${asked}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      if (lastOrder && lastOrder.at >= asked) applyOrder(d, lastOrder.ids);
      const changed = !data || data.generated !== d.generated;
      data = d; lastFetch = Date.now();
      render(d);
      if (changed && manual) $("#updated").classList.add("flash");
      tick();
    } catch (e) {
      $("#route-grid").innerHTML = `<p class="empty error">${esc(T("index.loadError", { error: e.message }))}</p>`;
    }
  }
  function tick() {
    if (!data) return;
    const el = $("#updated");
    el.textContent = ago(data.generated) || T("time.now");
  }

  // ---------- service control ----------
  let svcState = {};
  async function loadServices() {
    try { svcState = await (await fetch("api/services", { cache: "no-store" })).json(); } catch { return; }
    for (const [id, s] of Object.entries(svcState)) {
      const sw = document.querySelector(`[data-svc-toggle="${id}"]`);
      if (!sw) continue;
      sw.setAttribute("aria-checked", s.on);
      sw.disabled = !s.controllable || !!s.busy;
      sw.title = !s.controllable ? (s.kind === "windows_service" ? T("index.termWinService", { state: s.service_state || "" }) : T("index.termNoCmd"))
        : s.busy === "starting" ? T("index.termStartingShort") : s.busy === "stopping" ? T("index.termStoppingShort") : s.on ? T("index.termOnShort") : T("index.termOffShort");
      const card = document.querySelector(`[data-route="${id}"]`);
      if (card) { card.classList.toggle("busy", !!s.busy); card.classList.toggle("has-hook", !!s.hook); }
    }
  }
  async function toggleService(id, sw) {
    const s = svcState[id] || {};
    const action = s.on ? "stop" : "start";
    sw.disabled = true;
    try {
      const r = await fetch(`api/services/${id}/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      toast(action === "start" ? T("index.termStartingShort") : T("index.termStoppingShort"));
      openTerm(id, sw.closest("[data-svc]").querySelector("[data-term]").dataset.name);
    } catch (err) { toast(err.message); }
    setTimeout(loadServices, 600);
  }

  // Instructions for giving a new device its own hub (shown in the sliding terminal)
  async function showInstall(name, os) {
    // Any mesh hub serves the installer with its own address inside
    const base = new URL(".", location.href).href.replace(/\/$/, "");
    let here = null;
    try { here = (await (await fetch("api/mesh/view", { cache: "no-store" })).json()).hubs.find((h) => h.here) || null; } catch { /* no mesh */ }
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
    const { tx: android, win: windows, sh: unix } = installCmds(here);
    const sys = (os || "").toLowerCase();
    const after = T("index.installAfter");
    const cmd = sys === "android" ? android : sys === "windows" ? windows : unix;
    const txt = sys === "android"
      ? T("index.installAndroid", { name, cmd: android, after })
      : sys === "windows"
      ? T("index.installWindows", { name, cmd: windows, after })
      : T("index.installUnix", { name, os: os || "Linux, Raspberry Pi or macOS", cmd: unix, after });
    $("#term-title").textContent = T("index.installTitle", { name });
    $("#term-state").textContent = "";
    $("#term-out").textContent = txt;
    term.id = null; clearInterval(term.timer);
    const el = $("#term"); el.hidden = false; requestAnimationFrame(() => el.classList.add("open"));
    copy(cmd, T("index.installCopied"));
  }

  // Sliding terminal: live server output
  let term = { id: null, offset: 0, timer: null };
  function openTerm(id, name) {
    const el = $("#term");
    if (term.id !== id) { $("#term-out").textContent = ""; term = { id, offset: 0, timer: term.timer }; }
    $("#term-title").textContent = name;
    el.hidden = false;
    requestAnimationFrame(() => el.classList.add("open"));
    clearInterval(term.timer);
    pollTerm();
    term.timer = setInterval(pollTerm, 1000);
  }
  function closeTerm() {
    const el = $("#term");
    el.classList.remove("open");
    clearInterval(term.timer);
    setTimeout(() => { el.hidden = true; }, 350);
  }
  async function pollTerm() {
    if (!term.id) return;
    try {
      const j = await (await fetch(`api/services/${term.id}/log?offset=${term.offset}`, { cache: "no-store" })).json();
      if (j.text) {
        const out = $("#term-out"), stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
        out.textContent += j.text.replace(/\x1b\[[0-9;]*[A-Za-z]/g, "");
        if (out.textContent.length > 200000) out.textContent = out.textContent.slice(-150000);
        if (stick) out.scrollTop = out.scrollHeight;
      }
      term.offset = j.offset;
      const s = svcState[term.id];
      if (!j.offset && !$("#term-out").textContent) {
        $("#term-out").textContent = s && s.on ? T("index.termEmptyOn") : T("index.termEmptyOff");
      }
      $("#term-state").textContent = !s ? "" : s.busy === "starting" ? T("index.termStarting") : s.busy === "stopping" ? T("index.termStopping") : s.on ? T("index.termOn", { pid: s.pids.join(", ") }) : T("index.termOff");
    } catch { /* retry on the next tick */ }
  }

  document.addEventListener("click", (e) => {
    const le = e.target.closest("[data-label-edit]");
    if (le) {
      const r = data && data.routes.find((x) => x.id === le.dataset.labelEdit);
      if (r && window.HUB_LABELS) HUB_LABELS.open({ id: r.id, name: r.name, about: r.about, hidden: r.hidden }, (j) => {
        const cur = data && data.routes.find((x) => x.id === j.id);
        if (cur && j.service) {
          cur.name = j.service.name;
          cur.about = j.service.about || "";
          cur.custom = !!j.service.custom;
          render(data);
        }
        load(true);
        loadServices();
      }, { hook: !!r.control }); // the dialog asks this hub; containers simply have none
      return;
    }
    const b = e.target.closest("[data-copy]");
    if (b) { e.preventDefault(); copy(b.dataset.copy); return; }
    const sw = e.target.closest("[data-svc-toggle]");
    if (sw && !sw.disabled) { toggleService(sw.dataset.svcToggle, sw); return; }
    const sh = e.target.closest("[data-share]");
    if (sh) { toggleSelected(sh.dataset.share); return; }
    if (e.target.closest("#publish-open")) { showPublish(); return; }
    if (e.target.closest("#publish-clear")) { selected.clear(); syncSelection(); return; }
    const gm = e.target.closest("[data-group-min]");
    if (gm) {
      const minutes = +gm.dataset.groupMin;
      if (minutes === 0 && !confirm(T("index.shareConfirmForever"))) return;
      gm.disabled = true; startGroup(minutes); return;
    }
    const stopBtn = e.target.closest("[data-share-stop]");
    if (stopBtn) {
      fetch("api/funnel/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: stopBtn.dataset.shareStop }) })
        .then(() => { toast(T("index.shareStopped")); loadShare(); });
      return;
    }
    const inst = e.target.closest("[data-install]");
    if (inst) { showInstall(inst.dataset.install, inst.dataset.os); return; }
    const t = e.target.closest("[data-term]");
    if (t) { openTerm(t.dataset.term, t.dataset.name); return; }
    if (e.target.closest("#term-close")) closeTerm();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#term").hidden) closeTerm(); });
  // Collapsible sections; the preference is local to this browser.
  const COLLAPSE_KEY = "hub.sections.v1";
  let collapsed = {};
  try { collapsed = JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "{}") || {}; } catch { collapsed = {}; }
  function applyCollapse(key, value, animate = false) {
    const body = document.getElementById(`${key}-body`);
    const button = document.querySelector(`[data-collapse="${key}"]`);
    if (!body || !button) return;
    button.setAttribute("aria-expanded", String(!value));
    // Start from the height on screen, so a toggle in the middle of another one does not jump.
    const from = body.hidden ? 0 : body.getBoundingClientRect().height;
    body.getAnimations().forEach((a) => a.cancel());
    if (!animate || reduce) { body.hidden = !!value; body.style.overflow = ""; return; }
    body.hidden = false;
    const to = value ? 0 : body.scrollHeight;
    body.style.overflow = "hidden";
    const anim = body.animate([
      { height: `${from}px`, opacity: value ? 1 : 0 },
      { height: `${to}px`, opacity: value ? 0 : 1 },
    ], { duration: 340, easing: "cubic-bezier(.2,.8,.2,1)" });
    anim.onfinish = () => { body.hidden = !!value; body.style.overflow = ""; };
  }
  document.querySelectorAll("[data-collapse]").forEach((button) => {
    const key = button.dataset.collapse;
    applyCollapse(key, !!collapsed[key]);
    button.addEventListener("click", () => {
      collapsed[key] = !collapsed[key];
      applyCollapse(key, collapsed[key], true);
      try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify(collapsed)); } catch {}
    });
  });

  $("#show-hidden")?.addEventListener("click", () => {
    showHidden = !showHidden;
    try { localStorage.setItem("hub.showHidden", showHidden ? "1" : "0"); } catch {}
    if (data) render(data);
  });

  setInterval(() => { if (!document.hidden) loadServices(); }, 3000);
  $("#refresh").addEventListener("click", () => load(true));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      if (!reduce) requestAnimationFrame(drawRadar);
      if (Date.now() - lastFetch > 15000) load();
    }
  });
  addEventListener("resize", () => { sizeCanvas(); if (reduce) drawRadar(); });

  sizeCanvas();
  if (!reduce) requestAnimationFrame(drawRadar); else drawRadar();
  loadMesh();
  setInterval(loadMesh, 5000);
  observe();
  load();
  setInterval(() => { if (!document.hidden) load(); }, 30000);
  setInterval(() => { if (!document.hidden) tick(); }, 5000);
})();
