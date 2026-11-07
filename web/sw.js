// lariat service worker. The build replaces @VERSION@ (a hash of the whole
// bundle) and @PRECACHE@ (every static file), so each build gets a new cache.
// /api, /auth, /ws and /enroll are never cached. It also shows Web Push alerts.

const VERSION = '@VERSION@';
const CACHE = `lariat-${VERSION}`;
const PRECACHE = @PRECACHE@;
const NETWORK_ONLY = ['/api/', '/auth/', '/ws/', '/m/', '/enroll', '/healthz'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('lariat-') && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('message', (event) => {
  if (event.data === 'skip-waiting') self.skipWaiting();
});

// A push is an alert from the gateway: show it (iOS requires every push to
// show something) and badge the app icon with the agents waiting.
self.addEventListener('push', (event) => {
  let msg = {};
  try {
    msg = event.data ? event.data.json() : {};
  } catch {
    msg = { body: event.data?.text() };
  }
  const shown = self.registration.showNotification(msg.title || 'lariat', {
    body: msg.body || '',
    tag: msg.tag,
    data: { url: msg.url || '/' },
    icon: '/icons/icon-192.png',
  });
  const nav = self.navigator;
  const badge =
    typeof msg.badge === 'number' && nav.setAppBadge
      ? (msg.badge ? nav.setAppBadge(msg.badge) : nav.clearAppBadge()).catch(() => {})
      : null;
  event.waitUntil(Promise.all([shown, badge]));
});

// Tapping an alert opens the app on that agent (focusing it if it is open).
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || '/', self.location.origin).href;
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      const open = windows.find((w) => new URL(w.url).origin === self.location.origin);
      if (open) {
        await open.focus();
        open.postMessage({ type: 'open', url });
      } else {
        await self.clients.openWindow(url);
      }
    })(),
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== self.location.origin) return;
  if (NETWORK_ONLY.some((p) => url.pathname.startsWith(p))) return;

  if (req.mode === 'navigate') {
    // Network first so a new build shows up; the cached shell when offline.
    event.respondWith(fetch(req).catch(() => caches.match('/', { cacheName: CACHE })));
    return;
  }
  event.respondWith(caches.match(req, { cacheName: CACHE }).then((hit) => hit || fetch(req)));
});
