// Service worker: keeps the app itself (pages + scripts) available offline, so a book
// downloaded from Google Drive (kept by cloud.js in Cache Storage) can be opened with
// no connection. Network first, so a new deploy shows up as soon as you're online.
const SHELL = "readalong-shell-v1";
const FILES = ["./", "index.html", "glasses.html", "cloud.js", "cloud-config.js", "library.json"];

self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).catch(() => {}));
});
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  // Leave alone: other sites (Google), audio range requests, and books (they're large;
  // Drive books are cached separately by cloud.js).
  if (req.method !== "GET" || url.origin !== location.origin || req.headers.has("range") ||
      url.pathname.includes("/books/") || url.pathname.includes("/offline/")) return;
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(SHELL).then((c) => c.put(req, copy)); }
        return res;
      })
      .catch(() => caches.match(req, { ignoreSearch: true }).then((r) => r || Response.error()))
  );
});
