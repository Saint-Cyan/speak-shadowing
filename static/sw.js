// 只缓存页面外壳，保证弱网下能打开；语音接口与云服务请求一律走网络。
const CACHE = 'speak-shell-v14';
const SHELL = ['/', '/manifest.webmanifest', '/static/icon-192.png', '/static/icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/.cloud/')) return;
  // 页面：网络优先，失败回退缓存
  if (e.request.mode === 'navigate' && url.pathname === '/') {
    e.respondWith(
      fetch(e.request)
        .then((r) => {
          if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put('/', copy)); }
          return r;
        })
        .catch(() => caches.match('/'))
    );
    return;
  }
  if (SHELL.includes(url.pathname)) {
    e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
  }
});
