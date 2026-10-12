interface Env {
  DB: D1Database;
  PRODUCT_ID: string;
  BANK_BIN: string;
  BANK_ACCOUNT: string;
  BANK_ACCOUNT_NAME: string;
  PRICE_LIFETIME: string;
  LATEST_VERSION: string;
  DOWNLOAD_URL: string;
  ADMIN_PUBLIC_KEY_B64: string;
  CASSO_WEBHOOK_SECRET: string;
  LICENSE_PRIVATE_JWK: string;
}

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "content-type, secure-token, x-admin-timestamp, x-admin-signature",
  "access-control-allow-methods": "GET, POST, OPTIONS",
};

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: JSON_HEADERS });
}

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

function base64Url(value: ArrayBuffer | Uint8Array): string {
  const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function decodeBase64(value: string): Uint8Array {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function verifyAdminRequest(request: Request, env: Env, body: string): Promise<boolean> {
  const timestampText = request.headers.get("x-admin-timestamp") ?? "";
  const signatureText = request.headers.get("x-admin-signature") ?? "";
  const timestamp = Number(timestampText);
  if (!Number.isInteger(timestamp) || Math.abs(nowSeconds() - timestamp) > 300) return false;
  if (!env.ADMIN_PUBLIC_KEY_B64 || !signatureText) return false;
  try {
    const publicKey = await crypto.subtle.importKey(
      "raw",
      decodeBase64(env.ADMIN_PUBLIC_KEY_B64),
      { name: "Ed25519" },
      false,
      ["verify"],
    );
    const message = new TextEncoder().encode(`${timestampText}\n${body}`);
    return await crypto.subtle.verify(
      "Ed25519",
      publicKey,
      decodeBase64(signatureText),
      message,
    );
  } catch {
    return false;
  }
}

async function latestRelease(env: Env): Promise<Response> {
  const saved = await env.DB.prepare(
    "SELECT version, download_url, updated_at FROM app_settings WHERE id = 1",
  ).first<Record<string, unknown>>();
  return json({
    product: env.PRODUCT_ID,
    version: saved?.version ?? env.LATEST_VERSION,
    download_url: saved?.download_url ?? env.DOWNLOAD_URL,
    updated_at: saved?.updated_at ?? null,
  });
}

async function updateRelease(request: Request, env: Env): Promise<Response> {
  const rawBody = await request.text();
  if (!await verifyAdminRequest(request, env, rawBody)) {
    return json({ error: "unauthorized" }, 401);
  }
  let body: Record<string, unknown>;
  try {
    body = JSON.parse(rawBody) as Record<string, unknown>;
  } catch {
    return json({ error: "invalid_json" }, 400);
  }
  const version = String(body.version ?? "").trim();
  const downloadUrl = String(body.download_url ?? "").trim();
  if (!/^\d+(?:\.\d+){0,3}$/.test(version)) {
    return json({ error: "invalid_version" }, 400);
  }
  if (!downloadUrl.startsWith("https://")) {
    return json({ error: "invalid_download_url" }, 400);
  }
  const updatedAt = nowSeconds();
  await env.DB.prepare(
    `INSERT INTO app_settings (id, version, download_url, updated_at)
     VALUES (1, ?, ?, ?)
     ON CONFLICT(id) DO UPDATE SET
       version = excluded.version,
       download_url = excluded.download_url,
       updated_at = excluded.updated_at`,
  ).bind(version, downloadUrl, updatedAt).run();
  return json({ ok: true, version, download_url: downloadUrl, updated_at: updatedAt });
}

function randomId(bytes = 16): string {
  const value = new Uint8Array(bytes);
  crypto.getRandomValues(value);
  return base64Url(value);
}

async function sha256(value: string): Promise<string> {
  return base64Url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));
}

function constantTimeEqual(left: string, right: string): boolean {
  const a = new TextEncoder().encode(left);
  const b = new TextEncoder().encode(right);
  let difference = a.length ^ b.length;
  const length = Math.max(a.length, b.length);
  for (let index = 0; index < length; index++) {
    difference |= (a[index % Math.max(1, a.length)] ?? 0) ^
      (b[index % Math.max(1, b.length)] ?? 0);
  }
  return difference === 0;
}

