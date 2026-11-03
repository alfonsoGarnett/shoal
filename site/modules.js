// Modules page: enable and configure built-in optional modules.
(async () => {
  "use strict";
  await I18N.ready;
  const T = (k, v) => I18N.t(k, v);
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = (m) => { const t = $("#toast"); t.textContent = m; t.classList.add("show"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 1800); };
  const getJ = async (u) => (await fetch(u, { cache: "no-store" })).json();
  const stateLabel = (s) => {
    const key = `modules.state.${s}`;
    const value = T(key);
    return value === key ? s : value;
  };

  fetch("api/caps", { cache: "no-store" }).then((r) => r.json()).then((c) => {
    if (c.name) document.title = T("modules.title", { name: c.name });
  }).catch(() => {});

  function field(mid, key, spec, value) {
    if (key === "enabled") return "";
    const label = spec.label ? T(spec.label) : key;
    const id = `${mid}-${key}`;
    let input;
    const val = value && typeof value === "object" && "value" in value ? value.value : value;
    if (spec.type === "secret") {
      const configured = value && typeof value === "object" && value.configured;
      input = `<input class="mod-input" id="${id}" name="${esc(key)}" type="password" autocomplete="new-password" placeholder="${esc(configured ? T("modules.secretSet") : T("modules.secretEmpty"))}">`;
    } else if (spec.type === "select") {
      input = `<select class="mod-input" id="${id}" name="${esc(key)}">${(spec.options || []).map((o) => {
        const key2 = `${spec.label}.${o}`;
        const label2 = T(key2) === key2 ? o : T(key2);
        return `<option value="${esc(o)}"${String(val) === String(o) ? " selected" : ""}>${esc(label2)}</option>`;
      }).join("")}</select>`;
    } else if (spec.type === "bool") {  // a switch row, like the toggles in Control
      return `<label class="label-toggle mod-switch"><b>${esc(label)}</b><input id="${id}" name="${esc(key)}" type="checkbox"${val ? " checked" : ""}></label>`;
    } else if (spec.type === "int") {
      input = `<input class="mod-input" inputmode="numeric" id="${id}" name="${esc(key)}" type="number" value="${esc(val ?? 0)}"${spec.min != null ? ` min="${spec.min}"` : ""}${spec.max != null ? ` max="${spec.max}"` : ""}>`;
    } else {
      input = `<input class="mod-input" id="${id}" name="${esc(key)}" type="text" spellcheck="false" value="${esc(val ?? "")}">`;
    }
    return `<label class="mod-field"><span>${esc(label)}</span>${input}</label>`;
  }

  function card(m) {
    const values = m.config?.values || {};
    const schema = m.config?.schema || {};
    const enabled = !!m.enabled;
    const nameKey = `module.${m.id}.name`;
    const descKey = `module.${m.id}.description`;
    const name = T(nameKey) === nameKey ? m.name : T(nameKey);
    const description = T(descKey) === descKey ? (m.description || "") : T(descKey);
    const fields = Object.entries(schema).map(([k, s]) => field(m.id, k, s, values[k])).join("");
    const panel = m.panel && enabled ? `<a class="btn btn-ghost" href="${esc(m.panel)}"><i class="ph ph-arrow-up-right" aria-hidden="true"></i>${T("modules.open")}</a>` : "";
    const noteKey = `module.${m.id}.note`;
    const note = T(noteKey) === noteKey ? "" : T(noteKey);
    return `<article class="mod-card frame${enabled ? "" : " is-off"}" data-module="${esc(m.id)}"><div class="in">
      <header class="mod-head">
        <div class="mod-title"><h2>${esc(name)}</h2>
          <span class="mod-meta"><span>${esc(T("modules.version", { version: m.version }))}</span><span>${esc(T("modules.apiVersion", { version: m.api }))}</span></span></div>
        <span class="mod-state ${esc(m.state || "")}">${esc(stateLabel(m.state || "disabled"))}</span>
      </header>
      <p class="mod-desc">${esc(description)}</p>
      <label class="label-toggle mod-switch mod-enable"><b>${T("modules.enabled")}</b><input type="checkbox" data-enabled="${esc(m.id)}"${enabled ? " checked" : ""}></label>
      <form class="mod-form" data-form="${esc(m.id)}">
        <div class="mod-fields">${fields || `<p class="mod-desc">${T("modules.noConfig")}</p>`}</div>
        <div class="mod-foot">
          <button class="btn btn-primary" type="submit"><i class="ph ph-floppy-disk" aria-hidden="true"></i>${T("modules.save")}</button>
          ${panel}
        </div>
      </form>
      ${note ? `<p class="mod-note"><i class="ph ph-info" aria-hidden="true"></i><span>${esc(note)}</span></p>` : ""}
    </div></article>`;
  }

  async function load() {
    try {
      const data = await getJ("api/modules");
      const list = data.modules || [];
      $("#modules").innerHTML = list.length ? list.map(card).join("") : `<p class="empty-line">${T("modules.noConfig")}</p>`;
    } catch (e) {
      $("#modules").innerHTML = `<p class="empty-line">${esc(T("modules.loadError", { error: e.message }))}</p>`;
    }
  }

  $("#modules").addEventListener("submit", async (e) => {
    const form = e.target.closest("[data-form]");
    if (!form) return;
    e.preventDefault();
    const mid = form.dataset.form;
    const values = {};
    for (const el of form.querySelectorAll("input, select")) {
      if (!el.name) continue;
      if (el.type === "checkbox") values[el.name] = el.checked;
      else if (el.type === "number") values[el.name] = Number(el.value);
      else if (el.type === "password" && !el.value) continue;
      else values[el.name] = el.value;
    }
    const toggle = document.querySelector(`[data-enabled="${mid}"]`);
    if (toggle) values.enabled = toggle.checked;
    try {
      await fetch(`api/modules/${encodeURIComponent(mid)}/config`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values) });
      toast(T("modules.saved"));
      await load();
    } catch (err) { toast(T("modules.failed", { error: err.message })); }
  });

  $("#modules").addEventListener("change", async (e) => {
    const t = e.target.closest("[data-enabled]");
    if (!t) return;
    const mid = t.dataset.enabled;
    const card = document.querySelector(`[data-module="${mid}"] h2`);
    const name = card ? card.textContent.trim() : mid;
    if (!t.checked && !confirm(T("modules.confirmDisable", { name }))) { t.checked = true; return; }
    try {
      await fetch(`api/modules/${encodeURIComponent(mid)}/config`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: t.checked }) });
      toast(T("modules.saved"));
      await load();
    } catch (err) { t.checked = !t.checked; toast(T("modules.failed", { error: err.message })); }
  });

  load();
})();
