// Service worker: caches the app shell only (never API data). Scope = this folder.
// Every storage name is prefixed "lessonnotes" because all *.github.io project sites share one origin.
importScripts('precache.js'); // defines self.PRECACHE (git-tracked files only) and self.PRECACHE_VERSION
const PREFIX = 'lessonnotes-shell-';
const CACHE = PREFIX + self.PRECACHE_VERSION;
const base = new URL('./', self.location).href;

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    const c = await caches.open(CACHE);
    await c.addAll(self.PRECACHE.map((p) => new URL(p, base).href));
    await self.skipWaiting();
  })());
});
self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k.startsWith(PREFIX) && k !== CACHE) await caches.delete(k); // only our own prefix
    await self.clients.claim();
  })());
});
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin || !url.href.startsWith(base)) return; // API calls etc. go straight to network
  e.respondWith((async () => {
    const c = await caches.open(CACHE);
    const key = req.mode === 'navigate' ? new URL('index.html', base).href : req.url.split('#')[0].split('?')[0];
    const hit = await c.match(key); // our own cache only, never caches.match
    if (hit) return hit;
    try { return await fetch(req); } catch { return (await c.match(new URL('index.html', base).href)) || Response.error(); }
  })());
});
