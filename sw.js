// 定期報告（teiki.html）の「期日」の通知を受け取るためだけのサービスワーカー。
// ページの読み込みには関わらない（fetch は横取りしない）。
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || 'お金の期日', {
    body: d.body || '', tag: d.tag, icon: 'line-assets/teiki-icon-192.png', badge: 'line-assets/teiki-icon-192.png',
    data: { url: d.url || 'teiki.html#kg' }
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || 'teiki.html#kg', self.registration.scope).href;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    const base = url.split('#')[0];
    for (const c of list) if (c.url.split('#')[0] === base && 'focus' in c) { c.postMessage({ go: 'kg' }); return c.focus(); }
    return self.clients.openWindow(url);
  }));
});
