/* Minimal, dependency-free i18n for the hub UI. English is the source/default. */
(() => {
  "use strict";
  const SUPPORTED = ["en", "es"];
  const DEFAULT = "en";
  let lang = DEFAULT;
  let dict = {};
  let fallback = {};
  const listeners = new Set();

  const safeStorage = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  function interpolate(text, vars) {
    return String(text ?? "").replace(/\{(\w+)\}/g, (_, key) => (vars && key in vars ? String(vars[key]) : `{${key}}`));
  }

  function t(key, vars) {
    const value = dict[key] ?? fallback[key] ?? key;
    return interpolate(value, vars);
  }

  function tn(key, n, vars) {
    const suffix = Number(n) === 1 ? "one" : "other";
    return t(`${key}.${suffix}`, { ...(vars || {}), n });
  }

  function agoSeconds(seconds) {
    const s = Math.max(0, Number(seconds) || 0);
    if (s < 60) return t("time.now");
    if (s < 3600) return t("time.minutes", { n: Math.round(s / 60) });
    if (s < 86400) return t("time.hours", { n: Math.round(s / 3600) });
    return t("time.days", { n: Math.round(s / 86400) });
  }

  function ago(iso) {
    if (!iso) return "";
    const ms = new Date(iso).getTime();
    if (Number.isNaN(ms)) return "";
    return agoSeconds((Date.now() - ms) / 1000);
  }

  function apply(root = document) {
    root.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.dataset.i18n);
    });
    root.querySelectorAll("[data-i18n-html]").forEach((el) => {
      el.innerHTML = t(el.dataset.i18nHtml);
    });
    root.querySelectorAll("[data-i18n-aria]").forEach((el) => {
      el.setAttribute("aria-label", t(el.dataset.i18nAria));
    });
    root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      el.placeholder = t(el.dataset.i18nPlaceholder);
    });
    root.querySelectorAll("[data-i18n-title]").forEach((el) => {
      el.title = t(el.dataset.i18nTitle);
    });
    root.querySelectorAll("[data-i18n-content]").forEach((el) => {
      el.setAttribute("content", t(el.dataset.i18nContent));
    });
  }

  function mountLanguageToggle() {
    const meta = document.querySelector(".bar-meta");
    if (!meta || document.getElementById("lang-btn")) return;
    const btn = document.createElement("button");
    btn.className = "refresh lang-btn";
    btn.id = "lang-btn";
    btn.type = "button";
    const paint = () => {
      btn.textContent = lang === "es" ? "EN" : "ES";
      btn.setAttribute("aria-label", t("common.language"));
      btn.title = t("common.language");
    };
    paint();
    btn.addEventListener("click", () => {
      const next = lang === "es" ? "en" : "es";
      setLang(next).then(() => location.reload()).catch(() => location.reload());
    });
    meta.prepend(btn);
  }

  async function load(code) {
    const wanted = SUPPORTED.includes(code) ? code : DEFAULT;
    const [current, base] = await Promise.all([
      fetch(`i18n/${wanted}.json`, { cache: "no-store" }).then((r) => r.ok ? r.json() : {}),
      wanted === DEFAULT ? Promise.resolve(null) : fetch(`i18n/${DEFAULT}.json`, { cache: "no-store" }).then((r) => r.ok ? r.json() : {}),
    ]);
    dict = current || {};
    fallback = base || (wanted === DEFAULT ? current : {});
  }

  async function setLang(next, { persist = true, dispatch = true } = {}) {
    const wanted = SUPPORTED.includes(next) ? next : DEFAULT;
    if (persist) safeStorage.set("hub.lang", wanted);
    await load(wanted);
    lang = wanted;
    document.documentElement.lang = lang;
    apply();
    mountLanguageToggle();
    if (dispatch) {
      const event = new CustomEvent("hub:lang", { detail: lang });
      window.dispatchEvent(event);
      listeners.forEach((fn) => { try { fn(lang); } catch { /* ignore */ } });
    }
    return lang;
  }

  const ready = (async () => {
    let configured = null;
    try {
      const caps = await fetch("api/caps", { cache: "no-store" }).then((r) => r.json());
      window.__hubCaps = caps;
      configured = caps && caps.lang;
    } catch { /* standalone page */ }
    const query = new URLSearchParams(location.search).get("lang");
    const stored = safeStorage.get("hub.lang");
    lang = [query, stored, configured, DEFAULT].find((x) => SUPPORTED.includes(x)) || DEFAULT;
    if (query && SUPPORTED.includes(query)) safeStorage.set("hub.lang", query);
    await load(lang);
    document.documentElement.lang = lang;
    apply();
    mountLanguageToggle();
  })();

  window.I18N = {
    t,
    tn,
    apply,
    setLang,
    ready,
    ago,
    agoSeconds,
    onChange(fn) { listeners.add(fn); },
    get lang() { return lang; },
    get locale() { return lang === "es" ? "es-MX" : "en-US"; },
  };
})();
