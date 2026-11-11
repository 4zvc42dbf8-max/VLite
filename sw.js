// Offline support and push notifications for VLite. Bump CACHE to force a refresh of the app files.
const CACHE = 'vlite-v13';
const FILES = ['./', './index.html', './manifest.webmanifest', './icon-180.png', './icon-192.png', './icon-512.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  // Video list: always try the network first, fall back to the last copy when offline.
  if (url.pathname.endsWith('/videos.json')) {
    e.respondWith(
      fetch(req).then(res => {
        if (res && res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put('./videos.json', copy)); }
        return res;
      }).catch(() => caches.match('./videos.json'))
    );
    return;
  }
  // Page and channel list: network first (updates apply at once), cache when offline.
  if (req.mode === 'navigate' || url.pathname.endsWith('/index.html') || url.pathname.endsWith('/channels.json')) {
    e.respondWith(
      fetch(req).then(res => {
        if (res && res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req.mode === 'navigate' ? './index.html' : req, copy)); }
        return res;
      }).catch(() => caches.match(req, {ignoreSearch: true}).then(r => r || caches.match('./index.html')))
    );
    return;
  }
  // App files: instant from cache, refreshed quietly in the background.
  e.respondWith(
    caches.match(req, {ignoreSearch: true}).then(cached => {
      const net = fetch(req).then(res => {
        if (res && res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
        return res;
      }).catch(() => cached || (req.mode === 'navigate' ? caches.match('./index.html') : undefined));
      return cached || net;
    })
  );
});

// Web Push: a message from the collector server (notify.py) arrives here even when the app is closed.
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { title: 'VLite', body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.title || 'VLite', {
    body: d.body || '', icon: './icon-192.png', badge: './icon-192.png',
    tag: d.tag || 'vlite', renotify: true, data: { id: d.id || '' }
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const id = (e.notification.data && e.notification.data.id) || '';
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    const w = list.find(c => 'focus' in c);
    if (w) { w.postMessage(id ? { play: id } : { open: 1 }); return w.focus(); }
    return self.clients.openWindow('./' + (id ? '?v=' + encodeURIComponent(id) : ''));
  }));
});
