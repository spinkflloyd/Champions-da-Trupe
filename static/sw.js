/* Service worker do Bolão da Champions — v4
 *
 * Regras:
 *  - Navegação com cache disponível: espera 6 s pela rede; se não vier, mostra
 *    a última versão em cache (abre na hora).
 *  - Navegação sem cache: espera até 25 s (tempo de um servidor free acordar).
 *  - Se o servidor responder 5xx, mostra a tela de diagnóstico em vez de
 *    fingir que está "conectando" (isso causava um loop infinito).
 *  - /static/: cache na hora + atualização em segundo plano.
 *  - POST nunca é interceptado.
 *  - ?nosw=1 desliga o service worker (modo diagnóstico).
 */

const VERSION = "bolao-v4";
const OFFLINE_URL = "/static/offline.html";
const TIMEOUT_CACHED = 6000;
// 12 s: se o servidor estiver hibernando, o usuário vê a tela de progresso
// rápido (em vez de uma tela branca) e o app abre sozinho quando ele acordar.
const TIMEOUT_UNCACHED = 12000;

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

function fallback(status, text) {
  return new Response(text, {
    status,
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
}

async function navigate(request) {
  const cache = await caches.open(VERSION);
  const cached = await cache.match(request);
  const timeout = cached ? TIMEOUT_CACHED : TIMEOUT_UNCACHED;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(request, { signal: controller.signal });
    clearTimeout(timer);

    if (response.status >= 500) {
      // O servidor respondeu, mas com erro: não adianta recarregar em loop.
      // A página offline detecta o status pelo próprio health check/preflight.
      const offline = await cache.match(OFFLINE_URL);
      if (offline) return offline;
      return fallback(503, "O servidor respondeu com erro " + response.status + ".");
    }

    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch (error) {
    clearTimeout(timer);
    if (cached) return cached;
    const offline = await cache.match(OFFLINE_URL);
    return offline || fallback(503, "Sem conexão com o servidor.");
  }
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
  if (url.searchParams.get("nosw") === "1") return; // modo diagnóstico

  if (request.mode === "navigate") {
    event.respondWith(navigate(request));
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(staleWhileRevalidate(request));
  }
});
