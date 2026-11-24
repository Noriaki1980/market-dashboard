/**
 * Market Dashboard 最新値の中継（Cloudflare Workers）
 *
 *   GET /quotes?symbols=^N225,JPY=X,7203.T
 *   → { fetched_at, quotes: { "^N225": { price, prev_close, time, date, live, currency }, ... }, errors: {...} }
 *
 * - Yahoo Finance のチャートAPIから最新値（regularMarketPrice）と、その時刻を取得
 * - 受け付けるのは ALLOWED_ORIGINS のサイトからの呼び出しだけ
 * - 銘柄は DATA_URL（ダッシュボードの market.json）に載っているものだけ
 * - 同じ銘柄は 60 秒キャッシュ（Yahoo への問い合わせを減らす）
 */

// ======== 設定（必要ならここだけ書き換え） ========
const ALLOWED_ORIGINS = [
  "https://noriaki1980.github.io",
  "http://localhost:8000",            // 手元で試すとき（python -m http.server 8000）
];
const DATA_URL = "https://noriaki1980.github.io/market-dashboard/data/market.json";
const CACHE_SECONDS = 60;
const MAX_SYMBOLS = 80;
// ================================================

const YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/";
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get("Origin") || "";
    const allowed = (env && env.ALLOWED_ORIGINS ? String(env.ALLOWED_ORIGINS).split(",").map(s => s.trim()) : ALLOWED_ORIGINS);
    const cors = allowed.includes(origin)
      ? { "Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "GET, OPTIONS", "Vary": "Origin" }
      : null;

    if (request.method === "OPTIONS") return new Response(null, { status: cors ? 204 : 403, headers: cors || {} });
    const url = new URL(request.url);
    if (url.pathname === "/" ) return json({ ok: true, usage: "/quotes?symbols=^N225,JPY=X" }, 200, cors || {});
    if (!cors) return json({ error: "origin not allowed" }, 403, {});
    if (url.pathname !== "/quotes") return json({ error: "not found" }, 404, cors);

    const req = [...new Set((url.searchParams.get("symbols") || "").split(",").map(s => s.trim()).filter(Boolean))];
    if (!req.length) return json({ error: "symbols required" }, 400, cors);
    if (req.length > MAX_SYMBOLS) return json({ error: `too many symbols (max ${MAX_SYMBOLS})` }, 400, cors);

    const allow = await allowedSymbols(env, ctx);
    const quotes = {}, errors = {};
    const targets = req.filter(s => {
      const ok = /^[A-Za-z0-9^=.\-]{1,20}$/.test(s) && (!allow || allow.has(s));
      if (!ok) errors[s] = "not allowed";
      return ok;
    });

    // 6 並列で取得
    let i = 0;
    await Promise.all(Array.from({ length: Math.min(6, targets.length) }, async () => {
      while (i < targets.length) {
        const sym = targets[i++];
        try { quotes[sym] = await quote(sym, ctx); }
        catch (e) { errors[sym] = String(e && e.message || e).slice(0, 120); }
      }
    }));

    return json({ fetched_at: new Date().toISOString(), quotes, errors }, 200, { ...cors, "Cache-Control": "no-store" });
  },
};

async function allowedSymbols(env, ctx) {
  const src = (env && env.DATA_URL) || DATA_URL;
  try {
    const r = await fetch(src, { cf: { cacheTtl: 600, cacheEverything: true } });
    if (!r.ok) return null;
    const d = await r.json();
    return new Set(Object.keys(d.series || {}));
  } catch { return null; }   // 取れないときは記号の形式チェックだけ
}

async function quote(sym, ctx) {
  const cache = caches.default;
  const key = new Request("https://cache.local/q/" + encodeURIComponent(sym));
  const hit = await cache.match(key);
  if (hit) return hit.json();

  const r = await fetch(YAHOO + encodeURIComponent(sym) + "?range=1d&interval=5m&includePrePost=false",
    { headers: { "User-Agent": UA, "Accept": "application/json" } });
  if (!r.ok) throw new Error("yahoo " + r.status);
  const body = await r.json();
  const res = body && body.chart && body.chart.result && body.chart.result[0];
  const m = res && res.meta;
  if (!m || m.regularMarketPrice == null || !m.regularMarketTime) {
    const err = body && body.chart && body.chart.error;
    throw new Error(err ? (err.code || "error") : "no data");
  }
  const tz = m.exchangeTimezoneName || "UTC";
  const t = new Date(m.regularMarketTime * 1000);
  const now = Date.now() / 1000;
  const reg = m.currentTradingPeriod && m.currentTradingPeriod.regular;
  const live = !!(reg && now >= reg.start && now < reg.end && now - m.regularMarketTime < 40 * 60);
  const q = {
    price: m.regularMarketPrice,
    prev_close: m.chartPreviousClose ?? m.previousClose ?? null,
    time: toJST(t),                    // 価格の時刻（日本時間, 例 2026-11-24T21:25+09:00）
    date: localDate(t, tz),            // 取引所の日付（日足の日付と同じ基準）
    live,                              // 取引時間中か
    currency: m.currency || null,
  };
  ctx && ctx.waitUntil && ctx.waitUntil(cache.put(key, new Response(JSON.stringify(q), { headers: { "Cache-Control": `max-age=${CACHE_SECONDS}` } })));
  return q;
}

function localDate(d, tz) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
}
function toJST(d) {
  const j = new Date(d.getTime() + 9 * 3600 * 1000).toISOString();
  return j.slice(0, 16) + "+09:00";
}
function json(obj, status, headers) {
  return new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json; charset=utf-8", ...headers } });
}
