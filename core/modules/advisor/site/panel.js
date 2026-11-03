// Architect: read-only systems analyst. It explains; it never executes actions.
(async () => {
  "use strict";
  await I18N.ready;
  const T = (k, v) => I18N.t(k, v);
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const conversation = $("#conversation");
  const empty = $("#arch-empty");
  const input = $("#question");
  const askButton = $("#ask");
  const summaryButton = $("#summary");
  let status = null;
  let busy = false;

  const toast = (m) => {
    const t = $("#toast");
    t.textContent = m;
    t.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove("show"), 1600);
  };

  (window.__hubCaps ? Promise.resolve(window.__hubCaps) : fetch("api/caps", { cache: "no-store" }).then((r) => r.json())).then((c) => {
    if (c.name) document.title = T("advisor.title", { name: c.name });
  }).catch(() => {});

  function stateLabel(s) {
    if (s === "ready") return T("advisor.ready");
    if (s === "disabled") return T("advisor.disabled");
    if (s === "needs_config") return T("advisor.needsConfig");
    if (s === "error") return T("modules.state.error");
    const key = `modules.state.${s}`;
    const value = T(key);
    return value === key ? s : value;
  }

  function renderStatus() {
    const state = status?.state || "disabled";
    const ready = state === "ready";
    const stateEl = $("#arch-state");
    stateEl.textContent = stateLabel(state);
    stateEl.className = `arch-state ${state}`;
    const model = status?.model || status?.config?.values?.model || "";
    $("#arch-model").textContent = model ? T("advisor.model", { model }) : "";
    const values = status?.config?.values || {};
    const limit = Math.max(1, Number(values.daily_requests || 50));
    const used = Math.max(0, Number(status?.usage?.requests || 0));
    $("#arch-usage").textContent = T("advisor.usage", { used, limit });
    $("#arch-meter").style.width = `${Math.min(100, Math.round((used / limit) * 100))}%`;
    $("#arch-config").hidden = ready;
    askButton.disabled = summaryButton.disabled = !ready || busy;
    input.disabled = !ready || busy;
  }

  async function loadStatus() {
    try {
      status = await (await fetch("api/modules/advisor/status", { cache: "no-store" })).json();
    } catch {
      status = { state: "error", config: { values: {} }, usage: {} };
    }
    renderStatus();
  }

  // ---------- Markdown (safe subset) ----------
  function inline(text) {
    const codes = [];
    let s = esc(text);
    s = s.replace(/`([^`\n]+)`/g, (_, code) => {
      codes.push(code);
      return `\u0000${codes.length - 1}\u0000`;
    });
    s = s.replace(/\*\*\*([^*\n]+)\*\*\*/g, "<strong><em>$1</em></strong>");
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    s = s.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
    s = s.replace(/_([^_\n]+)_/g, "<em>$1</em>");
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
    return s;
  }

  function isBlockStart(line) {
    return /^(#{1,4}\s|```|>\s?|[-*+]\s|\d+[.)]\s|---\s*$|\*\*\*\s*$|___\s*$)/.test(line);
  }

  function tableRow(line) {
    return line.replace(/^\s*\|?/, "").replace(/\|?\s*$/, "").split("|").map((x) => x.trim());
  }

  function markdown(text) {
    const lines = String(text || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
    const out = [];
    let i = 0, list = null, code = null, codeLang = "";
    const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
    while (i < lines.length) {
      const line = lines[i];
      if (line.startsWith("```")) {
        if (!code) { closeList(); code = []; codeLang = line.slice(3).trim(); }
        else {
          out.push(`<pre><code${codeLang ? ` class="language-${esc(codeLang)}"` : ""}>${esc(code.join("\n"))}</code></pre>`);
          code = null; codeLang = "";
        }
        i++; continue;
      }
      if (code) { code.push(line); i++; continue; }
      if (!line.trim()) { closeList(); i++; continue; }
      if (/^(---|\*\*\*|___)\s*$/.test(line)) { closeList(); out.push("<hr>"); i++; continue; }
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) { closeList(); const n = h[1].length; out.push(`<h${n}>${inline(h[2])}</h${n}>`); i++; continue; }
      if (/^>\s?/.test(line)) {
        closeList(); const quote = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) { quote.push(lines[i].replace(/^>\s?/, "")); i++; }
        out.push(`<blockquote>${inline(quote.join(" "))}</blockquote>`); continue;
      }
      if (line.includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        closeList();
        const head = tableRow(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) { rows.push(tableRow(lines[i])); i++; }
        out.push(`<div class="arch-table-wrap"><table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
        continue;
      }
      const ul = line.match(/^\s*[-*+]\s+(.*)$/);
      const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        const tag = ul ? "ul" : "ol";
        if (list !== tag) { closeList(); out.push(`<${tag}>`); list = tag; }
        out.push(`<li>${inline((ul || ol)[1])}</li>`); i++; continue;
      }
      closeList();
      const para = [line]; i++;
      while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i]) &&
             !(lines[i].includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1]))) {
        para.push(lines[i]); i++;
      }
      out.push(`<p>${inline(para.join(" "))}</p>`);
    }
    closeList();
    if (code) out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
    return out.join("\n");
  }

  // ---------- conversation ----------
  function scroll() {
    requestAnimationFrame(() => { conversation.scrollTop = conversation.scrollHeight; });
  }

  function addMessage(role, text, meta = "", { error = false, raw = text } = {}) {
    empty.hidden = true;
    const article = document.createElement("article");
    article.className = `arch-msg ${role}${error ? " error" : ""}`;
    const head = document.createElement("div");
    head.className = "arch-msg-head";
    const who = role === "user" ? T("advisor.you") : T("advisor.architect");
    head.innerHTML = `<span class="arch-role">${esc(who)}</span>${meta ? `<span class="arch-msg-meta">${esc(meta)}</span>` : ""}`;
    if (role === "assistant") {
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "arch-copy";
      copy.innerHTML = `<i class="ph ph-copy" aria-hidden="true"></i>`;
      copy.title = T("advisor.copy");
      copy.setAttribute("aria-label", T("advisor.copy"));
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(raw); toast(T("advisor.copied")); } catch { /* ignore */ }
      });
      head.append(copy);
    }
    const bubble = document.createElement("div");
    bubble.className = "arch-bubble arch-body";
    if (role === "assistant" && !error) bubble.innerHTML = markdown(text);
    else bubble.textContent = text;
    article.append(head, bubble);
    conversation.append(article);
    scroll();
    return article;
  }

  function addTyping() {
    empty.hidden = true;
    const el = document.createElement("div");
    el.className = "arch-thinking";
    el.innerHTML = `<span class="arch-role">${esc(T("advisor.architect"))}</span><span class="arch-dots" aria-hidden="true"><i></i><i></i><i></i></span><span class="arch-thinking-label">${esc(T("advisor.thinking"))}</span>`;
    conversation.append(el);
    scroll();
    return el;
  }

  function setBusy(on) {
    busy = on;
    askButton.disabled = summaryButton.disabled = !status || status.state !== "ready" || on;
    input.disabled = !status || status.state !== "ready" || on;
    conversation.setAttribute("aria-busy", on ? "true" : "false");
  }

  async function ask(mode = "ask", prompt = "") {
    if (busy) return;
    const question = String(prompt || input.value || "").trim();
    if (mode === "ask" && !question) return;
    if (!status || status.state !== "ready") {
      addMessage("assistant", T("advisor.needsConfig"), "", { error: true });
      return;
    }
    const display = mode === "summary" ? T("advisor.summary") : question;
    addMessage("user", display, new Date().toLocaleTimeString(I18N.locale, { hour: "2-digit", minute: "2-digit" }));
    input.value = "";
    const typing = addTyping();
    setBusy(true);
    const t0 = performance.now();
    try {
      const url = mode === "summary" ? "api/modules/advisor/summary" : "api/modules/advisor/ask";
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      typing.remove();
      const ms = Math.round(performance.now() - t0);
      const article = addMessage("assistant", j.answer || "", `${j.model || status.model || ""} · ${T("advisor.elapsed", { ms })}`);
      if (j.truncated) {
        const note = document.createElement("div");
        note.className = "arch-truncated";
        note.textContent = T("advisor.truncated");
        article.append(note);
      }
      await loadStatus();
    } catch (e) {
      typing.remove();
      const friendly = /token budget|reasoning before producing/i.test(e.message)
        ? T("advisor.tokenBudget")
        : T("advisor.error", { error: e.message });
      addMessage("assistant", friendly, "", { error: true });
    } finally {
      setBusy(false);
    }
  }

  function clearSession() {
    conversation.querySelectorAll(".arch-msg, .arch-thinking").forEach((el) => el.remove());
    empty.hidden = false;
    input.value = "";
    input.focus();
  }

  $("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); ask("ask"); });
  summaryButton.addEventListener("click", () => ask("summary"));
  $("#arch-clear").addEventListener("click", clearSession);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask("ask"); }
  });
  $("#arch-chips").addEventListener("click", (e) => {
    const chip = e.target.closest("[data-prompt]");
    if (chip) ask("ask", T(chip.dataset.prompt));
  });

  // Fill chip labels from the catalogs after i18n is ready.
  document.querySelectorAll("#arch-chips [data-prompt]").forEach((chip) => { chip.textContent = T(chip.dataset.prompt); });

  loadStatus();
})();