function validUserId(value: unknown): value is string {
  return typeof value === "string" && /^ACC-PC-(?:[A-F0-9]{4}-){5}[A-F0-9]{4}$/.test(value);
}

function normalizeDescription(value: unknown): string {
  return String(value ?? "").toUpperCase().replace(/[^A-Z0-9]/g, "");
}

function qrUrl(env: Env, amount: number, paymentCode: string): string {
  const query = new URLSearchParams({
    amount: String(amount),
    addInfo: paymentCode,
    accountName: env.BANK_ACCOUNT_NAME,
  });
  return `https://img.vietqr.io/image/${encodeURIComponent(env.BANK_BIN)}-${encodeURIComponent(env.BANK_ACCOUNT)}-compact2.png?${query}`;
}

async function issueLicense(env: Env, userId: string): Promise<{ token: string; payload: Record<string, unknown> }> {
  const issuedAt = nowSeconds();
  const payload = {
    customer: "Thanh toán tự động",
    hwid: userId,
    issued_at: issuedAt,
    key_version: 3,
    license_id: `ACC-AUTO-${randomId(9).replace(/[-_]/g, "").slice(0, 12).toUpperCase()}`,
    license_type: "lifetime",
    product: env.PRODUCT_ID,
  };
  const payloadBytes = new TextEncoder().encode(JSON.stringify(payload));
  const privateKey = await crypto.subtle.importKey(
    "jwk",
    JSON.parse(env.LICENSE_PRIVATE_JWK),
    { name: "Ed25519" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("Ed25519", privateKey, payloadBytes);
  return { token: `ACC1.${base64Url(payloadBytes)}.${base64Url(signature)}`, payload };
}

async function createOrder(request: Request, env: Env): Promise<Response> {
  const body: Record<string, unknown> = await request.json<Record<string, unknown>>()
    .catch(() => ({} as Record<string, unknown>));
  const userId = String(body.user_id ?? "").trim().toUpperCase();
  if (!validUserId(userId)) return json({ error: "invalid_user_id" }, 400);
  if (body.plan !== "lifetime") return json({ error: "invalid_plan" }, 400);

  const createdAt = nowSeconds();
  const orderId = randomId(18);
  const pollToken = randomId(24);
  const paymentCode = `ACC${randomId(7).replace(/[-_]/g, "").slice(0, 9).toUpperCase()}`;
  const amount = Number(env.PRICE_LIFETIME);
  const expiresAt = createdAt + 30 * 60;
  await env.DB.prepare(
    `INSERT INTO orders
      (id, poll_token_hash, user_id, plan, amount, payment_code, status, created_at, expires_at)
     VALUES (?, ?, ?, 'lifetime', ?, ?, 'pending', ?, ?)`,
  ).bind(orderId, await sha256(pollToken), userId, amount, paymentCode, createdAt, expiresAt).run();

  return json({
    order_id: orderId,
    poll_token: pollToken,
    status: "pending",
    plan: "lifetime",
    amount,
    payment_code: paymentCode,
    expires_at: expiresAt,
    bank: {
      bin: env.BANK_BIN,
      account: env.BANK_ACCOUNT,
      account_name: env.BANK_ACCOUNT_NAME,
    },
    qr_url: qrUrl(env, amount, paymentCode),
  }, 201);
}

async function orderStatus(request: Request, env: Env, orderId: string): Promise<Response> {
  const pollToken = new URL(request.url).searchParams.get("token") ?? "";
  const order = await env.DB.prepare("SELECT * FROM orders WHERE id = ?")
    .bind(orderId).first<Record<string, unknown>>();
  if (!order || !constantTimeEqual(String(order.poll_token_hash), await sha256(pollToken))) {
    return json({ error: "order_not_found" }, 404);
  }
  if (order.status === "pending" && Number(order.expires_at) < nowSeconds()) {
    await env.DB.prepare("UPDATE orders SET status = 'expired' WHERE id = ? AND status = 'pending'")
      .bind(orderId).run();
    order.status = "expired";
  }
  return json({
    order_id: order.id,
    status: order.status,
    plan: order.plan,
    amount: order.amount,
    payment_code: order.payment_code,
    expires_at: order.expires_at,
    paid_at: order.paid_at,
    license: order.status === "paid" ? order.license_token : null,
  });
}

async function handleTransaction(env: Env, transaction: Record<string, unknown>): Promise<void> {
  const transactionId = String(transaction.id ?? transaction.reference ?? "").trim();
  const description = String(transaction.description ?? "");
  const amount = Math.round(Number(transaction.amount ?? 0));
  if (!transactionId || amount <= 0) return;

  const duplicate = await env.DB.prepare("SELECT transaction_id FROM transactions WHERE transaction_id = ?")
    .bind(transactionId).first();
  if (duplicate) return;

  const candidates = await env.DB.prepare(
    `SELECT * FROM orders
     WHERE status = 'pending' AND amount = ? AND expires_at >= ?
     ORDER BY created_at ASC`,
  ).bind(amount, nowSeconds()).all<Record<string, unknown>>();
  const normalized = normalizeDescription(description);
  const order = candidates.results.find((candidate) =>
    normalized.includes(normalizeDescription(candidate.payment_code))
  );
  if (!order) return;

  const issued = await issueLicense(env, String(order.user_id));
  const receivedAt = nowSeconds();
  await env.DB.batch([
    env.DB.prepare(
      `INSERT OR IGNORE INTO transactions
       (transaction_id, order_id, amount, description, received_at, raw_json)
       VALUES (?, ?, ?, ?, ?, ?)`,
    ).bind(transactionId, order.id, amount, description, receivedAt, JSON.stringify(transaction)),
    env.DB.prepare(
      `INSERT OR IGNORE INTO licenses
       (id, user_id, plan, token_hash, license_token, issued_at, order_id)
       VALUES (?, ?, 'lifetime', ?, ?, ?, ?)`,
    ).bind(
      issued.payload.license_id,
      order.user_id,
      await sha256(issued.token),
      issued.token,
      issued.payload.issued_at,
      order.id,
    ),
    env.DB.prepare(
      `UPDATE orders SET status = 'paid', paid_at = ?, transaction_id = ?, license_token = ?
       WHERE id = ? AND status = 'pending'`,
    ).bind(receivedAt, transactionId, issued.token, order.id),
  ]);
}

async function cassoWebhook(request: Request, env: Env): Promise<Response> {
  const suppliedSecret = request.headers.get("secure-token") ?? "";
  if (!env.CASSO_WEBHOOK_SECRET || !constantTimeEqual(suppliedSecret, env.CASSO_WEBHOOK_SECRET)) {
    return json({ success: false, error: "unauthorized" }, 401);
  }
  const body: Record<string, unknown> = await request.json<Record<string, unknown>>()
    .catch(() => ({} as Record<string, unknown>));
  if (Number(body.error ?? 0) !== 0) return json({ success: true });
  const data = Array.isArray(body.data) ? body.data : body.data ? [body.data] : [];
  for (const transaction of data) {
    if (transaction && typeof transaction === "object") {
      await handleTransaction(env, transaction as Record<string, unknown>);
    }
  }
  return json({ success: true });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: JSON_HEADERS });
    const url = new URL(request.url);
    try {
      if (request.method === "GET" && url.pathname === "/health") {
        return json({ ok: true, product: env.PRODUCT_ID, plan: "lifetime", amount: Number(env.PRICE_LIFETIME) });
      }
      if (request.method === "GET" && url.pathname === "/v1/app/latest") {
        return latestRelease(env);
      }
      if (request.method === "POST" && url.pathname === "/v1/admin/app-release") {
        return updateRelease(request, env);
      }
      if (request.method === "POST" && url.pathname === "/v1/orders") return createOrder(request, env);
      const statusMatch = url.pathname.match(/^\/v1\/orders\/([A-Za-z0-9_-]+)$/);
      if (request.method === "GET" && statusMatch) return orderStatus(request, env, statusMatch[1]);
      if (request.method === "POST" && url.pathname === "/v1/casso/webhook") return cassoWebhook(request, env);
      return json({ error: "not_found" }, 404);
    } catch (error) {
      console.error(error);
      return json({ error: "internal_error" }, 500);
    }
  },
};
