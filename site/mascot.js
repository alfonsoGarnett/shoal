// Pixel mascot for every hub, derived from its public key: the same key draws the same
// creature on every hub, every device and in the terminal. Pure function of the seed, no state.
// States: "ok", "off" (asleep: closed eyes, grey, z) and "coord" (wears the crown).
(() => {
  "use strict";
  const W = 14, H = 16;           // 12×12 body + 1 px outline, plus two rows for the crown
  const E = 0, BODY = 1, SHADE = 2, EYE = 3, LINE = 4, MOUTH = 5, ACC = 6, CROWN = 7, LID = 8, ZZZ = 9;

  function rng(seed) {
    let h = 1779033703 ^ seed.length;
    for (const c of seed) { h = Math.imul(h ^ c.charCodeAt(0), 3432918353); h = (h << 13) | (h >>> 19); }
    let a = h >>> 0;
    return () => {
      a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // Half-width of the body per row (0..6), rows 0..11 of the 12×12 body.
  const SHAPES = [
    [0, 0, 0, 3, 4, 5, 5, 5, 5, 5, 4, 0],   // blob
    [0, 0, 0, 5, 5, 5, 5, 5, 5, 5, 5, 0],   // bot
    [0, 0, 0, 3, 4, 5, 5, 5, 5, 5, 5, 0],   // ghost (ragged hem)
    [0, 0, 2, 3, 4, 4, 4, 4, 4, 4, 4, 0],   // tall
    [0, 0, 0, 0, 5, 6, 6, 6, 6, 6, 5, 0],   // wide
  ];
  const cache = new Map();

  function build(seed, state) {
    const key = seed + "|" + state;
    if (cache.has(key)) return cache.get(key);
    const r = rng(String(seed || "hub"));
    const pick = (n) => Math.floor(r() * n);
    const shape = pick(SHAPES.length), hw = SHAPES[shape];
    const half = Array.from({ length: 12 }, () => new Array(6).fill(E));
    const set = (x, y, v) => { if (x >= 0 && x < 6 && y >= 0 && y < 12) half[y][x] = v; };
    for (let y = 0; y < 12; y++) for (let x = 6 - hw[y]; x < 6; x++) set(x, y, BODY);
    if (shape === 2) for (let x = 0; x < 6; x += 2) set(x + (6 - hw[10]) % 2, 10, E);   // ghost hem
    else { const fx = 2 + pick(2); set(fx, 11, BODY); set(fx + 1, 11, BODY); }          // feet
    const top = hw.findIndex((w) => w > 0);
    // head piece: antenna, ears, horns, tuft or nothing
    const head = pick(5);
    if (head === 0) { const ax = 2 + pick(2); set(ax, top - 1, LINE); set(ax, top - 2, ACC); }
    if (head === 1) { const ex = 6 - hw[top]; set(ex, top - 1, BODY); set(ex, top - 2, BODY); set(ex + 1, top - 1, BODY); }
    if (head === 2) { const ex = 6 - hw[top]; set(ex, top - 1, ACC); set(ex - 1, top - 2, ACC); }
    if (head === 3) { set(5, top - 1, ACC); set(4, top - 2, ACC); }
    // belly or spots
    const pattern = pick(3);
    const ey = Math.min(top + 2 + pick(2), 6), ex = 3 + pick(2) - (shape === 3 ? 0 : 0);
    for (let y = ey + 2; y < 11; y++) for (let x = 0; x < 6; x++) {
      if (half[y][x] !== BODY) continue;
      if (pattern === 0 && x >= 4 && y >= ey + 3) half[y][x] = SHADE;
      if (pattern === 1 && r() < 0.18) half[y][x] = SHADE;
    }
    if (pattern === 2) for (let x = 0; x < 6; x++) if (half[ey + 3]?.[x] === BODY) half[ey + 3][x] = SHADE;   // stripe
    // eyes: dot, tall, wide or cyclops
    const eyes = pick(4), off = state === "off";
    const eye = (x, y) => set(x, y, off ? LID : EYE);
    if (eyes === 3) { eye(5, ey); if (!off) eye(5, ey + 1); }
    else {
      eye(ex, ey);
      if (eyes === 1 && !off) eye(ex, ey + 1);
      if (eyes === 2) eye(ex - 1, ey);
    }
    if (!off && eyes !== 3 && r() < 0.6 && half[ey + 1][ex - 1] === BODY) set(ex - 1, ey + 1, ACC);   // cheek
    // mouth
    const my = ey + (eyes === 1 ? 3 : 2), mouth = pick(4);
    if (mouth === 1) set(5, my, MOUTH);
    if (mouth === 2) { set(5, my, MOUTH); set(4, my, MOUTH); }
    if (mouth === 3) { set(5, my, MOUTH); set(4, my - 1, MOUTH); }
    // mirror into the 14×16 canvas (1 px margin, 2 rows on top for the crown), then outline
    const g = new Uint8Array(W * H);
    const at = (x, y) => (x < 0 || y < 0 || x >= W || y >= H) ? E : g[y * W + x];
    for (let y = 0; y < 12; y++) for (let x = 0; x < 6; x++) {
      const v = half[y][x];
      if (!v) continue;
      g[(y + 3) * W + x + 1] = v;
      g[(y + 3) * W + (11 - x) + 1] = v;
    }
    const filled = g.slice();
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      if (filled[y * W + x]) continue;
      const n = [[1, 0], [-1, 0], [0, 1], [0, -1]].some(([dx, dy]) => {
        const v = (x + dx < 0 || y + dy < 0 || x + dx >= W || y + dy >= H) ? E : filled[(y + dy) * W + x + dx];
        return v && v !== ACC && v !== LINE;
      });
      if (n) g[y * W + x] = LINE;
    }
    if (state === "coord") {  // sits right on the head, whatever its height
      let tr = 0;
      while (tr < H && !g.slice(tr * W, tr * W + W).some(Boolean)) tr++;
      const cy = Math.max(0, tr - 2);
      [[4, 0], [6, 0], [7, 0], [9, 0], [4, 1], [5, 1], [6, 1], [7, 1], [8, 1], [9, 1]].forEach(([x, y]) => { if (!at(x, cy + y)) g[(cy + y) * W + x] = CROWN; });
    }
    if (off) [[11, 0], [12, 0], [13, 0], [13, 1], [12, 2], [11, 3], [11, 4], [12, 4], [13, 4]].forEach(([x, y]) => { if (!at(x, y)) g[y * W + x] = ZZZ; });
    const hue = Math.floor(r() * 360), acc = (hue + 120 + pick(120)) % 360;
    const colors = off ? {
      [BODY]: "hsl(0 0% 46%)", [SHADE]: "hsl(0 0% 36%)", [EYE]: "#0b0d0c", [LINE]: "hsl(0 0% 14%)",
      [MOUTH]: "hsl(0 0% 20%)", [ACC]: "hsl(0 0% 56%)", [LID]: "hsl(0 0% 18%)", [ZZZ]: "hsl(0 0% 62%)",
    } : {
      [BODY]: `hsl(${hue} 62% 64%)`, [SHADE]: `hsl(${hue} 52% 49%)`, [EYE]: "#0b0d0c", [LINE]: `hsl(${hue} 38% 15%)`,
      [MOUTH]: `hsl(${hue} 42% 22%)`, [ACC]: `hsl(${acc} 78% 64%)`, [CROWN]: "#f4c64a", [LID]: `hsl(${hue} 38% 15%)`,
    };
    const out = { w: W, h: H, g, colors, hue, EYE };
    cache.set(key, out);
    return out;
  }

  const stateOf = (o) => (o && o.state) || "ok";

  // <svg> string; eyes carry class "m-eye" so CSS can make them blink.
  function svg(seed, opts = {}) {
    const m = build(seed, stateOf(opts)), size = opts.size || 28;
    let rects = "", eyes = "";
    for (let y = 0; y < m.h; y++) for (let x = 0; x < m.w; x++) {
      const v = m.g[y * m.w + x];
      if (!v) continue;
      const r = `<rect x="${x}" y="${y}" width="1" height="1" fill="${m.colors[v]}"/>`;
      if (v === m.EYE) eyes += r; else rects += r;
    }
    const delay = (m.hue % 37) / 10;
    const title = opts.title ? `<title>${String(opts.title).replace(/[<&>"]/g, "")}</title>` : "";
    return `<svg class="mascot${opts.cls ? " " + opts.cls : ""}" viewBox="0 0 ${m.w} ${m.h}" width="${size * m.w / m.h}" height="${size}" shape-rendering="crispEdges" role="img" aria-hidden="${title ? "false" : "true"}" style="--blink-delay:${delay}s">${title}${rects}<g class="m-eye">${eyes}</g></svg>`;
  }

  // Canvas: centred on (cx, cy), `px` screen pixels per sprite pixel; blink closes the eyes.
  function draw(ctx, seed, cx, cy, px, opts = {}) {
    const m = build(seed, stateOf(opts));
    const x0 = cx - (m.w * px) / 2, y0 = cy - (m.h * px) / 2 - (opts.lift || 0);
    // a fractional pixel size is fine: every edge lands on a whole device pixel, so no seams or blur
    const s = (ctx.getTransform && ctx.getTransform().a) || 1, snap = (v) => Math.round(v * s) / s;
    for (let y = 0; y < m.h; y++) for (let x = 0; x < m.w; x++) {
      let v = m.g[y * m.w + x];
      if (!v) continue;
      if (v === m.EYE && opts.blink) v = BODY;
      ctx.fillStyle = m.colors[v];
      const l = snap(x0 + x * px), t = snap(y0 + y * px);
      ctx.fillRect(l, t, snap(x0 + (x + 1) * px) - l, snap(y0 + (y + 1) * px) - t);
    }
  }

  // Terminal: lines of half blocks in 24-bit colour (two sprite rows per text line).
  function ansi(seed, opts = {}) {
    const m = build(seed, stateOf(opts));
    const rgb = (c) => { const d = document.createElement("i"); d.style.color = c; document.body.append(d); const v = getComputedStyle(d).color.match(/\d+/g).slice(0, 3).join(";"); d.remove(); return v; };
    const pal = {};
    for (const [k, c] of Object.entries(m.colors)) pal[k] = rgb(c);
    let first = 0;
    while (first < m.h && !m.g.slice(first * m.w, first * m.w + m.w).some(Boolean)) first++;
    first -= first % 2;
    const lines = [];
    for (let y = first; y < m.h; y += 2) {
      let s = "";
      for (let x = 0; x < m.w; x++) {
        const a = m.g[y * m.w + x], b = y + 1 < m.h ? m.g[(y + 1) * m.w + x] : 0;
        if (!a && !b) s += " ";
        else if (a && !b) s += `\x1b[38;2;${pal[a]}m▀\x1b[0m`;
        else if (!a && b) s += `\x1b[38;2;${pal[b]}m▄\x1b[0m`;
        else s += `\x1b[38;2;${pal[a]};48;2;${pal[b]}m▀\x1b[0m`;
      }
      lines.push(s);
    }
    return { lines, width: m.w, color: pal[BODY] };
  }

  // Off (Control › Settings) means plain nodes and icons everywhere; callers check this first.
  const enabled = () => !window.HubTheme || HubTheme.mascots();
  window.HubMascot = { svg, draw, ansi, enabled, size: { w: W, h: H } };
})();
