// Theme before the first paint (loaded without defer in <head>): acid is the original look and
// follows the system's light/dark; abyss is always dark blue; paper is always light. Theme and
// mascots are one preference for the whole mesh (api/caps "ui", saved with POST api/ui); this
// browser keeps a copy only so the page does not flash before api/caps answers.
(() => {
  "use strict";
  const THEMES = ["acid", "abyss", "paper"];
  const BAR = { abyss: "#070b14", paper: "#f5f5f7" };
  const root = document.documentElement;
  function apply(name) {
    root.dataset.theme = name;
    document.querySelectorAll('meta[name="theme-color"]').forEach((m) => {
      if (!m.dataset.orig) m.dataset.orig = m.content;
      m.content = BAR[name] || m.dataset.orig;
    });
  }
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };
  const saved = store.get("hub.theme") || "acid";
  apply(THEMES.includes(saved) ? saved : "acid");
  root.dataset.mascots = store.get("hub.mascots") === "off" ? "off" : "on";
  root.dataset.pixel = store.get("hub.pixel") === "off" ? "off" : "on";
  const flag = (key, value) => {  // true/false → "on"/"off" on <html data-…>, cached for the next load
    const v = value === false ? "off" : value === true ? "on" : root.dataset[key];
    if (v === root.dataset[key]) return false;
    store.set(`hub.${key}`, v);
    root.dataset[key] = v;
    return true;
  };
  const local = (name, mascots, pixel) => {
    let changed = false;
    if (THEMES.includes(name) && name !== root.dataset.theme) { store.set("hub.theme", name); apply(name); changed = true; }
    changed = flag("mascots", mascots) || changed;
    changed = flag("pixel", pixel) || changed;
    if (changed) window.dispatchEvent(new CustomEvent("hub-theme", { detail: root.dataset.theme }));
  };
  const save = (values) => fetch("api/ui", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values) }).catch(() => {});
  // Loading placeholders show CSS noise until site/scramble.js takes over (hub.css); if that
  // script never arrives, give the plain placeholder text back after a while.
  root.classList.add("scramble-on");
  setTimeout(() => { if (!window.HubScramble) root.classList.remove("scramble-on"); }, 15000);
  window.HubTheme = {
    list: THEMES,
    get: () => root.dataset.theme,
    mascots: () => root.dataset.mascots !== "off",
    pixel: () => root.dataset.pixel !== "off",
    set(name) { if (THEMES.includes(name)) { local(name); save({ theme: name }); } },
    setMascots(on) { local(undefined, !!on); save({ mascots: !!on }); },
    setPixel(on) { local(undefined, undefined, !!on); save({ pixel: !!on }); },
    sync(ui) { if (ui) local(ui.theme, ui.mascots, ui.pixel); },  // what the mesh says (from api/caps)
  };
})();
