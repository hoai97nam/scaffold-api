# Casso Payment Integration Strategy

> **Role: Senior Solution Architect**
> Source: `index.ts` (Cloudflare Worker)
> Service: [Casso](https://casso.vn) — Vietnamese Open Banking Webhook
> Date: 2026-10-12

---

## 1. System Overview

Casso is a Vietnamese open-banking transaction sync service. It monitors your
bank account and **automatically pushes real-time webhook notifications** to
your API server whenever money is received. You never poll Casso yourself.

### Components

| Component       | Technology              | Role                                               |
|-----------------|-------------------------|----------------------------------------------------|
| Client App      | User device             | Initiates purchase, polls for license              |
| API Server      | Cloudflare Worker       | Manages orders, webhooks, license issuance         |
| Database        | Cloudflare D1 (SQLite)  | Orders, transactions, licenses, app_settings       |
| Casso           | 3rd-party service       | Monitors bank account, pushes webhook to API       |
| Bank            | Vietnamese bank (by BIN)| Receives the actual VND transfer from user         |
| VietQR          | img.vietqr.io           | Generates QR code image for payment                |

---

## 2. Who Calls What — At a Glance

```
YOUR APP                YOUR API SERVER              CASSO              BANK
────────                ──────────────              ──────             ──────
POST /v1/orders ──────► creates order in DB
◄── { qr_url, ... } ──

[shows QR to user]

[user scans QR &
 transfers money] ──────────────────────────────────────────────────► receives money
                                                                            │
                                                   Casso detects tx         │
                                                   on your bank acct ◄──────┘
                                                          │
                         POST /v1/casso/webhook ◄─── Casso calls THIS
                         (automatic, no action
                          needed from your app)
                                │
                         matches order → issues license

GET /v1/orders/{id} ──► returns { status: "paid", license: "ACC1..." }
◄── license token ────
```

> **Key point:** Your app NEVER calls `/v1/casso/webhook` directly.
> That endpoint is ONLY for Casso's servers. You register it ONCE
> in the Casso dashboard and it runs automatically forever.

---

## 3. Full End-to-End Payment Flow

### Phase 1 — Order Creation

1. App calls `POST /v1/orders` with `{ user_id, plan: "lifetime" }`.
2. API server generates:
   - `order_id` — unique order identifier
   - `poll_token` — secret used only for polling (stored as SHA-256 hash in DB)
   - `payment_code` — short code (e.g. `ACCXYZ123`) that user puts in bank memo
   - `expires_at` — 30 minutes from now
3. API builds a VietQR URL with the amount and payment_code pre-filled.
4. API responds with all of the above.

### Phase 2 — User Pays

1. App displays the QR code image from `qr_url`.
2. User opens their banking app, scans the QR code.
3. The QR pre-fills: bank account, amount, and `payment_code` as the memo.
4. User confirms the transfer.

### Phase 3 — App Polls for Status

- App calls `GET /v1/orders/{order_id}?token={poll_token}` every 3–5 seconds.
- Possible responses:
  - `{ status: "pending" }` → keep polling
  - `{ status: "expired" }` → show "Order expired, please try again"
  - `{ status: "paid", license: "ACC1.xxx.yyy" }` → proceed to Phase 5

### Phase 4 — Casso Webhook (Background, Automatic)

This happens with NO action from your app:

1. Bank processes the transfer and notifies Casso.
2. Casso posts to `POST /v1/casso/webhook` on your API server.
3. API server validates the `secure-token` header.
4. API server checks for duplicate `transaction_id` (idempotency).
5. API server finds a matching pending order:
   - Same `amount`
   - `payment_code` found inside the transfer `description` (memo)
6. API server issues an Ed25519-signed license token.
7. API server atomically writes:
   - `transactions` table — records the bank transaction
   - `licenses` table — stores the issued license
   - `orders` table — updates status to `paid`

### Phase 5 — App Receives License

1. The next poll returns `{ status: "paid", license: "ACC1.<payload>.<signature>" }`.
2. App stores the license token locally on the device.
3. App verifies it offline using the embedded Ed25519 public key.
4. App is unlocked. ✅

---

## 4. Exact Steps Your App Must Implement

### Step 1 — Create Order

```
POST https://your-api/v1/orders
Content-Type: application/json

{
  "user_id": "ACC-PC-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX",
  "plan": "lifetime"
}
```

Response:
```json
{
  "order_id": "abc123",
  "poll_token": "xyz...",
  "payment_code": "ACCXYZ123",
  "amount": 299000,
  "expires_at": 1728742800,
  "qr_url": "https://img.vietqr.io/...",
  "bank": {
    "bin": "970415",
    "account": "1234567890",
    "account_name": "NGUYEN VAN A"
  }
}
```

### Step 2 — Show Payment UI

Display all of the following to the user:
- QR image loaded from `qr_url` (memo pre-filled automatically)
- Bank account number + account name (for manual transfer fallback)
- Exact amount to transfer
- Countdown timer to `expires_at`
- "Waiting for payment..." loading indicator

### Step 3 — Poll for Status

```
GET https://your-api/v1/orders/{order_id}?token={poll_token}
```

Handle each status:
- `"pending"` → wait 3–5s, poll again
- `"expired"` → stop polling, show error, let user retry
- `"paid"` → save `license` field, unlock app

### Step 4 — Store & Verify License

- Persist `license` token (e.g. `ACC1.xxx.yyy`) in local storage / keychain.
- Verify offline with Ed25519 public key (ask backend team for the public key).
- Unlock app features.

---

## 5. Security Notes

| Mechanism              | Details                                                    |
|------------------------|------------------------------------------------------------|
| Webhook auth           | `secure-token` header, constant-time compare               |
| Poll token             | SHA-256 hash stored in DB — raw token never persisted      |
| Duplicate prevention   | Idempotency check on `transaction_id` before processing    |
| License signing        | Ed25519 digital signature — unforgeable, offline-verifiable |
| Amount + memo matching | Exact amount AND payment_code must both match              |
| Order expiry           | 30-minute window — reduces DB pollution and fraud surface  |

---

## 6. One-Time Casso Setup (Done by Backend, Not App)

1. Sign up at https://my.casso.vn
2. Connect your Vietnamese bank account.
3. Register webhook:
   - URL: `https://your-api.workers.dev/v1/casso/webhook`
   - Secure token: same value as `CASSO_WEBHOOK_SECRET` env var
   - `income_only: true` (only receive inbound transfers)
4. Set Cloudflare Worker environment variables:
   - `CASSO_WEBHOOK_SECRET`
   - `BANK_BIN`, `BANK_ACCOUNT`, `BANK_ACCOUNT_NAME`
   - `PRICE_LIFETIME`
   - `LICENSE_PRIVATE_JWK`

After this one-time setup, **Casso runs automatically forever** — no further
configuration needed.

---

## 7. Casso Retry Behavior

If your server returns a `4XX` or `5XX`, or does not respond within 10 seconds,
Casso will retry the webhook up to **17 times over 24 hours** using Fibonacci backoff.
Your server handles this safely via the idempotency check on `transaction_id`.

---

## 8. Database Schema (Reference)

```sql
CREATE TABLE orders (
  id              TEXT PRIMARY KEY,   -- random base64url ID
  poll_token_hash TEXT NOT NULL,      -- SHA-256 of poll_token
  user_id         TEXT NOT NULL,      -- ACC-PC-XXXX-... device ID
  plan            TEXT NOT NULL,      -- 'lifetime'
  amount          INTEGER NOT NULL,   -- VND amount
  payment_code    TEXT NOT NULL,      -- short code in transfer memo
  status          TEXT DEFAULT 'pending', -- pending | paid | expired
  created_at      INTEGER,
  expires_at      INTEGER,            -- +1800s (30 min)
  paid_at         INTEGER,
  transaction_id  TEXT,
  license_token   TEXT
);

CREATE TABLE transactions (
  transaction_id  TEXT PRIMARY KEY,   -- Casso tx id
  order_id        TEXT,
  amount          INTEGER,
  description     TEXT,
  received_at     INTEGER,
  raw_json        TEXT               -- full Casso payload
);

CREATE TABLE licenses (
  id            TEXT PRIMARY KEY,    -- ACC-AUTO-XXXX
  user_id       TEXT,
  plan          TEXT,
  token_hash    TEXT,
  license_token TEXT,
  issued_at     INTEGER,
  order_id      TEXT
);
```
