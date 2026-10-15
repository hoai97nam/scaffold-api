"""
Payments router -- Casso / VietQR integration
=============================================

Endpoints
---------
POST   /v1/orders              -> Create a new payment order (returns QR code URL)
GET    /v1/orders/{order_id}   -> Poll order status (requires poll_token query param)
POST   /v1/casso/webhook       -> Receive bank transactions from Casso (server-to-server only)

Flow
----
1. Client  ->  POST /v1/orders   ->  API creates order, returns {qr_url, poll_token, payment_code, ...}
2. User scans QR, transfers money via banking app (memo = payment_code)
3. Bank credits account  ->  Casso detects tx  ->  POSTs to /v1/casso/webhook
4. Webhook matches tx to order via payment_code, issues license token, marks order 'paid'
5. Client polls GET /v1/orders/{id}?token=<poll_token>  ->  receives {status:'paid', license:'ACC1...'}

Security
--------
- Webhook endpoint is authenticated via a secure-token header compared with
  constant-time comparison (prevents timing attacks).
- The client poll_token is stored as SHA-256 in the DB; the raw token is
  returned ONCE at order creation and never persisted in plaintext.
- Orders expire 30 minutes after creation; expired orders are marked on the
  first poll after the deadline (lazy expiry -- no background job needed).
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets
import string
from datetime import datetime, timezone, timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.db.models import PaymentOrder, CassoTransaction
from app.schemas.schemas import (
    CreateOrderRequest,
    CreateOrderResponse,
    BankInfo,
    OrderStatusResponse,
    CassoWebhookPayload,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Payments"])


# -- Helpers ------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _constant_time_equal(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    return hmac.compare_digest(a.encode(), b.encode())


def _random_slug(length: int) -> str:
    """Generate a URL-safe random alphanumeric slug."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _build_vietqr_url(amount: int, payment_code: str) -> str:
    """
    Build a VietQR image URL using img.vietqr.io.

    Format:
      https://img.vietqr.io/image/{BIN}-{ACCOUNT}-compact2.png?amount=...&addInfo=...&accountName=...
    """
    account_name = settings.BANK_ACCOUNT_NAME or "BUSINESS ACCOUNT"
    bin_val = settings.BANK_BIN or "BANK_BIN"
    account_val = settings.BANK_ACCOUNT or "BANK_ACCOUNT"
    params = urlencode({"amount": amount, "addInfo": payment_code, "accountName": account_name})
    return f"https://img.vietqr.io/image/{bin_val}-{account_val}-compact2.png?{params}"


def _normalize_description(value: str) -> str:
    """Uppercase and strip everything except A-Z and 0-9 from a bank memo."""
    return "".join(c for c in value.upper() if c.isalnum())


