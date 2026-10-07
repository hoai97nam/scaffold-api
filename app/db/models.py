import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, Numeric,
    SmallInteger, String, Text, BigInteger, UniqueConstraint, CheckConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, INET
from sqlalchemy.orm import relationship

from app.db.session import Base


def now_utc():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(Text, nullable=False, unique=True, index=True)
    password_hash = Column(Text, nullable=False)
    full_name = Column(Text)
    is_active = Column(Boolean, nullable=False, default=True)
    is_verified = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_utc)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_utc, onupdate=now_utc)

    # relationships
    subscriptions = relationship("UserSubscription", back_populates="user", cascade="all, delete-orphan")
    api_keys = relationship("APIKey", back_populates="user", cascade="all, delete-orphan")
    usage_logs = relationship("UsageLog", back_populates="user")
    billing_history = relationship("BillingHistory", back_populates="user")


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(Text, nullable=False, unique=True)           # 'free', 'starter', 'pro'
    display_name = Column(Text, nullable=False)
    price_monthly = Column(Numeric(10, 2), nullable=False, default=0)
    price_yearly = Column(Numeric(10, 2), nullable=False, default=0)
    requests_per_min = Column(Integer, nullable=False, default=10)
    requests_per_day = Column(Integer, nullable=False, default=1000)
    requests_per_month = Column(Integer, nullable=False, default=10000)
    max_api_keys = Column(Integer, nullable=False, default=2)
    features = Column(JSONB, nullable=False, default=dict)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_utc)

    subscriptions = relationship("UserSubscription", back_populates="plan")


class UserSubscription(Base):
    __tablename__ = "user_subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_id = Column(UUID(as_uuid=True), ForeignKey("subscription_plans.id"), nullable=False)
    status = Column(
        Text, nullable=False,
        # trialing → active → past_due (grace 3 days) → expired
    )
    billing_cycle = Column(Text, nullable=False)    # 'monthly', 'yearly', 'trial', 'lifetime'
    current_period_start = Column(DateTime(timezone=True), nullable=False)
    current_period_end = Column(DateTime(timezone=True), nullable=False)
    trial_end = Column(DateTime(timezone=True))
    canceled_at = Column(DateTime(timezone=True))
    external_subscription_id = Column(Text)         # Stripe subscription ID
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_utc)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_utc, onupdate=now_utc)

    user = relationship("User", back_populates="subscriptions")
    plan = relationship("SubscriptionPlan", back_populates="subscriptions")


class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    key_hash = Column(Text, nullable=False, unique=True)    # SHA-256 of the raw key
    key_prefix = Column(Text, nullable=False)               # first 16 chars shown in UI
    name = Column(Text, nullable=False)                     # user-friendly label
    last_used_at = Column(DateTime(timezone=True))
    is_active = Column(Boolean, nullable=False, default=True)
    expires_at = Column(DateTime(timezone=True))            # NULL = no expiry
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_utc)
    revoked_at = Column(DateTime(timezone=True))

    user = relationship("User", back_populates="api_keys")
    usage_logs = relationship("UsageLog", back_populates="api_key")


class UsageLog(Base):
    __tablename__ = "usage_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    api_key_id = Column(UUID(as_uuid=True), ForeignKey("api_keys.id"))
    endpoint = Column(Text, nullable=False)
    method = Column(Text, nullable=False)
    status_code = Column(Integer)
    response_time_ms = Column(Integer)
    request_size_bytes = Column(Integer)
    ip_address = Column(Text)
    period_year = Column(SmallInteger, nullable=False)
    period_month = Column(SmallInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_utc)

    user = relationship("User", back_populates="usage_logs")
    api_key = relationship("APIKey", back_populates="usage_logs")


class UsageCounter(Base):
    """Aggregated per-user per-period counter. Avoids COUNT(*) on usage_logs hot path."""
    __tablename__ = "usage_counters"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    period_year = Column(SmallInteger, nullable=False)
    period_month = Column(SmallInteger, nullable=False)
    total_requests = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_utc, onupdate=now_utc)

    __table_args__ = (
        UniqueConstraint("user_id", "period_year", "period_month", name="uq_usage_counters_user_period"),
    )


class BillingHistory(Base):
    """Immutable append-only billing ledger."""
    __tablename__ = "billing_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    subscription_id = Column(UUID(as_uuid=True), ForeignKey("user_subscriptions.id"))
    amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(Text, nullable=False, default="USD")
    status = Column(Text, nullable=False)   # 'pending', 'paid', 'failed', 'refunded'
    description = Column(Text)
    external_payment_id = Column(Text, unique=True)     # Stripe charge ID — idempotency key
    period_start = Column(DateTime(timezone=True))
    period_end = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_utc)

    user = relationship("User", back_populates="billing_history")


class AuditLog(Base):
    """Security/admin events — append-only."""
    __tablename__ = "audit_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    actor_ip = Column(Text)
    event_type = Column(Text, nullable=False)   # 'login', 'key_created', 'key_revoked', 'plan_changed', …
    entity_type = Column(Text)                  # 'api_key', 'subscription', …
    entity_id = Column(Text)
    metadata_ = Column("metadata", JSONB, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_utc)
