// Command palette (⌘K / Ctrl+K, or the bar button): jump to any page, hub, hub terminal or
// network service, and switch theme or language, from any page of any hub.
(async () => {
  "use strict";
  await I18N.ready;
  const T = (k, v) => I18N.t(k, v);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const meta = document.querySelector(".bar-meta");
  if (!meta) return;
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

  const btn = document.createElement("button");
  btn.className = "refresh kbar-btn";
  btn.type = "button";
  btn.setAttribute("aria-label", T("palette.open"));
  btn.innerHTML = `<i class="ph ph-command" aria-hidden="true"></i><kbd class="hide-sm">${mac ? "⌘" : "Ctrl"} K</kbd>`;
  meta.prepend(btn);

  const dlg = document.createElement("dialog");
  dlg.className = "kbar";
  dlg.setAttribute("aria-label", T("palette.title"));
  dlg.innerHTML = `
    <div class="kbar-top"><i class="ph ph-magnifying-glass" aria-hidden="true"></i>
      <input type="text" autocomplete="off" spellcheck="false" placeholder="${esc(T("palette.placeholder"))}" aria-controls="kbar-list" role="combobox" aria-expanded="true">
      <kbd>esc</kbd></div>
    <ul class="kbar-list" id="kbar-list" role="listbox"></ul>
    <div class="kbar-foot"><span><kbd>↑</kbd><kbd>↓</kbd> ${esc(T("palette.move"))}</span><span><kbd>↵</kbd> ${esc(T("palette.go"))}</span></div>`;
  document.body.append(dlg);
  const input = dlg.querySelector("input"), list = dlg.querySelector("ul");

  const PAGES = [
    ["./", "ph-house", "common.index"], ["nodo.html", "ph-device-mobile", "common.device"],
    ["control.html", "ph-sliders-horizontal", "common.control"], ["terminal.html", "ph-terminal-window", "common.terminal"],
    ["modules.html", "ph-puzzle-piece", "common.modules"], ["avisos.html", "ph-bell", "common.notifications"],
    ["help.html", "ph-question", "common.help"],
  ];
  let items = [], shown = [], sel = 0, remote = null;

  function base() {
    const out = PAGES.map(([href, icon, key]) => ({ group: T("palette.pages"), icon, label: T(key), href }));
    for (const t of window.HubTheme ? HubTheme.list : []) {
      out.push({ group: T("palette.appearance"), icon: "ph-palette", label: T("palette.theme", { name: T("theme." + t) }),
        hint: HubTheme.get() === t ? "●" : "", run: () => HubTheme.set(t) });
    }
    if (window.HubTheme) out.push({ group: T("palette.appearance"), icon: "ph-text-aa", label: T(HubTheme.pixel() ? "palette.pixelOff" : "palette.pixelOn"),
      run: () => HubTheme.setPixel(!HubTheme.pixel()) });
    if (window.HubTheme) out.push({ group: T("palette.appearance"), icon: "ph-smiley", label: T(HubTheme.mascots() ? "palette.mascotsOff" : "palette.mascotsOn"),
      run: () => { HubTheme.setMascots(!HubTheme.mascots()); setTimeout(() => location.reload(), 300); } });
    for (const [code, name] of [["en", "English"], ["es", "Español"]]) {
      out.push({ group: T("palette.appearance"), icon: "ph-translate", label: name, hint: I18N.lang === code ? "●" : "",
        run: () => I18N.setLang(code).then(() => location.reload()) });
    }
    return out;
  }
  async function loadRemote() {
    const out = [];
    try {
      const f = await (await fetch("api/fleet", { cache: "no-store" })).json();
      for (const h of f.hubs || []) {
        const face = window.HubMascot && HubMascot.enabled() ? HubMascot.svg(h.pub || h.name, { size: 18, state: !h.online ? "off" : h.coordinator ? "coord" : "ok" }) : "";
        out.push({ group: T("palette.hubs"), face, label: h.name, hint: h.here ? T("fleet.here") : h.online ? "" : T("fleet.offline"), href: h.url });
        if (h.online) out.push({ group: T("palette.hubs"), icon: "ph-terminal-window", label: T("palette.terminalOn", { name: h.name }),
          href: h.here ? "terminal.html" : `terminal.html?n=${encodeURIComponent(h.name)}` });
      }
    } catch { /* no mesh */ }
    try {  // panels of the modules enabled on this hub, whatever module brings them
      const d = await (await fetch("api/modules", { cache: "no-store" })).json();
      for (const m of d.modules || []) {
        if (!m.enabled || m.ok === false || !m.panel) continue;
        const key = `module.${m.id}.name`, name = T(key) === key ? m.name : T(key);
        out.push({ group: T("palette.pages"), icon: (m.nav && m.nav.icon) || "ph-puzzle-piece", label: name, href: m.panel });
      }
    } catch { /* no modules */ }
    try {
      const c = await (await fetch("api/catalog", { cache: "no-store" })).json();
      for (const s of c.services || []) {
        // the service's own address when it has one, so it does not run inside the hub's origin
        const href = /^https?:\/\//i.test(String(s.url || "")) ? s.url : `api/goto/${s.key}${String(s.key).endsWith("/") ? "" : "/"}`;
        out.push({ group: T("palette.services"), icon: "ph-compass", label: s.name, hint: s.hub, href, off: !s.online });
      }
    } catch { /* no catalog */ }
    return out;
  }

  // Subsequence match: consecutive letters and word starts score higher.
  function score(text, q) {
    if (!q) return 1;
    text = text.toLowerCase();
    let s = 0, i = 0, run = 0;
    for (const c of q) {
      const j = text.indexOf(c, i);
      if (j < 0) return 0;
      run = j === i ? run + 1 : 0;
      s += 1 + run * 2 + (j === 0 || /[\s/\-.]/.test(text[j - 1]) ? 3 : 0);
      i = j + 1;
    }
    return s - text.length * 0.01;
  }
  function render() {
    const q = input.value.trim().toLowerCase();
    shown = items.map((it) => ({ it, s: score(`${it.label} ${it.hint || ""} ${it.group}`, q) })).filter((x) => x.s > 0);
    if (q) shown.sort((a, b) => b.s - a.s);
    shown = shown.map((x) => x.it).slice(0, 60);
    sel = Math.min(sel, Math.max(0, shown.length - 1));
    let last = "";
    list.innerHTML = shown.length ? shown.map((it, i) => {
      const head = !q && it.group !== last ? `<li class="kbar-group" role="presentation">${esc(it.group)}</li>` : "";
      last = it.group;
      return `${head}<li role="option" id="kbar-${i}" class="kbar-item${it.off ? " off" : ""}" data-i="${i}" aria-selected="${i === sel}">
        <span class="kbar-ic">${it.face || `<i class="ph ${it.icon}" aria-hidden="true"></i>`}</span>
        <span class="kbar-label">${esc(it.label)}</span><span class="kbar-hint">${esc(it.hint || "")}</span></li>`;
    }).join("") : `<li class="kbar-empty">${esc(T("palette.empty"))}</li>`;
    input.setAttribute("aria-activedescendant", shown.length ? `kbar-${sel}` : "");
    list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }
  function go(it) {
    if (!it) return;
    dlg.close();
    if (it.run) it.run(); else location.href = it.href;
  }
  async function open() {
    if (dlg.open) return;
    items = base().concat(remote || []);
    input.value = ""; sel = 0;
    render();
    dlg.showModal();
    input.focus();
    if (!remote) { remote = await loadRemote(); if (dlg.open) { items = base().concat(remote); render(); } }
  }
  btn.addEventListener("click", open);
  document.addEventListener("keydown", (e) => {
    if (!(e.metaKey || e.ctrlKey) || e.key.toLowerCase() !== "k") return;
    if (e.ctrlKey && !e.metaKey && e.target.closest && e.target.closest(".xterm")) return;  // the shell's Ctrl+K
    e.preventDefault();
    dlg.open ? dlg.close() : open();
  });
  input.addEventListener("input", () => { sel = 0; render(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { sel = Math.min(shown.length - 1, sel + 1); render(); e.preventDefault(); }
    if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); render(); e.preventDefault(); }
    if (e.key === "Enter") { go(shown[sel]); e.preventDefault(); }
  });
  list.addEventListener("click", (e) => { const li = e.target.closest("[data-i]"); if (li) go(shown[+li.dataset.i]); });
  list.addEventListener("pointermove", (e) => {
    const li = e.target.closest("[data-i]");
    if (li && +li.dataset.i !== sel) { sel = +li.dataset.i; list.querySelectorAll("[aria-selected]").forEach((x) => x.setAttribute("aria-selected", String(+x.dataset.i === sel))); }
  });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });  // backdrop
})();
