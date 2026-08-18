// Service Worker — Kalkulator EV (PWA)
// Strategia: network-first dla nawigacji (index.html jest aktualizowany przez
// sondę 3×/dziennie — użytkownik online ma zawsze świeże losowania), z fallbackiem
// do cache offline. Statyczne zasoby (ikony, manifest): cache-first.
// Wersja cache powiązana z wersją aplikacji — bump wersji aplikacji = wymiana cache.
// v4.15.5: nazwa aplikacji w manifeście skrócona do „Kalkulator Lotto" — wymiana cache.
const CACHE = 'lotto-ev-v4.15.5';
const CORE = [
  './',
  'index.html',
  'manifest.webmanifest',
  'icons/icon-192.png',
  'icons/icon-512.png',
  'icons/icon-maskable-512.png',
  'icons/apple-touch-icon.png'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(CORE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Nawigacje i index.html: sieć najpierw, cache jako fallback offline.
  if (req.mode === 'navigate' || url.pathname.endsWith('/index.html')) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => caches.match(req).then((m) => m || caches.match('index.html')))
    );
    return;
  }

  // Pozostałe zasoby same-origin: cache-first, potem sieć (i docache'uj).
  e.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      });
    })
  );
});