def _issue_license(user_id: str) -> str:
    """
    Generate a verifiable HMAC-SHA256 license token.

    Format: ACC1.<base64url-payload>.<base64url-hmac>

    The client can verify by re-computing HMAC-SHA256(payload_part, SECRET_KEY)
    and comparing with the sig part.

    In production you may switch to Ed25519 asymmetric signing (see index.ts
    reference) so the desktop app can verify offline without any server call.
    """
    now_ts = int(_now().timestamp())
    license_id = "ACC-AUTO-" + _random_slug(12)
    payload = {
        "user_id": user_id,
        "license_id": license_id,
        "license_type": "lifetime",
        "issued_at": now_ts,
        "key_version": 1,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()

    sig = hmac.new(
        settings.SECRET_KEY.encode(),
        payload_bytes,
        hashlib.sha256,
    ).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()

    return f"ACC1.{payload_b64}.{sig_b64}"


# -- Endpoints ----------------------------------------------------------------

@router.post(
    "/orders",
    response_model=CreateOrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a payment order",
    description=(
        "Creates a pending payment order and returns a VietQR URL for the user to scan.\n\n"
        "- `poll_token` is returned **once** -- the client must store it to poll status later.\n"
        "- `payment_code` is the short memo the user must include in the bank transfer.\n"
        "- Orders expire **30 minutes** after creation.\n\n"
        "Currently supports `plan='lifetime'` only."
    ),
)
async def create_order(
    body: CreateOrderRequest,
    x_app_secret: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> CreateOrderResponse:
    # --- X-App-Secret guard ---------------------------------------------------
    # This endpoint is intentionally unauthenticated in the JWT/API-key sense
    # because the buyer may not have an account yet. Instead, we require a
    # shared app secret that is embedded in the desktop app binary.
    #
    # Constant-time comparison prevents timing-oracle attacks (an attacker
    # probing character by character to brute-force the secret).
    app_secret = settings.APP_SECRET
    if not app_secret:
        # Dev mode: warn loudly but don't block, so local dev works out of the box.
        logger.warning(
            "APP_SECRET is not set. X-App-Secret auth is DISABLED. "
            "Set APP_SECRET in .env before going to production!"
        )
    elif not x_app_secret or not _constant_time_equal(x_app_secret, app_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-App-Secret header.",
        )
    # --------------------------------------------------------------------------

    if body.plan != "lifetime":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only 'lifetime' plan is currently supported.",
        )

    now = _now()
    order_id = _random_slug(18)
    poll_token = _random_slug(24)
    payment_code = "ACC" + _random_slug(9)
    amount = settings.PRICE_LIFETIME
    expires_at = now + timedelta(minutes=30)

    order = PaymentOrder(
        id=order_id,
        user_id=body.user_id,
        plan=body.plan,
        amount=amount,
        payment_code=payment_code,
        poll_token_hash=_sha256(poll_token),
        status="pending",
        created_at=now,
        expires_at=expires_at,
    )
    db.add(order)
    await db.commit()

    logger.info(
        "Payment order created: order_id=%s user_id=%s amount=%s",
        order_id, body.user_id, amount,
    )

    return CreateOrderResponse(
        order_id=order_id,
        poll_token=poll_token,
        status="pending",
        plan=body.plan,
        amount=amount,
        payment_code=payment_code,
        expires_at=expires_at,
        bank=BankInfo(
            bin=settings.BANK_BIN or "BANK_BIN",
            account=settings.BANK_ACCOUNT or "BANK_ACCOUNT",
            account_name=settings.BANK_ACCOUNT_NAME or "BUSINESS ACCOUNT",
        ),
        qr_url=_build_vietqr_url(amount, payment_code),
    )


@router.get(
    "/orders/{order_id}",
    response_model=OrderStatusResponse,
    summary="Poll order status",
    description=(
        "Returns the current status of a payment order.\n\n"
        "Requires `?token=<poll_token>` as a query parameter.\n\n"
        "When `status == 'paid'`, the response includes the `license` token."
    ),
)
async def get_order_status(
    order_id: str,
    token: str = "",
    db: AsyncSession = Depends(get_db),
) -> OrderStatusResponse:
    result = await db.execute(
        select(PaymentOrder).where(PaymentOrder.id == order_id)
    )
    order = result.scalar_one_or_none()

    # Always compute hash even if order is None to prevent timing oracle
    supplied_hash = _sha256(token)
    if not order or not _constant_time_equal(supplied_hash, order.poll_token_hash):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found.",
        )

    # Lazy expiry: mark pending orders as expired once the deadline passes
    if order.status == "pending" and order.expires_at < _now():
        order.status = "expired"
        await db.commit()
        logger.info("Order %s lazily expired.", order_id)

    return OrderStatusResponse(
        order_id=order.id,
        status=order.status,
        plan=order.plan,
        amount=order.amount,
        payment_code=order.payment_code,
        expires_at=order.expires_at,
        paid_at=order.paid_at,
        license=order.license_token if order.status == "paid" else None,
    )


# -- Casso Webhook ------------------------------------------------------------

