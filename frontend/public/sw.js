/* Service worker.
 *
 * Two jobs:
 *  1. Cache the app shell so the PWA opens instantly and shows a usable screen
 *     with no connectivity. API responses are deliberately NOT cached -- stale
 *     market data presented as current would be worse than an error.
 *  2. Receive Web Push (Phase 2). The handlers are here now so that enabling
 *     VAPID keys is a server-side change only.
 *
 * iOS note: push is delivered only when the app has been added to the Home
 * Screen. In a Safari tab, none of the push code below ever runs.
 */

const SHELL_CACHE = 'shell-v1';
const SHELL_ASSETS = ['/', '/index.html', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS)).then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;

  // Navigations: network first, shell as the offline fallback.
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request).catch(() => caches.match('/index.html')));
    return;
  }
  event.respondWith(caches.match(event.request).then((hit) => hit || fetch(event.request)));
});

self.addEventListener('push', (event) => {
  let payload = { title: 'New event detected', body: 'Open the app to review the analysis.' };
  try {
    if (event.data) payload = { ...payload, ...event.data.json() };
  } catch {
    /* A malformed push payload still shows the neutral default above. */
  }

  const target = payload.ticker
    ? `/ticker/${payload.ticker}`
    : payload.event_id
      ? `/event/${payload.event_id}`
      : '/notifications';

  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      tag: payload.notification_id || 'event',
      data: { url: target },
    }),
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/notifications';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ('focus' in client) {
          client.navigate(target);
          return client.focus();
        }
      }
      return self.clients.openWindow(target);
    }),
  );
});
