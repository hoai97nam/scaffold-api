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


UserProfile.model_rebuild()
UsageStats.model_rebuild()
