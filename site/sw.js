// Hub service worker: show push notifications and open the page when tapped.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch { d = { title: "Hub", body: event.data && event.data.text() }; }
  event.waitUntil(self.registration.showNotification(d.title || "Hub", {
    body: d.body || "",
    icon: "icon-192.png",
    badge: "badge-96.png",
    tag: d.tag || "hub",
    renotify: true,
    timestamp: d.ts ? d.ts * 1000 : Date.now(),
    data: { url: d.url || "/hub/" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  // Only pages of this hub: a notification relayed by another hub must not open an outside address.
  let url = new URL((event.notification.data && event.notification.data.url) || "/hub/", self.location.origin);
  if (url.origin !== self.location.origin) url = new URL("/hub/", self.location.origin);
  url = url.href;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) if (w.url === url && "focus" in w) return w.focus();
    return self.clients.openWindow(url);
  })());
});
