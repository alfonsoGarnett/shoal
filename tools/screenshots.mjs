// README screenshots and the demo GIF, taken from `python tools/demo.py` (made-up hubs only), in English.
// With the demo running:  CHROME=/path/to/chrome node tools/screenshots.mjs docs/screenshots
// then build the GIF from <outdir>/frames:  python tools/screenshots_gif.py docs/screenshots
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";

const out = process.argv[2] || "docs/screenshots";
const BASE = "http://127.0.0.1:8890/hub/";
const chrome = spawn(process.env.CHROME, ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-color-profile=srgb",
  "--remote-debugging-port=9341", `--user-data-dir=${out}/cdpmedia`, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws, id = 0;
const pending = new Map();
const send = (method, params = {}, sessionId) => {
  const m = { id: ++id, method, params, ...(sessionId ? { sessionId } : {}) };
  ws.send(JSON.stringify(m));
  return new Promise((r) => pending.set(m.id, r));
};
const ev = (s, expression) => send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }, s).then((r) => r.result && r.result.value);
const shot = async (s, file) => {
  const r = await send("Page.captureScreenshot", { format: "png" }, s);
  writeFileSync(file, Buffer.from(r.data, "base64"));
};
async function open(s, page, theme, w, h, mobile = false) {
  await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: mobile ? 2 : 1, mobile }, s);
  await send("Page.navigate", { url: BASE + "help.html" }, s);
  await sleep(700);
  await ev(s, `localStorage.setItem("hub.theme", ${JSON.stringify(theme)}); localStorage.setItem("hub.lang", "en"); localStorage.setItem("hub.mascots", "on")`);
  await send("Page.navigate", { url: BASE + page }, s);
  await sleep(7000);
}
try {
  let ver;
  for (let i = 0; i < 40; i++) { try { ver = await (await fetch("http://127.0.0.1:9341/json/version")).json(); break; } catch { await sleep(250); } }
  ws = new WebSocket(ver.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r));
  ws.addEventListener("message", (e) => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result || {}); pending.delete(m.id); } });
  const { targetId } = await send("Target.createTarget", { url: "about:blank" });
  const { sessionId: s } = await send("Target.attachToTarget", { targetId, flatten: true });
  await send("Runtime.enable", {}, s);
  await send("Page.enable", {}, s);

  // stills
  await open(s, "index.html", "acid", 1440, 900); await shot(s, `${out}/index.png`);
  await open(s, "control.html", "abyss", 1440, 900); await shot(s, `${out}/control.png`);
  await open(s, "control.html", "paper", 1440, 900);
  await ev(s, `document.querySelector("#net").scrollIntoView({ block: "center" })`); await sleep(900);
  await shot(s, `${out}/network-paper.png`);
  await open(s, "index.html", "abyss", 390, 844, true); await shot(s, `${out}/phone.png`);

  // GIF frames: the live mesh map, then the command palette
  mkdirSync(`${out}/frames`, { recursive: true });
  await open(s, "index.html", "acid", 1280, 800);
  let n = 0;
  const grab = async (count, gap) => { for (let i = 0; i < count; i++) { await shot(s, `${out}/frames/${String(n++).padStart(3, "0")}.png`); await sleep(gap); } };
  await grab(28, 90);
  await ev(s, `document.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true }))`);
  await sleep(500);
  for (const ch of "term") {
    await ev(s, `(() => { const i = document.querySelector(".kbar input"); i.value += ${JSON.stringify(ch)}; i.dispatchEvent(new Event("input")); })()`);
    await grab(2, 90);
  }
  await grab(8, 120);
  await ev(s, `document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))`);
  await send("Page.navigate", { url: BASE + "control.html" }, s);
  await sleep(4500);
  await grab(10, 120);
  console.log("frames", n);
} finally {
  chrome.kill();
  process.exit(0);
}
