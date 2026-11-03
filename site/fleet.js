// "Mesh" selector: jump between hubs on the tailnet. Every hub keeps the full
// directory (masterless mesh, core/mesh.py), so api/fleet responds on any hub.
(async () => {
  "use strict";
  await I18N.ready;
  const T = (k, v) => I18N.t(k, v);
  const meta = document.querySelector(".bar-meta");
  if (!meta) return;

  // Standard UI on every hub: hide what this hub cannot do according to api/caps.
  const capsPromise = window.__hubCaps ? Promise.resolve(window.__hubCaps)
    : fetch("api/caps", { cache: "no-store" }).then((r) => r.json());
  capsPromise.then((c) => {
    if (window.HubTheme) HubTheme.sync(c.ui);
    const caps = c.caps || [];
    document.querySelectorAll("[data-cap]").forEach((el) => { el.hidden = !caps.includes(el.dataset.cap); });
    if ((c.kind === "agent" || c.kind === "core") && c.name) {
      const mark = document.querySelector(".mark");
      const face = window.HubMascot && HubMascot.enabled() ? HubMascot.svg(c.pub || c.name, { size: 22, title: c.name }) : "";
      if (mark) mark.innerHTML = `${face}<span>shoal</span><span class="slash">/</span><span>${esc(c.name.toLowerCase())}</span>`;
      if (/\bShoal\b/.test(document.title)) document.title = document.title.replace(/\bShoal\b/, c.name);
    }
  }).catch(() => {});
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Global navigation: the same destinations on every page, in the same order, with the current
  // one marked. Each item is hidden by api/caps like any [data-cap]. On a phone the items fold
  // into one menu button so the bar keeps the mesh selector and the page's own action.
  const NAV = [
    ["nodo.html", "ph-device-mobile", "common.device", "device"],
    ["control.html", "ph-sliders-horizontal", "common.control", "control"],
    ["terminal.html", "ph-terminal-window", "common.terminal", "terminal"],
    ["modules.html", "ph-puzzle-piece", "common.modules", "modules"],
    ["avisos.html", "ph-bell", "common.notifications", "push"],
  ];
  // A module panel lives at modules/<id>/…: its own nav entry if it declares one, else Modules.
  const modPage = location.pathname.match(/\/modules\/([\w-]+)\/[^/]*$/);
  const page = location.pathname.split("/").pop() || "index.html";
  let current = modPage ? "modules.html" : page;
  const node = new URLSearchParams(location.search).get("n"); // keep the device on its own pages
  const href = (h) => node && (h === "nodo.html" || h === "terminal.html") ? `${h}?n=${encodeURIComponent(node)}` : h;
  const link = ([h, ic, key, cap], cls) => `<a class="${cls}" href="${esc(href(h))}" data-cap="${cap}"${h === current ? ' aria-current="page"' : ""} aria-label="${esc(T(key))}"><i class="ph ${ic}" aria-hidden="true"></i><span>${esc(T(key))}</span></a>`;
  const nav = document.createElement("nav");
  nav.className = "nav";
  nav.setAttribute("aria-label", T("common.navigation"));
  nav.innerHTML = NAV.map((item) => link(item, "refresh nav-link")).join("") +
    `<button class="refresh nav-more" type="button" aria-haspopup="true" aria-expanded="false" aria-label="${esc(T("common.menu"))}"><i class="ph ph-list" aria-hidden="true"></i></button>` +
    `<div class="fleet-menu nav-menu" role="menu" hidden>${NAV.map((item) => link(item, "fleet-item nav-item").replace("<a ", '<a role="menuitem" ')).join("")}</div>`;
  meta.querySelector("#refresh") ? meta.insertBefore(nav, meta.querySelector("#refresh")) : meta.append(nav);
  const more = nav.querySelector(".nav-more"), navMenu = nav.querySelector(".nav-menu");
  // Enabled modules with a panel and a nav entry (manifest "nav") join the bar after Modules.
  fetch("api/modules", { cache: "no-store" }).then((r) => r.json()).then((d) => {
    const items = (d.modules || []).filter((m) => m.enabled && m.ok !== false && m.panel && m.nav)
      .map((m) => [m.panel, m.nav.icon || "ph-puzzle-piece", m.nav.label || m.name, ""]);
    if (!items.length) return;
    const mine = modPage && items.find(([h]) => h.startsWith(`modules/${modPage[1]}/`));
    if (mine) { current = mine[0]; nav.querySelectorAll('[aria-current="page"]').forEach((a) => a.removeAttribute("aria-current")); }
    const after = (box, cls) => {
      const anchor = box.querySelector('[href="modules.html"]');
      const html = items.map((it) => link(it, cls).replace(' data-cap=""', "")).join("");
      anchor ? anchor.insertAdjacentHTML("afterend", html) : box.insertAdjacentHTML("beforeend", html);
    };
    after(nav, "refresh nav-link");
    after(navMenu, "fleet-item nav-item");
    navMenu.querySelectorAll("a:not([role])").forEach((a) => a.setAttribute("role", "menuitem"));
    fit();
  }).catch(() => { /* no modules on this hub */ });
  // The bar must never squeeze the hub's name. When it does not fit (several modules with an entry of
  // their own, a long tailnet name), the items drop their labels first and then fold into the menu.
  const bar = meta.closest(".bar"), mark = bar && bar.querySelector(".mark");
  const fits = () => bar.scrollWidth <= bar.clientWidth + 1 && (!mark || mark.scrollWidth <= mark.clientWidth + 1);
  function fit() {
    if (!bar) return;
    nav.classList.remove("nav-icons", "nav-fold");
    if (fits()) return;
    nav.classList.add("nav-icons");
    if (!fits()) nav.classList.add("nav-fold");
  }
  if (bar && "ResizeObserver" in window) new ResizeObserver(fit).observe(bar);
  capsPromise.then(() => requestAnimationFrame(fit)).catch(() => {});  // the hub's name and mascot arrive here
  if (document.fonts) document.fonts.ready.then(fit);
  const closeNav = () => { navMenu.hidden = true; more.setAttribute("aria-expanded", "false"); };
  more.addEventListener("click", (e) => {
    e.stopPropagation();
    navMenu.hidden = !navMenu.hidden;
    more.setAttribute("aria-expanded", String(!navMenu.hidden));
  });
  document.addEventListener("click", (e) => { if (!nav.contains(e.target)) closeNav(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeNav(); });

  const icon = (h) => { const o = (h.os || "").toLowerCase(); return o === "android" ? "ph-device-mobile" : o === "linux" ? "ph-linux-logo" : o === "macos" ? "ph-apple-logo" : o === "windows" || h.kind === "pc" ? "ph-desktop-tower" : "ph-cpu"; };

  const wrap = document.createElement("div");
  wrap.className = "fleet";
  wrap.innerHTML = `<button class="refresh" type="button" aria-haspopup="true" aria-expanded="false" aria-label="${esc(T("fleet.menuAria"))}"><i class="ph ph-graph" aria-hidden="true"></i><span class="hide-sm">${T("fleet.button")}</span></button><div class="fleet-menu" role="menu" hidden></div>`;
  meta.prepend(wrap);
  const btn = wrap.querySelector("button"), menu = wrap.querySelector(".fleet-menu");

  const here = (h) => {
    try { const u = new URL(h.url); return u.hostname === location.hostname && (u.port || "443") === (location.port || "443"); }
    catch { return false; }
  };
  async function load() {
    try {
      const j = await (await fetch("api/fleet", { cache: "no-store" })).json(); // every hub implements it
      if (!j.hubs) throw new Error(j.error || "no data");
      // a hub URL comes from the mesh directory: only http(s) becomes a link
      menu.innerHTML = `<div class="fleet-head">${T("fleet.head")}</div>` + j.hubs.filter((h) => /^https?:\/\//i.test(String(h.url || ""))).map((h) => `
        <a role="menuitem" class="fleet-item${here(h) ? " here" : ""}${h.online ? "" : " off"}" href="${esc(h.url)}">
          ${window.HubMascot && HubMascot.enabled() ? HubMascot.svg(h.pub || h.name, { size: 24, state: !h.online ? "off" : h.coordinator ? "coord" : "ok" }) : `<i class="ph ${icon(h)}" aria-hidden="true"></i>`}
          <span><b>${esc(h.name)}</b><small>${here(h) || h.here ? T("fleet.here") : h.online ? esc(new URL(h.url).host) : T("fleet.offline")}${h.coordinator ? T("fleet.coord") : ""}</small></span>
        </a>`).join("");
    } catch (e) { menu.innerHTML = `<div class="fleet-head">${esc(T("fleet.error", { error: e.message }))}</div>`; }
  }
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = menu.hidden;
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", open);
    if (open) load();
  });
  document.addEventListener("click", (e) => { if (!wrap.contains(e.target)) { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); } });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); } });
})();
