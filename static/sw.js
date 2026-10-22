/* Service worker do Bolão da Champions.
 *
 * Objetivo: o app abre na hora, mesmo quando o servidor está "dormindo"
 * (plano free do Render) ou quando o celular está sem internet.
 *
 * Regras:
 *  - Navegação (abrir uma página): tenta a rede por até 3,5 s; se não vier,
 *    mostra a última versão em cache; se não houver, mostra a tela offline.
 *  - Arquivos de /static/: mostra o cache na hora e atualiza em segundo plano.
 *  - POST (salvar palpite etc.) nunca é interceptado.
 */

const VERSION = "bolao-v3";
const OFFLINE_URL = "/static/offline.html";
const NETWORK_TIMEOUT = 3500;

const SHELL = [
  "/static/style.css",
  OFFLINE_URL,
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-maskable-512.png",
  "/static/img/esquerda.png",
  "/static/img/direita.png",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(VERSION);
      // add() individual: se um arquivo falhar, o resto continua sendo cacheado.
      await Promise.all(SHELL.map((url) => cache.add(url).catch(() => {})));
      await self.skipWaiting();
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)));
      await self.clients.claim();
    })()
  );
});

self.addEventListener("message", (event) => {
  if (event.data === "skip-waiting") self.skipWaiting();
});

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timeout")), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      }
    );
  });
}

async function networkFirst(request) {
  const cache = await caches.open(VERSION);
  try {
    const response = await withTimeout(fetch(request), NETWORK_TIMEOUT);
    // 5xx = servidor acordando ou fora do ar: vale mais a última versão em cache.
    if (response && response.status < 500) {
      if (response.ok) cache.put(request, response.clone());
      return response;
    }
  } catch (error) {
    // sem conexão / tempo esgotado: cai no cache abaixo
  }
  const cached = await cache.match(request);
  if (cached) return cached;
  const offline = await cache.match(OFFLINE_URL);
  if (offline) return offline;
  return new Response("Sem conexão com o servidor.", {
    status: 503,
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(VERSION);
  const cached = await cache.match(request);
  const network = fetch(request)
    .then((response) => {
      if (response && response.status < 500) {
        if (response.ok) cache.put(request, response.clone());
        return response;
      }
      return null;
    })
    .catch(() => null);
  const fresh = cached || (await network);
  return fresh || Response.error();
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return; // fontes do Google etc.
  if (url.pathname === "/sw.js") return;

  if (request.mode === "navigate") {
    event.respondWith(networkFirst(request));
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(staleWhileRevalidate(request));
  }
});
