// Loading placeholders as moving binary/ASCII noise, decoded into the real text when it arrives.
//   <h1 data-scramble>Hub</h1>      a text that some script will replace: it scrambles until then
//   <ol data-fill></ol>             a list that arrives empty: a scrambling line until it gets content
// A placeholder that is only translated (data-i18n with the same key) keeps scrambling; any other
// text written into it is the real one. Screen readers get aria-busy instead of noise.
(() => {
  "use strict";
  const GLYPHS = "01010101<>/\\{}[]#$%&*+=?;:.";
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const live = new Map();  // element -> { len, frozen }
  const noise = (n, keep = "") => Array.from({ length: n }, (_, i) =>
    keep[i] === " " ? " " : GLYPHS[(Math.random() * GLYPHS.length) | 0]).join("");
  // Our own writes must not look like real content. MutationObserver reports them later (as a
  // microtask), so a flag set around the write is already cleared by then: remember what we wrote.
  const ours = new WeakMap();
  const put = (el, text) => { el.textContent = text; ours.set(el, el.textContent); };
  const mine = (el) => ours.get(el) === el.textContent;

  function placeholderText(el, text) {
    const key = el.dataset.i18n;
    if (!key) return false;
    const t = window.I18N && I18N.t ? I18N.t(key) : null;
    return text === key || text === t || text === el.dataset.scrambleFrom;
  }

  function start(el) {
    if (live.has(el)) return;
    if (el.children.length) { el.classList.add("scramble-done"); return; }
    const from = el.textContent.trim();
    el.dataset.scrambleFrom = from;
    live.set(el, { len: Math.max(4, Math.min(from.length || 8, 32)), shape: from });
    el.classList.add("scrambling");
    el.setAttribute("aria-busy", "true");
    put(el, noise(live.get(el).len, from));
    new MutationObserver((records, obs) => {
      if (!el.children.length && mine(el)) return;  // the shimmer itself, not the real text
      const st = live.get(el);
      if (!st) return obs.disconnect();
      const text = el.textContent;
      if (!el.children.length && placeholderText(el, text.trim())) {  // only translated: still loading
        st.shape = text.trim();
        st.len = Math.max(4, Math.min(st.shape.length, 32));
        return;
      }
      obs.disconnect();
      stop(el, el.children.length ? null : text);
    }).observe(el, { childList: true, characterData: true, subtree: true });
  }

  function stop(el, finalText) {
    live.delete(el);
    el.removeAttribute("aria-busy");
    el.classList.add("scramble-done");  // the CSS stand-in (before this script ran) must not come back
    if (finalText == null || reduce || !finalText.trim()) { el.classList.remove("scrambling"); return; }
    // decode: the real text appears from left to right out of the noise
    const t0 = performance.now(), dur = Math.min(700, 200 + finalText.length * 25);
    const step = (now) => {
      if (live.has(el) || el.textContent !== frame.last) { el.classList.remove("scrambling"); return; }  // replaced again
      const k = Math.min(1, (now - t0) / dur), n = Math.floor(finalText.length * k);
      frame.last = finalText.slice(0, n) + noise(finalText.length - n, finalText.slice(n));
      put(el, frame.last);
      if (k < 1) requestAnimationFrame(step); else el.classList.remove("scrambling");
    };
    const frame = { last: noise(finalText.length, finalText) };
    put(el, frame.last);
    requestAnimationFrame(step);
  }

  function fill(box) {
    if (box.dataset.fillDone) return;
    if (box.children.length || box.textContent.trim()) { box.dataset.fillDone = "1"; return; }
    const line = document.createElement(box.tagName === "OL" || box.tagName === "UL" ? "li" : "span");
    line.className = "scramble-fill";
    line.setAttribute("aria-hidden", "true");
    line.textContent = "loading";
    box.setAttribute("aria-busy", "true");
    box.append(line);
    live.set(line, { len: 18 + ((Math.random() * 14) | 0), shape: "" });
    put(line, noise(live.get(line).len));
    new MutationObserver((records, obs) => {
      const others = [...box.childNodes].some((n) => n !== line && (n.nodeType === 1 || n.textContent.trim()));
      if (!others && line.isConnected) return;
      obs.disconnect();
      box.dataset.fillDone = "1";
      box.removeAttribute("aria-busy");
      live.delete(line);
      if (line.isConnected) line.remove();  // content was appended next to it
    }).observe(box, { childList: true, characterData: true, subtree: true });
  }

  function scan(root = document) {
    root.querySelectorAll("[data-scramble]").forEach(start);
    root.querySelectorAll("[data-fill]").forEach(fill);
  }

  // one timer for every placeholder: a few characters change per frame, so it shimmers, not flickers
  let last = 0;
  function tick(now) {
    if (now - last > 70 && !document.hidden) {
      last = now;
      for (const [el, st] of live) {
        if (!el.isConnected) { live.delete(el); continue; }
        const cur = el.textContent.split("");
        const want = st.len;
        while (cur.length < want) cur.push(GLYPHS[0]);
        cur.length = want;
        for (let i = 0; i < Math.max(1, want / 4); i++) {
          const j = (Math.random() * want) | 0;
          if (st.shape[j] !== " ") cur[j] = GLYPHS[(Math.random() * GLYPHS.length) | 0];
        }
        put(el, cur.join(""));
      }
    }
    if (live.size && !reduce) requestAnimationFrame(tick); else ticking = false;
  }
  let ticking = false;
  const run = () => { scan(); if (!ticking && !reduce && live.size) { ticking = true; requestAnimationFrame(tick); } };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run); else run();
  window.HubScramble = { scan: (root) => { scan(root); run(); } };
})();
