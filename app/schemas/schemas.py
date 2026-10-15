from typing import Optional
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field
from datetime import datetime


# ─────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: Optional[str] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class RefreshRequest(BaseModel):
    refresh_token: str


# ─────────────────────────────────────────────
# User
# ─────────────────────────────────────────────

class UserProfile(BaseModel):
    id: UUID
    email: str
    full_name: Optional[str]
    is_active: bool
    is_verified: bool
    created_at: datetime
    subscription: Optional["SubscriptionStatus"] = None

    class Config:
        from_attributes = True


# ─────────────────────────────────────────────
# Subscription Plans
# ─────────────────────────────────────────────

class PlanOut(BaseModel):
    id: UUID
    name: str
    display_name: str
    price_monthly: float
    price_yearly: float
    requests_per_min: int
    requests_per_day: int
    requests_per_month: int
    max_api_keys: int
    features: dict

    class Config:
        from_attributes = True


class SubscribeRequest(BaseModel):
    plan_name: str = Field(..., description="'free', 'starter', or 'pro'")
    billing_cycle: str = Field("monthly", description="'monthly' or 'yearly'")


class SubscriptionStatus(BaseModel):
    plan_name: str
    display_name: str
    status: str
    billing_cycle: str
    price_monthly: float
    current_period_start: datetime
    current_period_end: datetime
    requests_per_min: int
    requests_per_day: int
    requests_per_month: int

    class Config:
        from_attributes = True


# ─────────────────────────────────────────────
# API Keys
# ─────────────────────────────────────────────

class CreateAPIKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="Friendly label for this key")


class APIKeyOut(BaseModel):
    id: UUID
    name: str
    key_prefix: str
    is_active: bool
    created_at: datetime
    last_used_at: Optional[datetime]
    expires_at: Optional[datetime]

    class Config:
        from_attributes = True


class APIKeyCreated(APIKeyOut):
    """Returned only on creation — raw_key shown ONCE, never again."""
    raw_key: str


# ─────────────────────────────────────────────
# Usage
# ─────────────────────────────────────────────

class UsageStats(BaseModel):
    period: str                     # "2026-10"
    total_requests: int
    monthly_limit: int
    remaining: int
    percent_used: float
    daily_limit: int
    per_minute_limit: int
    recent_logs: list["UsageLogOut"]


class UsageLogOut(BaseModel):
    endpoint: str
    method: str
    status_code: Optional[int]
    response_time_ms: Optional[int]
    created_at: datetime

    class Config:
        from_attributes = True


# ─────────────────────────────────────────────
# Billing
# ─────────────────────────────────────────────

class BillingRecord(BaseModel):
    id: UUID
    amount: float
    currency: str
    status: str
    description: Optional[str]
    period_start: Optional[datetime]
    period_end: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


# ─────────────────────────────────────────────
# Text to Speech (TTS)
# ─────────────────────────────────────────────

class TTSGenerateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000, description="Text to synthesize")
    voice: Optional[str] = Field("BV074_streaming", description="Voice identifier, e.g. 'BV074_streaming' or 'BV421_vivn_streaming'")
    rate: Optional[str] = Field("1.0", description="Speech speed multiplier, e.g. '1.0'")
    wait: Optional[bool] = Field(True, description="Whether to wait for audio generation to complete")


class VoiceOut(BaseModel):
    voice_type: str
    display_name: str
    resource_id: str
    lang: str
    lan: str


class TTSGenerateResponse(BaseModel):
    ok: bool
    task_id: Optional[str] = None
    status: Optional[str] = None
    audio_urls: list[str] = []
    token: Optional[str] = None
    response_time_ms: int


UserProfile.model_rebuild()
UsageStats.model_rebuild()


# ─────────────────────────────────────────────
# Payment / Casso (VietQR + Open Banking)
# ─────────────────────────────────────────────

class CreateOrderRequest(BaseModel):
    """Client sends this to kick off a payment flow."""
    user_id: str = Field(..., description="Opaque user identifier from the client app (e.g. 'ACC-PC-...')")
    plan: str = Field("lifetime", description="Plan to purchase. Currently only 'lifetime' is supported.")


class BankInfo(BaseModel):
    bin: str
    account: str
    account_name: str


class CreateOrderResponse(BaseModel):
    order_id: str
    poll_token: str           # Raw token — returned ONCE. Client stores it for polling.
    status: str               # always 'pending' on creation
    plan: str
    amount: int               # VND
    payment_code: str         # Short memo code the user puts in the bank transfer
    expires_at: datetime
    bank: BankInfo
    qr_url: str               # VietQR image URL, ready to display


class OrderStatusResponse(BaseModel):
    order_id: str
    status: str               # pending | paid | expired
    plan: str
    amount: int
    payment_code: str
    expires_at: datetime
    paid_at: Optional[datetime] = None
    license: Optional[str] = None   # non-null only when status == 'paid'


# ── Casso Webhook Payload ──────────────────────────────────────
# Casso POSTs JSON bodies that look like:
#   {"error": 0, "data": [{"id": 12345, "amount": 590000,
#                          "description": "ACCXYZ123", ...}]}
# We keep validation permissive ("extra = 'allow'") because Casso can add new
# fields at any time and we want the webhook to stay robust.

class CassoTransactionItem(BaseModel):
    model_config = {"extra": "allow"}

    id: Optional[int] = None
    tid: Optional[str] = None           # Casso transaction ID (preferred)
    amount: int = 0
    description: Optional[str] = None
    when: Optional[str] = None          # ISO datetime string from Casso

    @property
    def transaction_id(self) -> str:
        """Return a stable string key for idempotency checks."""
        return str(self.tid or self.id or "")


class CassoWebhookPayload(BaseModel):
    model_config = {"extra": "allow"}

    error: int = 0
    data: list[CassoTransactionItem] = []
