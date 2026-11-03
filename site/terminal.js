// Remote terminal on any device: one-shot commands everywhere (PowerShell on Windows, bash on
// the rest) and an interactive shell on a PTY on every POSIX hub, carried through this one.
// Everything goes through the hub, which verifies your Tailscale identity.
(async () => {
  "use strict";
  await I18N.ready;
  const T = (k, v) => I18N.t(k, v);
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const out = $("#out"), cmd = $("#cmd");
  // A node hub (agent or core): its own terminal, directly. Decided by api/caps, not by port.
  let AGENT = false;
  let dev = new URLSearchParams(location.search).get("n") || "local";
  if (dev === (window.__hubCaps && window.__hubCaps.name)) dev = "local";
  let shellName = "bash";
  let state = { enabled: false, cwd: "" };
  let relay = false; // this hub can carry another hub's interactive shell
  const hist = JSON.parse(sessionStorage.getItem("hub-term-hist") || "[]");
  let hi = hist.length;

  let selfName = "", localHost = T("nodo.thisDevice"), localShell = "PowerShell";
  const label = () => dev === "self" ? selfName || T("nodo.thisDevice") : dev;
  const node = (p) => AGENT ? p.slice(1) : `api/nodes/${encodeURIComponent(dev)}${p}`;
  const ep = {
    info: () => dev === "local" ? "api/term" : node("/node"),
    enable: () => dev === "local" ? "api/term/enable" : node("/exec/enable"),
    exec: () => dev === "local" ? "api/term/exec" : node("/exec"),
  };
  const print = (text, cls) => {
    const span = document.createElement("span");
    if (cls) span.className = cls;
    span.textContent = text;
    out.append(span);
    out.scrollTop = out.scrollHeight;
  };
  const ps = () => /powershell/i.test(localShell);
  const prompt = () => dev === "local" ? (ps() ? `PS ${state.cwd}>` : `${localHost}:${state.cwd || "~"}$`) : `${label()}:${state.cwd || "~"}$`;
  function render() {
    $("#enabled").setAttribute("aria-checked", state.enabled);
    cmd.disabled = $("#run").disabled = !state.enabled;
    $("#prompt").textContent = prompt();
    cmd.placeholder = state.enabled ? T("terminal.commandPlaceholder") : T("terminal.commandDisabled");
  }

  async function devices() {
    const c = window.__hubCaps || await (await fetch("api/caps", { cache: "no-store" })).json().catch(() => ({}));
    // Hub without a mesh (old agent): its own terminal; mesh hubs (caps "nodes") show the selector
    AGENT = !new URLSearchParams(location.search).get("n") && (c.kind === "agent" || c.kind === "core") && !(c.caps || []).includes("nodes");
    if (AGENT) {
      document.querySelectorAll('a.refresh[href="control.html"]').forEach((el) => el.remove());
      const back = document.querySelector(".back");
      if (back) { back.setAttribute("href", "./"); back.lastChild.textContent = T("terminal.index"); }
      const i = await (await fetch("node", { cache: "no-store" })).json().catch(() => ({}));
      selfName = i.name || "";
      shellName = i.shell || "bash";
      if (selfName) document.querySelector(".mark").innerHTML = `<span>shoal</span><span class="slash">/</span><span>${esc(selfName.toLowerCase())}</span>`;
      $("#dev").innerHTML = `<option value="self">${esc(T("terminal.local", { name: i.name || T("nodo.thisDevice"), shell: shellName }))}</option>`;
      $("#dev").disabled = true;
      dev = "self";
      return;
    }
    const t = await (await fetch("api/term", { cache: "no-store" })).json().catch(() => ({}));
    localHost = t.host || localHost; localShell = t.shell || localShell; relay = !!t.relay;
    $("#dev").innerHTML = `<option value="local">${esc(T("terminal.local", { name: localHost, shell: localShell }))}</option>`;
    try {
      const j = await (await fetch("api/nodes", { cache: "no-store" })).json();
      $("#dev").innerHTML += (j.nodes || []).map((n) => `<option value="${esc(n.name)}"${n.online ? "" : " disabled"}>${esc(n.online ? T("terminal.local", { name: n.name, shell: n.shell || "bash" }) : T("terminal.offlineOption", { name: n.name, shell: n.shell || "bash" }))}</option>`).join("");
    } catch { /* only this device */ }
    if (dev === localHost) dev = "local";  // this hub, reached by name
    $("#dev").value = dev;
    if ($("#dev").value !== dev) dev = "local";
  }
  // ---------- banner: the hub's name in block letters next to its mascot ----------
  const FONT = {
    A: ".#.|#.#|###|#.#|#.#", B: "##.|#.#|##.|#.#|##.", C: ".##|#..|#..|#..|.##", D: "##.|#.#|#.#|#.#|##.",
    E: "###|#..|##.|#..|###", F: "###|#..|##.|#..|#..", G: ".##|#..|#.#|#.#|.##", H: "#.#|#.#|###|#.#|#.#",
    I: "###|.#.|.#.|.#.|###", J: "..#|..#|..#|#.#|.#.", K: "#.#|#.#|##.|#.#|#.#", L: "#..|#..|#..|#..|###",
    M: "#...#|##.##|#.#.#|#...#|#...#", N: "#..#|##.#|#.##|#..#|#..#", O: ".#.|#.#|#.#|#.#|.#.",
    P: "##.|#.#|##.|#..|#..", Q: ".#.|#.#|#.#|##.|.##", R: "##.|#.#|##.|#.#|#.#", S: ".##|#..|.#.|..#|##.",
    T: "###|.#.|.#.|.#.|.#.", U: "#.#|#.#|#.#|#.#|###", V: "#.#|#.#|#.#|#.#|.#.", W: "#...#|#...#|#.#.#|##.##|#...#",
    X: "#.#|#.#|.#.|#.#|#.#", Y: "#.#|#.#|.#.|.#.|.#.", Z: "###|..#|.#.|#..|###",
    0: "###|#.#|#.#|#.#|###", 1: ".#.|##.|.#.|.#.|###", 2: "##.|..#|.#.|#..|###", 3: "##.|..#|.#.|..#|##.",
    4: "#.#|#.#|###|..#|..#", 5: "###|#..|##.|..#|##.", 6: ".##|#..|###|#.#|###", 7: "###|..#|.#.|.#.|.#.",
    8: "###|#.#|###|#.#|###", 9: "###|#.#|###|..#|##.", "-": "...|...|###|...|...", "_": "...|...|...|...|###",
    ".": "...|...|...|...|.#.", " ": "..|..|..|..|..", "?": "##.|..#|.#.|...|.#.",
  };
  function blockText(text) {
    const rows = ["", "", "", "", ""];
    for (const ch of text.toUpperCase()) {
      const g = (FONT[ch] || FONT["?"]).split("|");
      g.forEach((r, i) => { rows[i] += r.replace(/#/g, "█").replace(/\./g, " ") + " "; });
    }
    return rows.map((r) => r.replace(/\s+$/, ""));
  }
  // Writes the banner into the interactive terminal: mascot, name and a status line, in colour.
  function banner(name, pub, info, cols) {
    const face = window.HubMascot && HubMascot.enabled() ? HubMascot.ansi(pub || name) : { lines: [], width: 0, color: "" };
    // Two pixel rows per line in half blocks: square pixels, and no gaps from the line height.
    const px = blockText(name), wide = Math.max(...px.map((r) => r.length));
    const at = (y, x) => (px[y] || "")[x] === "█";
    const text = [0, 2, 4].map((y) => Array.from({ length: wide }, (_, x) =>
      at(y, x) && at(y + 1, x) ? "█" : at(y, x) ? "▀" : at(y + 1, x) ? "▄" : " ").join("").replace(/\s+$/, ""));
    const width = wide;
    const acc = (() => { const c = cssVar("--accent"); const d = document.createElement("i"); d.style.color = c; document.body.append(d); const v = getComputedStyle(d).color.match(/\d+/g).slice(0, 3).join(";"); d.remove(); return v; })();
    const grad = (row) => [...row].map((ch, i) => ch === " " ? ch : `\x1b[38;2;${i / Math.max(1, width) > 0.5 ? acc : face.color || acc}m${ch}`).join("") + "\x1b[0m";
    // Never wider than the terminal (a wrapped line breaks the drawing): beside the mascot when it
    // all fits, otherwise below it; the name in block letters only when it fits the width.
    const clip = (t) => (t.length > cols ? t.slice(0, Math.max(1, cols - 1)) + "…" : t);
    const big = width <= cols;
    const side = face.width && face.width + 3 + Math.max(big ? width : name.length, info.length) <= cols;
    const block = big ? text.map(grad) : [`\x1b[1m${clip(name)}\x1b[0m`];
    const lines = [];
    if (side) {
      const h = Math.max(face.lines.length, block.length + 2);
      const top = Math.floor((h - block.length - 2) / 2);
      for (let i = 0; i < h; i++) {
        const ti = i - top;
        const tx = ti >= 0 && ti < block.length ? block[ti] : ti === block.length + 1 ? `\x1b[2m${info}\x1b[0m` : "";
        lines.push(`${face.lines[i] || " ".repeat(face.width)}   ${tx}`.replace(/\s+$/, ""));
      }
    } else {
      if (face.width <= cols) lines.push(...face.lines, "");
      lines.push(...block, `\x1b[2m${clip(info)}\x1b[0m`);
    }
    while (lines.length && !lines[0].replace(/\x1b\[[0-9;]*m/g, "").trim()) lines.shift();
    return lines.join("\r\n") + "\r\n\r\n";
  }
  // The same banner for the command mode: the letters as pixels (block characters leave gaps
  // between lines in a <pre>), next to the mascot.
  function bannerText(name, pub, info) {
    const rows = blockText(name), w = Math.max(...rows.map((r) => r.length));
    let rects = "";
    rows.forEach((r, y) => [...r].forEach((c, x) => { if (c !== " ") rects += `<rect x="${x}" y="${y}" width="1" height="1"/>`; }));
    const wrap = document.createElement("span");
    wrap.className = "t-banner";
    wrap.innerHTML = (window.HubMascot && HubMascot.enabled() ? HubMascot.svg(pub || name, { size: 64 }) : "") +
      `<span class="t-banner-text"><svg class="t-letters" viewBox="0 0 ${w} 5" width="${w * 7}" height="35" shape-rendering="crispEdges" aria-label="${esc(name)}">${rects}</svg><span class="t-dim"></span></span>`;
    wrap.querySelector(".t-dim").textContent = info;
    out.append(wrap, document.createTextNode("\n"));
  }
  let pubs = {};
  fetch("api/fleet", { cache: "no-store" }).then((r) => r.json()).then((f) => {
    for (const h of f.hubs || []) pubs[h.here ? "local" : h.name] = h.pub;
    showConsole();
  }).catch(() => {});
  function showConsole() {
    const name = dev === "local" ? localHost : label();
    $("#console-title").textContent = `${name} · ${dev === "local" ? localShell : shellName}`;
    $("#console-face").innerHTML = window.HubMascot && HubMascot.enabled() ? HubMascot.svg(pubs[dev] || name, { size: 18 }) : "";
  }

  async function load() {
    try {
      const j = await (await fetch(ep.info(), { cache: "no-store" })).json();
      if (j.error) throw new Error(j.error);
      state = dev === "local" ? j : { enabled: !!j.exec, cwd: state.cwd, remotePty: !!j.pty };
      if (dev !== "local" && j.shell) shellName = j.shell;  // the shell that device really runs
    } catch (e) { print(`${T("terminal.couldNotReach", { name: dev === "local" ? T("common.device") : dev, error: e.message })}\n`, "t-err"); state = { enabled: false, cwd: "" }; }
    render();
    setupMode();
    hintRemotePty();
    showConsole();
    bannerText(dev === "local" ? (state.host || localHost) : label(), pubs[dev], `${dev === "local" ? localShell : shellName} · ${T("terminal.remote")}`);
    print(state.enabled ? T("terminal.active") + "\n\n" : T("terminal.inactive") + "\n\n", "t-dim");
    if (state.enabled) cmd.focus();
  }

  $("#dev").addEventListener("change", (e) => {
    dev = e.target.value;
    history.replaceState(null, "", dev === "local" ? "terminal.html" : `terminal.html?n=${encodeURIComponent(dev)}`);
    state = { enabled: false, cwd: "" };
    stopPty();
    if (pty.term) pty.term.reset();
    print(`\n── ${dev === "local" ? (localHost || T("nodo.thisDevice")) : label()} ──\n`, "t-dim");
    load();
  });
  $("#enabled").addEventListener("click", async (e) => {
    const on = e.currentTarget.getAttribute("aria-checked") !== "true";
    if (on && !confirm(T("terminal.confirm", { name: dev === "local" ? (localHost || T("nodo.thisDevice")) : label() }))) return;
    const r = await fetch(ep.enable(), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on }) });
    const j = await r.json();
    if (!r.ok) return print(`${j.error}\n`, "t-err");
    if (j.pending) {
      const need = j.pending.need || 2;
      print(T("terminal.pending", { n: need - 1 }) + "\n\n", "t-dim");
      state.enabled = false;
      render();
      return;
    }
    state.enabled = dev === "local" ? j.enabled : j.exec;
    print(on ? T("terminal.enabledToast") + "\n\n" : T("terminal.disabledToast") + "\n\n", "t-dim");
    render();
    setupMode();
  });

  $("#form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const c = cmd.value.trim();
    if (!c) return;
    if (c === "cls" || c === "clear") { out.textContent = ""; cmd.value = ""; return; }
    hist.push(c); hi = hist.length;
    sessionStorage.setItem("hub-term-hist", JSON.stringify(hist.slice(-100)));
    print(`${prompt()} `, "t-prompt"); print(`${c}\n`, "t-cmd");
    cmd.value = ""; cmd.disabled = $("#run").disabled = true;
    try {
      const r = await fetch(ep.exec(), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cmd: c }) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      if (j.output.trim()) print(j.output, j.code ? "t-err" : "");
      print(`[${j.code === 0 ? T("terminal.ok") : T("terminal.code", { code: j.code })} · ${T("terminal.seconds", { n: j.secs })}]\n\n`, "t-dim");
      state.cwd = j.cwd;
    } catch (err) { print(`${err.message}\n\n`, "t-err"); }
    render();
    cmd.focus();
  });
  cmd.addEventListener("keydown", (e) => {
    if (e.key === "ArrowUp" && hi > 0) { cmd.value = hist[--hi]; e.preventDefault(); }
    if (e.key === "ArrowDown") { hi = Math.min(hist.length, hi + 1); cmd.value = hist[hi] || ""; e.preventDefault(); }
  });
  // ---------- interactive terminal (PTY over a WebSocket; other hubs' through this one) ----------
  // The shell lives on the hub and survives the connection: a phone that backgrounds the
  // page reconnects to the same session and gets its recent output back.
  const SESSION_KEY = "hub.pty.session";
  const KEYS = {
    esc: "\x1b", tab: "\t", up: "\x1b[A", down: "\x1b[B", right: "\x1b[C", left: "\x1b[D",
    "^c": "\x03", "^d": "\x04", "^z": "\x1a",
    f1: "\x1bOP", f2: "\x1bOQ", f3: "\x1bOR", f4: "\x1bOS", f5: "\x1b[15~", f6: "\x1b[17~", f7: "\x1b[18~",
    f8: "\x1b[19~", f9: "\x1b[20~", f10: "\x1b[21~", f11: "\x1b[23~", f12: "\x1b[24~",
    home: "\x1b[H", end: "\x1b[F", pgup: "\x1b[5~", pgdn: "\x1b[6~", ins: "\x1b[2~", del: "\x1b[3~",
  };
  const pty = { term: null, fit: null, ws: null, retry: 0, timer: 0, ended: false, ctrl: false, alt: false, wanted: false };
  const ptyAvailable = () => typeof window.Terminal === "function" &&
    (dev === "local" ? !!state.pty : dev !== "self" && relay && !!state.remotePty);
  const sessionKey = () => dev === "local" ? SESSION_KEY : `${SESSION_KEY}:${dev}`; // one session per hub
  const cssVar = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

  function setStatus(text, action, label) {
    const el = $("#pty-status"), btn = $("#pty-action");
    el.hidden = !text;
    el.querySelector("span").textContent = text || "";
    btn.hidden = !action;
    btn.textContent = label || "";
    btn.onclick = action || null;
  }

  // ANSI colours tuned to the hub palette, one set per scheme so both stay readable.
  // ANSI colours per theme: the prompt (green user@host, blue folder), ls and git use them.
  const PALETTES = {
    acid: {
      selectionBackground: "rgba(196, 234, 108, .28)",
      black: "#1b211d", red: "#ff7a6b", green: "#c4ea6c", yellow: "#e8d27a", blue: "#7fb4ff", magenta: "#d59cff", cyan: "#7fe0d0", white: "#d7ddd5",
      brightBlack: "#5d6861", brightRed: "#ff9d91", brightGreen: "#d8f59a", brightYellow: "#f3e2a0", brightBlue: "#a8ccff", brightMagenta: "#e5bdff", brightCyan: "#a6efe3", brightWhite: "#f3f6f1",
    },
    acidLight: {
      selectionBackground: "rgba(76, 122, 0, .22)",
      black: "#121512", red: "#b8321c", green: "#3f6a00", yellow: "#8a6500", blue: "#1f5fbf", magenta: "#8a3fb5", cyan: "#0f7a72", white: "#5c655d",
      brightBlack: "#48514a", brightRed: "#d4442c", brightGreen: "#4c7a00", brightYellow: "#a87c00", brightBlue: "#2f74d8", brightMagenta: "#a355cf", brightCyan: "#159085", brightWhite: "#121512",
    },
    abyss: {
      selectionBackground: "rgba(92, 200, 255, .28)",
      black: "#121a2b", red: "#ff6b81", green: "#7ee8a6", yellow: "#ffd166", blue: "#5cc8ff", magenta: "#c49bff", cyan: "#7fe7f0", white: "#d4def0",
      brightBlack: "#5f7088", brightRed: "#ff94a5", brightGreen: "#a6f2c3", brightYellow: "#ffe09a", brightBlue: "#94dcff", brightMagenta: "#dcc2ff", brightCyan: "#aef1f6", brightWhite: "#f2f6fc",
    },
    paper: {
      selectionBackground: "rgba(0, 113, 227, .18)",
      black: "#1d1d1f", red: "#d70015", green: "#1a7f37", yellow: "#9a6700", blue: "#0071e3", magenta: "#8944ab", cyan: "#0e7c86", white: "#6e6e73",
      brightBlack: "#86868b", brightRed: "#ff3b30", brightGreen: "#28a745", brightYellow: "#b78103", brightBlue: "#2f8cff", brightMagenta: "#af52de", brightCyan: "#1a9ca8", brightWhite: "#1d1d1f",
    },
  };
  function theme() {
    const base = { background: cssVar("--bg-2"), foreground: cssVar("--ink"), cursor: cssVar("--accent"), cursorAccent: cssVar("--bg-2") };
    const t = window.HubTheme ? HubTheme.get() : "acid";
    const light = getComputedStyle(document.documentElement).colorScheme === "light";
    return { ...base, ...(t === "abyss" ? PALETTES.abyss : t === "paper" ? PALETTES.paper : light ? PALETTES.acidLight : PALETTES.acid) };
  }

  const atBottom = (term) => term.buffer.active.viewportY >= term.buffer.active.baseY;

  // A "latest output" button appears when you have scrolled up and something new arrives; the
  // button (or typing) brings you back, and from the bottom the view follows the output again.
  function follow(term) {
    const btn = $("#pty-latest");
    const sync = () => { if (atBottom(term)) btn.hidden = true; };
    term.onWriteParsed(() => { if (!atBottom(term)) btn.hidden = false; });
    term.onScroll(sync);
    $("#xterm .xterm-viewport").addEventListener("scroll", sync, { passive: true });
    btn.addEventListener("click", () => { term.scrollToBottom(); btn.hidden = true; term.focus(); });
  }

  // Touch: xterm scrolls a whole line per step and stops dead when the finger lifts, which feels
  // stuck on a phone. In the normal screen the viewport moves by pixels and a flick keeps gliding;
  // full-screen programs (alternate screen: htop, less, vim) keep xterm's own handling, which
  // passes the scroll on to them.
  function touchScroll(term) {
    const box = $("#xterm");
    const viewport = () => box.querySelector(".xterm-viewport");
    let lastY = 0, lastT = 0, v = 0, raf = 0, active = false;
    box.addEventListener("touchstart", (e) => {
      cancelAnimationFrame(raf);
      v = 0;
      active = e.touches.length === 1 && term.buffer.active.type === "normal";
      if (active) { lastY = e.touches[0].clientY; lastT = e.timeStamp; }
    }, { capture: true, passive: true });
    box.addEventListener("touchmove", (e) => {
      if (!active || e.touches.length !== 1) return;
      const y = e.touches[0].clientY, dy = lastY - y, dt = Math.max(1, e.timeStamp - lastT);
      viewport().scrollTop += dy;
      v = 0.8 * (dy / dt) + 0.2 * v;  // px per ms, smoothed over the last moves
      lastY = y;
      lastT = e.timeStamp;
      e.stopPropagation();
      e.preventDefault();
    }, { capture: true, passive: false });
    box.addEventListener("touchend", () => {
      if (!active) return;
      active = false;
      let prev = performance.now();
      const glide = (now) => {
        const dt = now - prev;
        prev = now;
        v *= Math.pow(0.996, dt);  // friction
        if (Math.abs(v) < 0.02) return;
        viewport().scrollTop += v * dt;
        raf = requestAnimationFrame(glide);
      };
      raf = requestAnimationFrame(glide);
    }, { capture: true, passive: true });
  }

  // Phones whose browser does not shrink the page for the on-screen keyboard (iOS Safari, Samsung
  // Internet): size the page to what is really visible, so the newest line never hides behind it.
  if (window.visualViewport) {
    const vv = window.visualViewport;
    const fitPage = () => {
      document.documentElement.style.setProperty("--vvh", `${Math.round(vv.height)}px`);
      if (vv.offsetTop > 0) window.scrollTo(0, 0);
    };
    vv.addEventListener("resize", fitPage);
    vv.addEventListener("scroll", fitPage);
    fitPage();
  }

  async function ensureTerm() {
    if (pty.term) return pty.term;
    try { await document.fonts.load('13px "Geist Mono"'); } catch { /* falls back to a system mono */ }
    const touch = matchMedia("(pointer: coarse)").matches;
    const term = new window.Terminal({
      fontFamily: '"Geist Mono", ui-monospace, "Cascadia Mono", Menlo, monospace',
      fontSize: touch ? 12.5 : 13.5, lineHeight: 1.2, cursorBlink: true, scrollback: 5000,
      allowProposedApi: false, macOptionIsMeta: true, theme: theme(),
    });
    pty.fit = new window.FitAddon.FitAddon();
    term.loadAddon(pty.fit);
    term.open($("#xterm"));
    // GPU rendering: far smoother scrolling, above all on phones; the DOM renderer stays as fallback
    if (window.WebglAddon) {
      try {
        const gl = new window.WebglAddon.WebglAddon();
        gl.onContextLoss(() => gl.dispose());
        term.loadAddon(gl);
      } catch { /* no WebGL here */ }
    }
    follow(term);
    touchScroll(term);
    term.onData((d) => send({ t: "i", d: modify(d) }));
    term.onResize(({ cols, rows }) => send({ t: "r", c: cols, r: rows }));
    new ResizeObserver(() => {  // keyboard, rotation: refit, and stay at the bottom if you were there
      if ($("#pty").hidden) return;
      const stick = atBottom(term);
      try { pty.fit.fit(); } catch { /* not laid out yet */ }
      if (stick) term.scrollToBottom();
    }).observe($("#xterm"));
    matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", () => { term.options.theme = theme(); });
    addEventListener("hub-theme", () => { term.options.theme = theme(); });
    pty.term = term;
    return term;
  }

  function send(msg) {
    if (pty.ws && pty.ws.readyState === 1) pty.ws.send(JSON.stringify(msg));
  }

  // On-screen Ctrl and Alt hold for the next key, from the bar or the phone's keyboard.
  function setMod(name, on) {
    pty[name] = on;
    $(`[data-key="${name}"]`).setAttribute("aria-pressed", String(on));
  }
  function modify(d) {
    if (pty.ctrl && d.length === 1) {
      const c = d.toUpperCase().charCodeAt(0);
      if (c >= 64 && c <= 95) d = String.fromCharCode(c - 64); // Ctrl+letter
      else if (d === " ") d = "\x00";
    }
    if (pty.alt) d = "\x1b" + d; // Alt = ESC prefix, as terminals send it
    if (pty.ctrl) setMod("ctrl", false);
    if (pty.alt) setMod("alt", false);
    return d;
  }

  function connect() {
    clearTimeout(pty.timer);
    if (!pty.wanted || (pty.ws && pty.ws.readyState <= 1)) return;
    const term = pty.term;
    let id = "";
    try { id = sessionStorage.getItem(sessionKey()) || ""; } catch {}
    const url = new URL("api/term/pty", location.href);
    url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const q = { session: id, cols: term.cols, rows: term.rows };
    if (dev !== "local") q.node = dev;
    url.search = new URLSearchParams(q).toString();
    setStatus(T(pty.retry ? "terminal.reconnecting" : "terminal.connecting"));
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    pty.ws = ws;
    pty.ended = false;
    ws.onmessage = (e) => {
      if (typeof e.data !== "string") { term.write(new Uint8Array(e.data)); return; }
      let m; try { m = JSON.parse(e.data); } catch { return; }
      if (m.t === "hello") {
        try { sessionStorage.setItem(sessionKey(), m.session); } catch {}
        if (m.reattached) term.reset(); // the hub replays the recent output
        else term.write(banner(dev === "local" ? localHost : label(), pubs[dev], `${m.shell || "shell"} · ${T("terminal.remote")}`, term.cols));
        pty.retry = 0;
        setStatus("");
        send({ t: "r", c: term.cols, r: term.rows });
        term.focus();
      } else if (m.t === "exit") {
        pty.ended = true;
        try { sessionStorage.removeItem(sessionKey()); } catch {}
        setStatus(T("terminal.ended", { code: m.code ?? "-" }), () => { term.reset(); connect(); }, T("terminal.newSession"));
      } else if (m.t === "error") {
        pty.ended = true;
        setStatus(T("terminal.ptyError", { error: m.error }), () => connect(), T("common.retry"));
      }
    };
    ws.onclose = (e) => {
      if (pty.ws !== ws) return;
      pty.ws = null;
      if (pty.ended || !pty.wanted) return;
      if (e.code === 4000) { // the same session was opened in another tab or device
        setStatus(T("terminal.elsewhere"), () => { pty.retry = 0; connect(); }, T("terminal.takeOver"));
        return;
      }
      pty.retry = Math.min(pty.retry + 1, 6);
      setStatus(T("terminal.reconnecting"), () => { pty.retry = 0; connect(); }, T("terminal.reconnect"));
      if (!document.hidden) pty.timer = setTimeout(connect, Math.min(10000, 500 * 2 ** pty.retry));
    };
  }

  function stopPty() {
    pty.wanted = false;
    clearTimeout(pty.timer);
    if (pty.ws) { const ws = pty.ws; pty.ws = null; ws.close(); }
  }

  // A hub on older code cannot carry another hub's shell: then link straight to that hub.
  async function hintRemotePty() {
    const hint = $("#pty-hint");
    hint.hidden = true;
    if (dev === "local" || !state.remotePty || relay) return;
    try {
      const hubs = (await (await fetch("api/fleet", { cache: "no-store" })).json()).hubs || [];
      const hub = hubs.find((h) => h.name === dev);
      if (!hub || !hub.url) return;
      hint.querySelector("span").textContent = T("terminal.remoteInteractive", { name: dev });
      const a = hint.querySelector("a");
      a.href = new URL("terminal.html", hub.url).href;
      a.textContent = T("terminal.openInteractive");
      hint.hidden = false;
    } catch { /* without the directory there is nothing to link */ }
  }

  async function setupMode() {
    // Interactive whenever the device has a shell for it; one-shot commands only as the fallback.
    const show = ptyAvailable() && state.enabled;
    $("#pty").hidden = !show;
    $("#pty-end").hidden = !show;
    out.hidden = $("#form").hidden = show;
    document.body.classList.toggle("pty-on", show);
    if (!show) { stopPty(); return; }
    await ensureTerm();
    try { pty.fit.fit(); } catch {}
    pty.wanted = true;
    connect();
  }

  $("#pty-end").addEventListener("click", () => {
    if (!pty.ws) return;
    send({ t: "bye" }); // the hub hangs up the shell; its exit message offers a new session
  });
  $("#keys").addEventListener("pointerdown", (e) => { if (e.target.closest("button")) e.preventDefault(); }); // keep the keyboard open
  $("#keys").addEventListener("click", (e) => {
    const b = e.target.closest("[data-key]");
    if (!b || !pty.term) return;
    const k = b.dataset.key;
    if (k === "ctrl" || k === "alt") { setMod(k, !pty[k]); pty.term.focus(); return; }
    if (k === "fn") {  // the second layer: function keys and navigation
      const on = b.getAttribute("aria-pressed") !== "true";
      b.setAttribute("aria-pressed", String(on));
      $('[data-set="main"]').hidden = on;
      $('[data-set="fn"]').hidden = !on;
      pty.term.focus();
      return;
    }
    send({ t: "i", d: modify(KEYS[k] || k) });
    pty.term.focus();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && pty.wanted && !pty.ws && !pty.ended) { pty.retry = 0; connect(); }
  });

  devices().then(load);
})();
