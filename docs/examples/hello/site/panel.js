// Hello panel: reads and bumps the counter through the module's own API namespace.
(async () => {
  "use strict";
  await I18N.ready;
  const T = (k, v) => I18N.t(k, v);
  const $ = (s) => document.querySelector(s);
  const show = (st) => {
    $("#greeting").textContent = st.greeting;
    $("#count").textContent = T("hello.count", { n: st.count, hub: st.hub });
  };
  const state = async () => (await fetch("api/modules/hello/state", { cache: "no-store" })).json();
  $("#bump").addEventListener("click", async () => {
    await fetch("api/modules/hello/bump", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    show(await state());
  });
  show(await state());
})();
