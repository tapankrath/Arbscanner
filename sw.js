const CACHE = 'spread-v9';
const ASSETS = ['./index.html', './manifest.json', './icon-192.png', './icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  // Network-first for the live API calls (kalshi/polymarket), cache-first for our own shell files.
  const url = e.request.url;
  const isOwnAsset = ASSETS.some((a) => url.endsWith(a.replace('./', '')));
  if (isOwnAsset) {
    e.respondWith(caches.match(e.request).then((cached) => cached || fetch(e.request)));
  }
});
