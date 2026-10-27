// M1: network-only. Never cache authentication, API responses or trading state.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil((async () => {
  for (const key of await caches.keys()) {
    if (key.startsWith("nastack-trading-")) await caches.delete(key);
  }
  await self.clients.claim();
})()));
