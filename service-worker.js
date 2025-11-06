// service-worker.js
// ——— الإعدادات ———
const CACHE_NAME = 'lifecrown-v1';
const STATIC_ASSETS = [
  '/',                      // الصفحة الرئيسية (Shell)
  '/static/manifest.webmanifest',
  // أضف ملفات ثابتة مهمة إن رغبت
  // مثل: '/static/css/app.css', '/static/js/app.js', '/static/icons/icon-192.png', '/static/icons/icon-512.png'
];

// دوال مساعدة
const isMethodSafe = (req) => ['GET', 'HEAD'].includes(req.method);

// لا نكاشي WebSockets أو SSE أو أي شيء يشبه البث الحي
const shouldBypass = (req) => {
  const url = new URL(req.url);
  const accept = req.headers.get('accept') || '';
  const upgrade = req.headers.get('upgrade') || '';

  // Socket.IO / WebSocket
  if (url.pathname.startsWith('/socket.io') || upgrade.toLowerCase() === 'websocket') return true;

  // Server-Sent Events
  if (accept.includes('text/event-stream')) return true;

  // أي طلبات POST/PUT/DELETE وما شابه
  if (!isMethodSafe(req)) return true;

  return false;
};

// ——— التثبيت ———
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

// ——— التفعيل ———
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.map((k) => (k === CACHE_NAME ? null : caches.delete(k))))
    )
  );
  self.clients.claim();
});

// ——— الإستراتيجية ———
// 1) صفحات التنقل (navigate): Network First مع fallback للكاش عند انقطاع الشبكة
// 2) الملفات الثابتة (css/js/img): Stale-While-Revalidate (أرجع من الكاش بسرعة وحدث بالخلفية)
// 3) أي شيء آخر: مرره كما هو
self.addEventListener('fetch', (event) => {
  const req = event.request;

  // تجاوز ما لا نريد التعامل معه
  if (shouldBypass(req)) return;

  // تنقلات (HTML)
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const resClone = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, resClone));
          return res;
        })
        .catch(() => caches.match(req).then((r) => r || caches.match('/')))
    );
    return;
  }

  // ملفات ثابتة
  const url = new URL(req.url);
  const isStatic =
    url.pathname.startsWith('/static/') ||
    /\.(?:css|js|png|jpg|jpeg|gif|svg|webp|ico|woff2?)$/i.test(url.pathname);

  if (isStatic) {
    event.respondWith(
      caches.match(req).then((cached) => {
        const fetchPromise = fetch(req)
          .then((networkRes) => {
            // احفظ نسخة محدثة
            caches.open(CACHE_NAME).then((cache) => cache.put(req, networkRes.clone()));
            return networkRes;
          })
          .catch(() => cached); // عند الانقطاع أعد الكاش إن وجد
        return cached || fetchPromise;
      })
    );
    return;
  }

  // أي شيء آخر: Network fallback to cache
  event.respondWith(
    fetch(req)
      .then((res) => res)
      .catch(() => caches.match(req))
  );
});
