// Help page: commands for this hub, with its real port, folders, address and service filled in.
(async () => {
  "use strict";
  await I18N.ready;
  const T = (k, v) => I18N.t(k, v);
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = (m) => {
    const t = $("#toast");
    t.textContent = m;
    t.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove("show"), 1600);
  };
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); }
    catch {
      const ta = Object.assign(document.createElement("textarea"), { value: text });
      document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
    }
    toast(T("help.copied"));
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-copy]");
    if (b) { e.preventDefault(); copy(b.dataset.copy); }
  });

  let h;
  try { h = await (await fetch("api/hub/help", { cache: "no-store" })).json(); }
  catch { $("#facts").innerHTML = `<p class="ct-sub">${esc(T("help.unavailable"))}</p>`; return; }
  const win = h.family === "windows", termux = h.family === "android";
  const q = (path) => (win ? `"${path}"` : /[\s"'$]/.test(path) ? `"${path}"` : path);
  const sep = win ? "; " : " && ";
  const inApp = (cmd) => `cd ${q(h.app)}${sep}${cmd}`;
  const join = (a, b) => (win ? `${a}\\${b}` : `${a}/${b}`);
  const local = `http://127.0.0.1:${h.port}/hub`;
  const url = (h.url || "").replace(/\/$/, "");

  const rows = (box, list) => {
    $(box).innerHTML = list.filter(Boolean).map(([label, cmd]) =>
      `<div class="help-cmd"><span>${esc(label)}</span><button type="button" class="cmd" data-copy="${esc(cmd)}">${esc(cmd)}</button></div>`).join("");
  };

  // This hub
  const svc = h.service || {};
  const starts = svc.kind === "systemd" ? T("help.svcSystemd", { name: svc.name })
    : svc.kind === "task" ? T("help.svcTask", { name: svc.name })
    : svc.kind === "launchd" ? T("help.svcLaunchd", { name: svc.name })
    : svc.kind === "termux" ? T("help.svcTermux") : T("help.svcNone");
  const facts = [
    [T("help.factName"), h.name], [T("help.factAddress"), url || `http://${h.ip || h.name}:${h.port}/hub/`],
    [T("help.factPort"), String(h.port)], [T("help.factCode"), h.app], [T("help.factData"), h.data],
    [T("help.factStarts"), starts], [T("help.factRole"), h.author ? T("help.roleAuthor") : T("help.roleFollower")],
  ];
  $("#facts").innerHTML = facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");

  // On this device
  const restart = svc.kind === "systemd" ? `sudo systemctl restart ${svc.name}`
    : svc.kind === "task" ? `Stop-ScheduledTask -TaskName ${svc.name}; Start-ScheduledTask -TaskName ${svc.name}`
    : svc.kind === "launchd" ? `launchctl kickstart -k gui/$(id -u)/${svc.name}`
    : svc.kind === "termux" ? `pkill -f "core --home"` : "";
  rows("#daily", [
    [T("help.cmdAnswers"), win ? `irm ${local}/api/hub/version` : `curl -s ${local}/api/hub/version`],
    [T("help.cmdLog"), win ? `Get-Content -Wait -Tail 40 ${q(join(h.data, "mesh.log"))}` : `tail -f ${q(join(h.data, "mesh.log"))}`],
    svc.kind === "systemd" && [T("help.cmdJournal"), `journalctl -u ${svc.name} -f`],
    restart && [T("help.cmdRestart"), restart],
    [T("help.cmdRelease"), inApp(`${h.python} -m core --release`)],
  ]);

  // Updates
  $("#update-text").textContent = h.author ? T("help.updatesAuthor") : T("help.updatesFollower");
  rows("#updates", h.author ? [
    [T("help.cmdPull"), inApp(`git pull${sep}${h.python} -m core --apply-update`)],
    [T("help.cmdRollback"), inApp(`${h.python} -m core --rollback`)],
    [T("help.cmdPause"), inApp(`${h.python} -m core --pause-updates`)],
  ] : [
    [T("help.cmdRollback"), inApp(`${h.python} -m core --rollback`)],
    [T("help.cmdPause"), inApp(`${h.python} -m core --pause-updates`)],
    [T("help.cmdResume"), inApp(`${h.python} -m core --resume-updates`)],
  ]);

  // Tailscale
  $("#ts-text").textContent = termux ? T("help.tsTermux", { port: h.port }) : T("help.tsText");
  rows("#tailscale", termux ? [["Tailscale", "tailscale status"]] : [
    [T("help.cmdServeStatus"), "tailscale serve status"],
    [T("help.cmdPublish"), `tailscale serve --bg --https=443 --set-path=/hub http://127.0.0.1:${h.port}`],
    [T("help.cmdUnpublish"), "tailscale serve --https=443 --set-path=/hub off"],
    [T("help.cmdFunnel"), "tailscale funnel status"],
  ]);

  // Add a device: always this hub's tailnet address, never localhost
  const loop = /^https?:\/\/(127\.|localhost|\[::1\])/i.test(url);  // a test hub: never hand out loopback
  const base = url && !loop ? url : `http://${h.ip || h.name}:${h.port}/hub`;
  const u = new URL(base + "/");
  const resolve = h.ip && /[a-z]/i.test(u.hostname) ? `--resolve ${u.hostname}:${u.port || (u.protocol === "https:" ? 443 : 80)}:${h.ip} ` : "";
  rows("#add", [
    ["Windows", `irm ${base}/install.ps1 | iex`],
    ["Linux, macOS, Raspberry Pi", `curl -fsSL ${base}/install.sh | sh`],
    ["Android (Termux)", `curl -fsSL ${resolve}${base}/install.sh | sh`],
  ]);

  // If something is wrong
  $("#fix").innerHTML = [
    T("help.fixUnreachable", { port: h.port }),
    T("help.fixOffline"),
    T("help.fixUpdate"),
    T("help.fixNotify"),
  ].map((t) => `<li>${esc(t)}</li>`).join("");
})();
