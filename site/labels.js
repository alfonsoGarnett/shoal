/* Service labels: edit the custom title, description and visibility of a discovered route.
   The values are stored in the mesh CRDT and shown on every hub. When the service has a start
   hook on this hub (the command its switch runs), the dialog shows it too; saving a hook needs
   the hub's remote terminal to be enabled, which the hub enforces. */
(() => {
  "use strict";
  const T = (k, v) => I18N.t(k, v);
  let dlg = null, form = null, titleEl = null, aboutEl = null, errorEl = null, subEl = null, hiddenEl = null, hookEl = null;
  let current = null, onSaved = null;
  let initial = { name: "", about: "", hidden: false };
  let hook = null, hookInitial = "";
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let busy = false;

  function build() {
    if (dlg) return;
    dlg = document.createElement("dialog");
    dlg.className = "label-dlg";
    dlg.id = "label-dlg";
    dlg.setAttribute("aria-labelledby", "label-h");
    dlg.innerHTML = `
      <form class="label-form" method="dialog">
        <header class="label-head">
          <i class="ph ph-text-aa" aria-hidden="true"></i>
          <div>
            <h2 id="label-h" data-i18n="labels.edit">Edit title and description</h2>
            <p class="label-sub" id="label-sub"></p>
          </div>
          <button class="icon-btn" type="button" data-label-close aria-label="Close" data-i18n-aria="labels.close">
            <i class="ph ph-x" aria-hidden="true"></i>
          </button>
        </header>
        <label class="label-field"><span data-i18n="labels.title">Custom title</span>
          <input id="label-title" type="text" maxlength="80" autocomplete="off">
        </label>
        <label class="label-field"><span data-i18n="labels.about">Description</span>
          <textarea id="label-about" rows="4" maxlength="400"></textarea>
        </label>
        <label class="label-toggle">
          <span><b data-i18n="labels.hidden">Hide from the index</b><small data-i18n="labels.hiddenHint">Hidden on every hub; its URL keeps working.</small></span>
          <input type="checkbox" id="label-hidden" role="switch">
        </label>
        <section class="label-hook" id="label-hook" hidden></section>
        <p class="label-error" id="label-error" hidden></p>
        <div class="label-foot">
          <button class="btn btn-ghost btn-xs" type="button" data-label-reset>
            <i class="ph ph-arrow-counter-clockwise" aria-hidden="true"></i><span data-i18n="labels.automatic">Use automatic values</span>
          </button>
          <span class="label-spacer"></span>
          <button class="btn btn-ghost btn-xs" type="button" data-label-cancel><span data-i18n="labels.cancel">Cancel</span></button>
          <button class="btn btn-primary btn-xs" type="submit">
            <i class="ph ph-check" aria-hidden="true"></i><span data-i18n="labels.save">Save</span>
          </button>
        </div>
        <p class="label-hint" data-i18n="labels.hint">Saved on every hub of the tailnet and shown in the service catalogue.</p>
      </form>`;
    document.body.append(dlg);
    I18N.apply(dlg);
    form = dlg.querySelector(".label-form");
    titleEl = dlg.querySelector("#label-title");
    aboutEl = dlg.querySelector("#label-about");
    errorEl = dlg.querySelector("#label-error");
    subEl = dlg.querySelector("#label-sub");
    hiddenEl = dlg.querySelector("#label-hidden");
    hookEl = dlg.querySelector("#label-hook");

    const close = () => dlg.close();
    dlg.querySelector("[data-label-close]").addEventListener("click", close);
    dlg.querySelector("[data-label-cancel]").addEventListener("click", close);
    dlg.querySelector("[data-label-reset]").addEventListener("click", () => {
      if (current && !busy) save({ id: current.id, name: "", about: "" });
    });
    dlg.addEventListener("click", (e) => { if (e.target === dlg) close(); });
    dlg.addEventListener("close", () => { if (!dlg.open) { current = null; onSaved = null; } }); // it fires late: ignore it after a reopen
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (!current) return;
      const payload = { id: current.id };
      let changed = false;
      if (titleEl.value !== initial.name) { payload.name = titleEl.value; changed = true; }
      if (aboutEl.value !== initial.about) { payload.about = aboutEl.value; changed = true; }
      if (hiddenEl.checked !== initial.hidden) { payload.hidden = hiddenEl.checked; changed = true; }
      const hookBody = hook && hook.editable ? hookValues() : null;
      const hookChanged = hookBody && JSON.stringify(hookBody) !== hookInitial;
      if (!changed && !hookChanged) { dlg.close(); return; }
      save(changed ? payload : null, hookChanged ? hookBody : null);
    });
  }

  function setBusy(on) {
    busy = on;
    form.querySelectorAll("button").forEach((b) => { b.disabled = on; });
    dlg.classList.toggle("busy", on);
  }

  function showError(message) {
    errorEl.textContent = message;
    errorEl.hidden = !message;
  }

  function toast(message) {
    const t = document.querySelector("#toast");
    if (!t) return;
    t.textContent = message;
    t.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove("show"), 1800);
  }

  // ---------- start hook ----------
  function hookValues() {
    const v = (id) => hookEl.querySelector(id).value;
    return { cwd: v("#hook-cwd"), commands: v("#hook-commands"), env: v("#hook-env"), ports: v("#hook-ports") };
  }

  function renderHook() {
    if (!hook) { hookEl.hidden = true; hookEl.innerHTML = ""; return; }
    const ro = hook.editable ? "" : " readonly";
    const system = hook.kind !== "process";
    hookEl.innerHTML = `
      <header class="label-hook-head">
        <i class="ph ph-file-code" aria-hidden="true"></i>
        <h3>${esc(T("labels.hook"))}</h3>
        <span class="label-hook-state${hook.editable ? " on" : ""}"><i class="ph ${hook.editable ? "ph-lock-simple-open" : "ph-lock-simple"}" aria-hidden="true"></i>${esc(T(hook.editable ? "labels.hookEditable" : "labels.hookLocked"))}</span>
      </header>
      <p class="label-hint">${esc(T(system ? "labels.hookSystem" : "labels.hookHint", { file: hook.source, service: hook.service }))}</p>
      ${system ? "" : `
      <label class="label-field"><span>${esc(T("labels.hookCwd"))}</span><input id="hook-cwd" type="text" autocomplete="off" spellcheck="false" value="${esc(hook.cwd)}"${ro}></label>
      <label class="label-field"><span>${esc(T("labels.hookCommands"))}</span><textarea id="hook-commands" rows="3" spellcheck="false"${ro}>${esc(hook.commands)}</textarea></label>
      <label class="label-field"><span>${esc(T("labels.hookEnv"))}</span><textarea id="hook-env" rows="2" spellcheck="false" placeholder="NAME=value"${ro}>${esc(hook.env)}</textarea></label>
      <label class="label-field"><span>${esc(T("labels.hookPorts"))}</span><input id="hook-ports" type="text" inputmode="numeric" autocomplete="off" value="${esc(hook.ports)}"${ro}></label>`}
      ${hook.editable || system ? "" : `<p class="label-hint label-hook-lock">${esc(T("labels.hookUnlock"))} <a href="terminal.html">${esc(T("labels.hookOpenTerminal"))}</a></p>`}`;
    hookEl.hidden = false;
    hookInitial = hook.editable ? JSON.stringify(hookValues()) : "";
  }

  async function loadHook(id) {
    hook = null;
    renderHook();
    try {
      const r = await fetch(`api/services/${encodeURIComponent(id)}/hook`, { cache: "no-store" });
      if (!r.ok) return;
      const j = await r.json();
      if (current && current.id === id && j.kind) { hook = j; renderHook(); }
    } catch { /* no hook on this hub */ }
  }

  async function save(payload, hookBody) {
    const cb = onSaved, id = current ? current.id : "";
    setBusy(true);
    showError("");
    try {
      if (hookBody) {
        const h = await fetch(`api/services/${encodeURIComponent(id)}/hook`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(hookBody),
        });
        const hj = await h.json().catch(() => ({}));
        if (!h.ok) throw new Error(hj.error || `HTTP ${h.status}`);
      }
      if (!payload) {
        dlg.close();
        toast(T("labels.hookSaved"));
        if (cb) cb({ id, hook: true });
        return;
      }
      const r = await fetch("api/labels", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      dlg.close();
      toast(T("labels.saved"));
      if (cb) cb(j);
    } catch (e) {
      showError(T("labels.error", { error: e.message }));
    } finally {
      setBusy(false);
    }
  }

  function open(service, cb, opts = {}) {
    build();
    current = { id: String(service.id || "") };
    onSaved = cb || null;
    subEl.textContent = T("labels.service", { key: current.id });
    titleEl.value = service.name || "";
    aboutEl.value = service.about || "";
    hiddenEl.checked = !!service.hidden;
    initial = { name: titleEl.value, about: aboutEl.value, hidden: hiddenEl.checked };
    hook = null;
    if (opts.hook) loadHook(current.id); else renderHook();
    titleEl.placeholder = T("labels.titlePlaceholder");
    aboutEl.placeholder = T("labels.aboutPlaceholder");
    showError("");
    setBusy(false);
    dlg.showModal();
    titleEl.focus();
    titleEl.select();
  }

  window.HUB_LABELS = { open };
})();
