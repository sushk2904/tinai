"""
api/models.py — SQLAlchemy ORM Models (Phase 1.3)

ALL four tables defined here per TODO §1.3 spec.

Critical constraints encoded:
  • cost_cents uses Numeric(10, 4) — USD cents per tradeoffs-info §3
    (prevents floating-point underflow on micro-transactions)
  • InferenceLog.provider + created_at both have index=True — prevents
    full-table sequential scans on the Phase 3.4 drift query
    (WHERE created_at >= NOW() - INTERVAL '24h' AND provider = ?).
    At 1M rows/day this is non-negotiable (TODO §1.3 ⚠️ note).
  • ProviderStats stores EMA mu/var for all three metrics using
    Numeric(12, 6) — enough precision for Z-score denominator stability.
  • All timestamps use timezone=True (TIMESTAMP(timezone=True) in Postgres) so
    Celery Beat's UTC cron schedule aligns with stored timestamps.
"""

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, Integer,
    Numeric, Text, func, TIMESTAMP,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# InferenceLog — one row per API request through the data plane
# ---------------------------------------------------------------------------
class InferenceLog(Base):
    __tablename__ = "inference_logs"

    # Primary key: BigInteger auto-increment (bigserial in Postgres).
    # Chosen over UUID PK because sequential writes on a bigint PK avoid
    # heap-level page splits under high INSERT throughput.
    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # UUID from the request middleware — links Celery tasks back to the row.
    request_id = Column(UUID(as_uuid=True), nullable=False)

    client_key = Column(Text, nullable=False)

    # INDEX — Phase 3.4 drift query filters on this column in combination
    # with created_at. Without it, Postgres full-scans at 1M rows/day.
    provider = Column(Text, index=True, nullable=False)

    # Routing policy selected: "latency-first" | "cost-first" | "sla-aware"
    policy = Column(Text, nullable=False)

    # SHA-256 hex digest of the raw prompt — used for L1 cache lookup.
    prompt_hash = Column(Text, nullable=False)

    # Wall-clock latency measured from first HTTP byte to last (ms).
    latency_ms = Column(Integer, nullable=False)

    # Nullable — token count is not always returned by all providers.
    token_count = Column(Integer, nullable=True)

    # USD cents (e.g., 0.0150 = $0.00015). NEVER store raw USD floats here.
    # tradeoffs-info §3: all backend math in cents to avoid float underflow.
    cost_cents = Column(Numeric(10, 4), nullable=False)

    error_flag = Column(Boolean, default=False, nullable=False)

    # Raw LLM output — nullable because error responses have no output text.
    output_text = Column(Text, nullable=True)

    # INDEX — Phase 3.4 drift query: WHERE created_at >= NOW() - INTERVAL '24h'
    # Also used by Phase 6.2 live metrics dashboard time-window queries.
    created_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        index=True,
        nullable=False,
    )


# ---------------------------------------------------------------------------
# ProviderStats — durable backup of Redis EMA running statistics
# Redis is the hot path; this table is the source of truth for recovery.
# Written by the Celery update_mab_weights task (Phase 3.2).
# ---------------------------------------------------------------------------
class ProviderStats(Base):
    __tablename__ = "provider_stats"

    # One row per provider. Upserted on each MAB weight update.
    provider = Column(Text, primary_key=True)

    # EMA mean (μ) and variance (σ²) for each metric — tradeoffs-info §2.1.
    # Numeric(12, 6): 6 decimal places needed for Z-score denominator (√σ² + ε).
    ema_latency_mu  = Column(Numeric(12, 6), nullable=False, default=0)
    ema_latency_var = Column(Numeric(12, 6), nullable=False, default=1)
    ema_cost_mu     = Column(Numeric(12, 6), nullable=False, default=0)
    ema_cost_var    = Column(Numeric(12, 6), nullable=False, default=1)
    ema_quality_mu  = Column(Numeric(12, 6), nullable=False, default=0)
    ema_quality_var = Column(Numeric(12, 6), nullable=False, default=1)

    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# DriftSnapshot — nightly Evidently AI drift analysis results (Phase 3.4)
# Celery Beat fires run_drift_analysis() at 2 AM UTC; results stored here.
# ---------------------------------------------------------------------------
class DriftSnapshot(Base):
    __tablename__ = "drift_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Date of the analysis run (not a timestamp — one row per day per provider).
    run_date = Column(Date, nullable=False)

    provider = Column(Text, nullable=False)

    # Evidently drift score: 0.0 (no drift) → 1.0 (full distribution shift).
    drift_score = Column(Numeric(6, 4), nullable=False)

    # How many inference_log rows were included in this analysis window.
    sample_count = Column(Integer, nullable=False)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# ClientBudget — per-client daily spend tracking (Phase 2.2 / 3.5)
# Redis holds the hot counter (budget:daily:{client_key}).
# This table is the durable reset anchor: Celery resets Redis from here.
# ---------------------------------------------------------------------------
class ClientBudget(Base):
    __tablename__ = "client_budgets"

    client_key = Column(Text, primary_key=True)

    # Default 10,000 cents = $100/day. Override per client in the DB.
    daily_limit_cents = Column(
        Numeric(10, 4), nullable=False, default=10000.0000
    )

    # Accumulated spend for the current day (in USD cents).
    spent_today_cents = Column(
        Numeric(10, 4), nullable=False, default=0.0000
    )

    # Timestamp when the daily counter resets (midnight UTC).
    reset_at = Column(TIMESTAMP(timezone=True), nullable=False)