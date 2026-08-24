const COOKIE = "statuspulse_access";
const SESSION_SECONDS = 24 * 60 * 60;
const SIGNATURE_TOLERANCE = 5 * 60;

function response(body, status = 200, headers = {}) {
  return new Response(body, { status, headers: { "Cache-Control": "no-store", ...headers } });
}

function hex(bytes) {
  return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256(value) {
  return hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));
}

function parseSignature(header) {
  const result = {};
  for (const item of (header || "").split(",")) {
    const [key, value] = item.split("=", 2);
    if (key && value) (result[key] ||= []).push(value);
  }
  return result;
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let result = 0;
  for (let i = 0; i < a.length; i++) result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return result === 0;
}

async function verifyStripe(payload, header, secret) {
  const parts = parseSignature(header);
  const timestamp = Number(parts.t?.[0]);
  if (!Number.isInteger(timestamp) || !parts.v1?.length || Math.abs(Date.now() / 1000 - timestamp) > SIGNATURE_TOLERANCE) throw new Error("invalid signature");
  const data = `${timestamp}.${payload}`;
  const digest = await crypto.subtle.sign("HMAC", await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]), new TextEncoder().encode(data));
  const expected = hex(digest);
  if (!parts.v1.some((candidate) => timingSafeEqual(expected, candidate))) throw new Error("invalid signature");
  return JSON.parse(payload);
}

async function webhook(request, env) {
  const payload = await request.text();
  let event;
  try { event = await verifyStripe(payload, request.headers.get("Stripe-Signature"), env.STRIPE_WEBHOOK_SECRET); }
  catch (_) { return response("invalid webhook\n", 400); }
  if (event.livemode !== true || env.STATUSPULSE_ALLOW_LIVE !== "true") return response("live mode disabled\n", 400);
  if (typeof event.id !== "string" || !event.id) return response("invalid event\n", 400);
  const existing = await env.DB.prepare("SELECT event_id FROM stripe_events WHERE event_id = ?").bind(event.id).first();
  if (existing) return response("duplicate\n");
  const object = event.data?.object || {};
  const now = Math.floor(Date.now() / 1000);
  const statements = [env.DB.prepare("INSERT INTO stripe_events(event_id, processed_at) VALUES (?, ?)").bind(event.id, now)];
  if (event.type === "checkout.session.completed") {
    if (object.mode !== "subscription" || object.payment_status !== "paid" || typeof object.id !== "string" || typeof object.subscription !== "string") return response("incomplete checkout\n", 400);
    statements.unshift(env.DB.prepare("INSERT INTO entitlements(checkout_session_id, subscription_id, active, created_at) VALUES (?, ?, 1, ?) ON CONFLICT(checkout_session_id) DO UPDATE SET subscription_id=excluded.subscription_id, active=1").bind(object.id, object.subscription, now));
  } else if (event.type === "customer.subscription.deleted" && typeof object.id === "string") {
    statements.unshift(env.DB.prepare("UPDATE entitlements SET active=0 WHERE subscription_id=?").bind(object.id));
  }
  await env.DB.batch(statements);
  return response("ok\n");
}

function sessionCookie(token) { return `${COOKIE}=${token}; Path=/; Max-Age=${SESSION_SECONDS}; HttpOnly; Secure; SameSite=Lax`; }

async function checkoutSuccess(request, env) {
  const sessionId = new URL(request.url).searchParams.get("session_id") || "";
  const entitlement = await env.DB.prepare("SELECT 1 FROM entitlements WHERE checkout_session_id=? AND active=1").bind(sessionId).first();
  if (!entitlement) return response("Payment confirmation is still processing. Refresh shortly.\n", 403, { "Content-Type": "text/plain" });
  const token = crypto.randomUUID() + crypto.randomUUID();
  const expires = Math.floor(Date.now() / 1000) + SESSION_SECONDS;
  await env.DB.prepare("INSERT INTO access_sessions(token_hash, checkout_session_id, expires_at) VALUES (?, ?, ?)").bind(await sha256(token), sessionId, expires).run();
  return response(`<!doctype html><meta charset=utf-8><title>StatusPulse downloads</title><h1>Your StatusPulse downloads</h1><p><a href="/download/statuspulse.csv">Full CSV dataset</a></p><p><a href="/download/statusfeed.db">Full SQLite dataset</a></p>`, 200, { "Content-Type": "text/html; charset=utf-8", "Set-Cookie": sessionCookie(token), "Referrer-Policy": "no-referrer" });
}

async function authorized(request, env) {
  const match = request.headers.get("Cookie")?.match(new RegExp(`${COOKIE}=([^;]+)`));
  if (!match) return false;
  return !!await env.DB.prepare("SELECT 1 FROM access_sessions s JOIN entitlements e ON e.checkout_session_id=s.checkout_session_id WHERE s.token_hash=? AND s.expires_at>? AND e.active=1").bind(await sha256(match[1]), Math.floor(Date.now() / 1000)).first();
}

async function download(request, env, path) {
  if (!await authorized(request, env)) return response("authentication required\n", 401, { "Content-Type": "text/plain" });
  const key = path.endsWith(".db") ? "statusfeed.db" : "statuspulse.csv";
  const object = await env.DATA.get(key);
  if (!object) return response("release unavailable\n", 503);
  return new Response(object.body, { headers: { "Content-Type": key.endsWith(".db") ? "application/vnd.sqlite3" : "text/csv; charset=utf-8", "Content-Disposition": `attachment; filename="${key === "statusfeed.db" ? "statuspulse.db" : key}"`, "Cache-Control": "private, no-store" } });
}

export default { async fetch(request, env) {
  const url = new URL(request.url);
  try {
    if (request.method === "POST" && url.pathname === "/stripe/webhook") return await webhook(request, env);
    if (request.method === "GET" && url.pathname === "/checkout/success") return await checkoutSuccess(request, env);
    if (request.method === "GET" && (url.pathname === "/download/statuspulse.csv" || url.pathname === "/download/statusfeed.db")) return await download(request, env, url.pathname);
    return response("not found\n", 404);
  } catch (_) { return response("internal error\n", 500); }
} };
