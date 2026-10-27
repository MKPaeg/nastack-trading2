from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .db import now


def identifier() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class UserBrokerKey(Base):
    __tablename__ = "user_broker_keys"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    broker: Mapped[str] = mapped_column(String(10), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    key_ciphertext: Mapped[str] = mapped_column(Text)
    secret_ciphertext: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TradePlan(Base):
    __tablename__ = "trade_plans"
    __table_args__ = (UniqueConstraint("owner_id", "broker", "code"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    broker: Mapped[str] = mapped_column(String(10))
    code: Mapped[str] = mapped_column(String(6))
    version: Mapped[int] = mapped_column(Integer, default=1)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PaperWallet(Base):
    __tablename__ = "paper_wallets"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    value: Mapped[dict] = mapped_column(JSON)


class PaperEvent(Base):
    __tablename__ = "paper_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    value: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TradeOrder(Base):
    __tablename__ = "trade_orders"
    __table_args__ = (UniqueConstraint("owner_id", "request_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    plan_id: Mapped[str] = mapped_column(ForeignKey("trade_plans.id"))
    request_key: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20))
    value: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class StockFactorProfile(Base):
    __tablename__ = "stock_factor_profiles"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BusinessResearch(Base):
    """Personal, versioned web evidence and LLM business summaries; no broker data."""

    __tablename__ = "business_research"
    __table_args__ = (
        UniqueConstraint("owner_id", "request_key"),
        Index("ix_business_research_owner_code", "tenant_id", "owner_id", "code", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    code: Mapped[str] = mapped_column(String(6))
    request_key: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20))
    value: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class VectorSelection(Base):
    __tablename__ = "vector_selections"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class VectorResult(Base):
    __tablename__ = "vector_results"
    __table_args__ = (
        UniqueConstraint("owner_id", "fingerprint"),
        Index("ix_vector_result_input_signature", "tenant_id", "owner_id", "input_signature"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    # Kept outside JSON so a cache hit does not decode past result payloads.
    input_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    value: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DataCollectionPolicy(Base):
    __tablename__ = "data_collection_policies"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DataCollectionTask(Base):
    __tablename__ = "data_collection_tasks"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    resource: Mapped[str] = mapped_column(String(100), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="waiting")
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CollectionSearch(Base):
    __tablename__ = "collection_searches"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    status: Mapped[str] = mapped_column(String(20))
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CollectionWorkflow(Base):
    __tablename__ = "collection_workflows"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class NewsRun(Base):
    __tablename__ = "news_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    news_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(20))
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class NewsArticle(Base):
    __tablename__ = "news_articles"
    __table_args__ = (UniqueConstraint("owner_id", "fingerprint"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("personal_sources.id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    news_date: Mapped[date] = mapped_column(Date, index=True)
    value: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DimensionExclusion(Base):
    __tablename__ = "dimension_exclusions"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    dimension_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    reason: Mapped[str] = mapped_column(Text)
    definition: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DimensionCollection(Base):
    __tablename__ = "dimension_collections"
    __table_args__ = (
        Index("ix_dimension_owner_range", "tenant_id", "owner_id", "dimension_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    dimension_id: Mapped[str] = mapped_column(String(40))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    name: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64))


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(254))
    role: Mapped[str] = mapped_column(String(16), default="viewer")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    session_hours: Mapped[int] = mapped_column(Integer, default=24, server_default="24")
    code_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    public_id: Mapped[str] = mapped_column(String(36), default=identifier, unique=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=now)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    device: Mapped[str] = mapped_column(String(40), default="기존 세션", server_default="기존 세션")
    browser: Mapped[str] = mapped_column(String(40), default="정보 없음", server_default="정보 없음")
    user_agent: Mapped[str] = mapped_column(String(500), default="", server_default="")
    client_ip: Mapped[str] = mapped_column(String(45), default="", server_default="")


class RememberedLogin(Base):
    __tablename__ = "remembered_logins"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AdminSettings(Base):
    __tablename__ = "admin_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    default_session_hours: Mapped[int] = mapped_column(Integer, default=24)
    max_session_hours: Mapped[int] = mapped_column(Integer, default=168)
    version: Mapped[int] = mapped_column(Integer, default=1)
    code_ttl_minutes: Mapped[int] = mapped_column(Integer, default=10)
    code_max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    server_orders_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    server_automatic_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    trading_safety_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class LoginChallenge(Base):
    __tablename__ = "login_challenges"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(256))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    bucket: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    window_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class StatisticSeries(Base):
    __tablename__ = "statistic_series"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    series_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UserSettings(Base):
    __tablename__ = "user_settings"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SchedulePolicy(Base):
    """User-owned recurring-task plan; execution is a separate, explicit SDLC stage."""

    __tablename__ = "schedule_policies"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceCatalog(Base):
    __tablename__ = "source_catalog"
    __table_args__ = (UniqueConstraint("tenant_id", "domain"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    domain: Mapped[str] = mapped_column(String(253))
    version: Mapped[int] = mapped_column(Integer, default=1)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UserSourceList(Base):
    __tablename__ = "user_source_lists"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    initialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PersonalSource(Base):
    __tablename__ = "personal_sources"
    __table_args__ = (UniqueConstraint("owner_id", "domain"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    template_id: Mapped[str | None] = mapped_column(ForeignKey("source_catalog.id"), nullable=True)
    domain: Mapped[str] = mapped_column(String(253))
    version: Mapped[int] = mapped_column(Integer, default=1)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (
        UniqueConstraint("owner_id", "request_key"),
        Index("ix_runs_queue", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(6))
    kind: Mapped[str] = mapped_column(String(32), default="pipeline_demo")
    status: Mapped[str] = mapped_column(String(16), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(String(200), default="작업 대기 중")
    settings_version: Mapped[int] = mapped_column(Integer)
    settings_snapshot: Mapped[dict] = mapped_column(JSON)
    source_catalog_snapshot: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    instrument_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RealizedSale(Base):
    """One cumulative confirmed sell per order; partial updates never add duplicates."""

    __tablename__ = "realized_sales"
    __table_args__ = (
        UniqueConstraint("order_id"),
        Index("ix_realized_owner_day", "owner_id", "tenant_id", "trade_day"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    order_id: Mapped[str] = mapped_column(ForeignKey("trade_orders.id"))
    plan_id: Mapped[str] = mapped_column(ForeignKey("trade_plans.id"))
    broker: Mapped[str] = mapped_column(String(10))
    code: Mapped[str] = mapped_column(String(6))
    trade_day: Mapped[date] = mapped_column(Date)
    quantity: Mapped[int] = mapped_column(Integer)
    proceeds: Mapped[float] = mapped_column(Numeric(24, 6))
    cost: Mapped[float | None] = mapped_column(Numeric(24, 6), nullable=True)
    profit: Mapped[float | None] = mapped_column(Numeric(24, 6), nullable=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    code: Mapped[str] = mapped_column(String(6))
    version: Mapped[int] = mapped_column(Integer, default=1)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class InstrumentMaster(Base):
    """Public lookup snapshot, separate from enabled tenant instruments."""

    __tablename__ = "instrument_masters"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("owner_id", "instrument_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BrokerConnection(Base):
    __tablename__ = "broker_connections"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    environment: Mapped[str] = mapped_column(String(8), default="mock")
    version: Mapped[int] = mapped_column(Integer, default=1)
    next_probe_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KiwoomConnection(Base):
    __tablename__ = "kiwoom_connections"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    environment: Mapped[str] = mapped_column(String(8), default="mock")
    version: Mapped[int] = mapped_column(Integer, default=1)
    token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(40), nullable=True)


class BrokerSnapshot(Base):
    """User-scoped, timestamped read-only market or portfolio result."""

    __tablename__ = "broker_snapshots"
    __table_args__ = (Index("ix_broker_snapshots_lookup", "owner_id", "kind", "symbol", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(6), default="")
    environment: Mapped[str] = mapped_column(String(8))
    value: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BrokerProbe(Base):
    __tablename__ = "broker_probes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    policy_version: Mapped[int] = mapped_column(Integer)
    environment: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), default="running")
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reused_token: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SystemLog(Base):
    __tablename__ = "system_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    level: Mapped[str] = mapped_column(String(10), default="INFO")
    message: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