@router.post(
    "/casso/webhook",
    summary="Casso bank webhook (server-to-server only)",
    description=(
        "**This endpoint is called ONLY by Casso's servers -- never by your app.**\n\n"
        "Register this URL once in the Casso dashboard under *Webhook URL*.\n\n"
        "Steps on each call:\n"
        "1. Verifies `secure-token` header (constant-time comparison).\n"
        "2. Skips duplicate transactions (idempotency guard).\n"
        "3. Finds a matching pending order by `payment_code` in the bank memo.\n"
        "4. Issues a license token and marks the order `paid`.\n\n"
        "Always returns `{success: true}` to Casso to prevent unwanted retries."
    ),
)
async def casso_webhook(
    payload: CassoWebhookPayload,
    secure_token: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
):
    # 1. Authenticate via constant-time header comparison
    webhook_secret = settings.CASSO_WEBHOOK_SECRET
    if not webhook_secret:
        logger.warning(
            "CASSO_WEBHOOK_SECRET is not set -- webhook auth is DISABLED. "
            "Set it in .env before going to production!"
        )
    elif not secure_token or not _constant_time_equal(secure_token, webhook_secret):
        logger.warning("Casso webhook: rejected request with invalid secure-token.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret.",
        )

    # 2. error != 0 is a Casso heartbeat/test ping -- acknowledge and skip
    if payload.error != 0:
        return {"success": True}

    # 3. Process each transaction in the batch
    for tx in payload.data:
        tx_id = tx.transaction_id
        if not tx_id or tx.amount <= 0:
            logger.debug("Skipping invalid transaction: id=%s amount=%s", tx_id, tx.amount)
            continue

        await _handle_transaction(
            db,
            transaction_id=tx_id,
            amount=tx.amount,
            description=tx.description or "",
            raw_json=tx.model_dump(),
        )

    return {"success": True}


async def _handle_transaction(
    db: AsyncSession,
    transaction_id: str,
    amount: int,
    description: str,
    raw_json: dict,
) -> None:
    """
    Core idempotent payment-matching logic (called per Casso transaction).

    Steps:
    1. Check for a duplicate transaction -- skip if already processed.
    2. Find a matching pending order: same amount + payment_code in the memo.
    3. Issue a license token.
    4. Record the CassoTransaction and update the PaymentOrder atomically.
    """
    # 1. Idempotency guard
    existing_result = await db.execute(
        select(CassoTransaction).where(CassoTransaction.transaction_id == transaction_id)
    )
    if existing_result.scalar_one_or_none():
        logger.info("Transaction %s already processed -- skipping duplicate.", transaction_id)
        return

    now = _now()

    # 2. Find matching pending order (same amount + payment_code substring in memo)
    candidates_result = await db.execute(
        select(PaymentOrder)
        .where(
            PaymentOrder.status == "pending",
            PaymentOrder.amount == amount,
            PaymentOrder.expires_at >= now,
        )
        .order_by(PaymentOrder.created_at.asc())   # FIFO: match oldest order first
    )
    candidates = candidates_result.scalars().all()

    normalized_desc = _normalize_description(description)
    matched_order = None
    for candidate in candidates:
        if _normalize_description(candidate.payment_code) in normalized_desc:
            matched_order = candidate
            break

    if not matched_order:
        logger.info(
            "Casso tx %s (amount=%s VND) matched no pending order. description=%r",
            transaction_id, amount, description,
        )
        db.add(CassoTransaction(
            transaction_id=transaction_id,
            order_id=None,
            amount=amount,
            description=description,
            raw_json=raw_json,
            received_at=now,
        ))
        await db.commit()
        return

    # 3. Issue license
    license_token = _issue_license(matched_order.user_id)

    # 4. Persist atomically
    db.add(CassoTransaction(
        transaction_id=transaction_id,
        order_id=matched_order.id,
        amount=amount,
        description=description,
        raw_json=raw_json,
        received_at=now,
    ))

    matched_order.status = "paid"
    matched_order.paid_at = now
    matched_order.transaction_id = transaction_id
    matched_order.license_token = license_token

    await db.commit()

    logger.info(
        "Order %s marked PAID via Casso tx %s. License issued for user=%s.",
        matched_order.id, transaction_id, matched_order.user_id,
    )
