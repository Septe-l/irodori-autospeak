// Service worker for Irodori AutoSpeak.
//
// This app is useless without its servers — everything it shows comes from the
// LAN over SSE — so there is nothing worth serving offline beyond the shell.
// The worker exists to make the app installable and to survive a brief network
// blip on launch, not to cache content.
//
// Served from the origin root so its scope covers the whole app; a worker
// under /static/ could only control /static/.

const CACHE = "autospeak-shell-v1";
const SHELL = [
  "/",
  "/static/style.css",
  "/static/app.js",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      // One missing entry must not abort the whole install.
      .then((c) => Promise.allSettled(SHELL.map((u) => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Anything live must never be intercepted: /events is an endless SSE stream,
// /audio serves clips that are deleted once played, and /api is all state.
function isLive(url) {
  return url.pathname === "/events"
    || url.pathname.startsWith("/api/")
    || url.pathname.startsWith("/audio/");
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin || isLive(url)) return;

  // Network first: an edited page has to win over a cached one. The cache is
  // only a fallback for when the server is unreachable.
  event.respondWith(
    fetch(req)
      .then((resp) => {
        if (resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return resp;
      })
      .catch(() => caches.match(req).then((hit) => hit || caches.match("/")))
  );
});
