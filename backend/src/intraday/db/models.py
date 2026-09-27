"""ORM models for every table in docs/design/04-database-schema.md.

Enumerations are TEXT + CHECK constraints. Money is NUMERIC(14,2), prices
NUMERIC(14,4), quantities whole shares. Timestamps are timestamptz (UTC).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from intraday.db.base import TS, UUIDPK, Base, MoneyCol, Price, TSNow, check_in

# ---------------------------------------------------------------- enumerations
ROLES = ("admin", "viewer")
SYMBOL_ROLES = ("TRADABLE", "CONTEXT")
ASSET_TYPES = ("COMMON_STOCK", "ETF", "OTHER")
FEEDS = ("iex", "sip")
SENTIMENTS = ("POSITIVE", "NEGATIVE", "NEUTRAL", "UNKNOWN")
SIGNAL_ACTIONS = ("BUY", "NO_TRADE", "EXIT")
DECISION_RESULTS = ("VETOED", "RISK_REJECTED", "BLOCKED", "ORDER_SUBMITTED", "ORDER_FAILED")
AI_MODES = ("OFF", "EXPLAIN", "VETO")
LLM_VERDICTS = ("APPROVE", "REJECT", "ERROR")
LLM_PURPOSES = ("VETO", "EXPLAIN", "NEWS_SENTIMENT")
ORDER_SIDES = ("buy", "sell")
ORDER_INTENTS = ("ENTRY", "TAKE_PROFIT", "STOP_LOSS", "ENGINE_EXIT", "FORCE_CLOSE",
                 "EMERGENCY_CLOSE", "MANUAL_CLOSE")
ORDER_CLASSES = ("simple", "bracket", "oco")
ORDER_STATUSES = ("PENDING_SUBMIT", "SUBMITTED", "UNKNOWN", "PARTIALLY_FILLED", "FILLED",
                  "PENDING_CANCEL", "CANCELED", "REJECTED", "EXPIRED", "REPLACED", "FAILED")
EVENT_SOURCES = ("STREAM", "POLL", "RECONCILE", "LOCAL")
POSITION_STATUSES = ("PENDING_ENTRY", "OPEN", "EXITING", "CLOSED", "ABANDONED")
EXIT_REASONS = ("TAKE_PROFIT", "STOP_LOSS", "TRAILING_STOP", "TIME_EXIT", "MOMENTUM_REVERSAL",
                "VWAP_LOSS", "REGIME_REVERSAL", "EOD_LIQUIDATION", "EMERGENCY", "MANUAL",
                "DAILY_LOSS_FLATTEN", "RECONCILE")
POSITION_EVENT_TYPES = ("STOP_MOVED", "TRAIL_ACTIVATED", "EXIT_SIGNAL", "EXIT_SUBMITTED",
                        "PARTIAL_EXIT", "MARK")
SNAPSHOT_SOURCES = ("BROKER", "DERIVED")
ENGINE_COMMANDS = ("START", "PAUSE", "EMERGENCY_STOP", "CLOSE_ALL", "CLOSE_POSITION",
                   "RESET_DAILY_LOCK", "RESET_LATCH", "RECONCILE_NOW", "BACKFILL", "SCAN")
COMMAND_STATUSES = ("PENDING", "ACKED", "DONE", "FAILED")
SEVERITIES = ("DEBUG", "INFO", "WARN", "ERROR", "CRITICAL")
ALERT_SEVERITIES = ("INFO", "WARN", "HIGH", "CRITICAL")
BACKTEST_STATUSES = ("QUEUED", "RUNNING", "DONE", "FAILED", "CANCELED")
BACKTEST_SPLITS = ("TRAIN", "VALIDATION", "TEST")



def _fk(target: str, *, nullable: bool = True, ondelete: str | None = None) -> Any:
    return mapped_column(UUID(as_uuid=True), ForeignKey(target, ondelete=ondelete),
                         nullable=nullable)


# ================================================================= identity & security

class User(Base):
    __tablename__ = "users"
    __table_args__ = (check_in("role", ROLES),)

    id: Mapped[UUIDPK]
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_until: Mapped[TS | None]
    created_at: Mapped[TSNow]
    last_login_at: Mapped[TS | None]
    password_changed_at: Mapped[TS | None]


class Session(Base):
    __tablename__ = "sessions"

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 hex of token
    user_id: Mapped[uuid.UUID] = _fk("users.id", nullable=False, ondelete="CASCADE")
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[TSNow]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))
    revoked_at: Mapped[TS | None]


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_ts_desc", text("ts DESC")),)

    id: Mapped[UUIDPK]
    ts: Mapped[TSNow]
    user_id: Mapped[uuid.UUID | None] = _fk("users.id")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(256))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))
    result: Mapped[str] = mapped_column(String(32), nullable=False, server_default="OK")


# ================================================================= configuration

class SettingsVersion(Base):
    __tablename__ = "settings_versions"
    __table_args__ = (
        Index("uq_settings_versions_one_active", "is_active", unique=True,
              postgresql_where=text("is_active")),
    )

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[TSNow]
    created_by: Mapped[uuid.UUID | None] = _fk("users.id")
    comment: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


# ================================================================= universe & market data

class Symbol(Base):
    __tablename__ = "symbols"
    __table_args__ = (
        check_in("role", SYMBOL_ROLES),
        check_in("asset_type", ASSET_TYPES),
        # CLAUDE.md "Tradable assets": only U.S.-issuer common stock can be TRADABLE.
        CheckConstraint("role <> 'TRADABLE' OR (asset_type = 'COMMON_STOCK' AND country = 'US')",
                        name="tradable_is_us_common_stock"),
    )

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str | None] = mapped_column(String(256))
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    country: Mapped[str | None] = mapped_column(String(2))
    sector: Mapped[str | None] = mapped_column(String(64))
    sector_etf: Mapped[str | None] = mapped_column(String(16))
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    notes: Mapped[str | None] = mapped_column(Text)
    alpaca_class: Mapped[str | None] = mapped_column(String(32))
    alpaca_exchange: Mapped[str | None] = mapped_column(String(32))
    alpaca_name: Mapped[str | None] = mapped_column(String(256))
    alpaca_tradable: Mapped[bool | None] = mapped_column(Boolean)
    alpaca_status: Mapped[str | None] = mapped_column(String(32))
    alpaca_synced_at: Mapped[TS | None]
    eligibility: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[TSNow]
    updated_at: Mapped[TSNow]


class Bar1m(Base):
    __tablename__ = "bars_1m"
    __table_args__ = (check_in("feed", FEEDS),)

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    feed: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Price] = mapped_column(nullable=False)
    high: Mapped[Price] = mapped_column(nullable=False)
    low: Mapped[Price] = mapped_column(nullable=False)
    close: Mapped[Price] = mapped_column(nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vwap: Mapped[Price | None]
    trade_count: Mapped[int | None] = mapped_column(Integer)
    is_updated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class Bar1d(Base):
    __tablename__ = "bars_1d"
    __table_args__ = (check_in("feed", FEEDS),)

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    feed: Mapped[str] = mapped_column(String(8), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Price] = mapped_column(nullable=False)
    high: Mapped[Price] = mapped_column(nullable=False)
    low: Mapped[Price] = mapped_column(nullable=False)
    close: Mapped[Price] = mapped_column(nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vwap: Mapped[Price | None]


class MarketCalendarDay(Base):
    """Trading sessions from Alpaca's calendar (regular hours, early closes included)."""

    __tablename__ = "market_calendar"

    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    close_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    early_close: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fetched_at: Mapped[TSNow]


class UniverseScan(Base):
    __tablename__ = "universe_scans"
    __table_args__ = (Index(None, "session_date", "symbol"), Index(None, "scan_id"),
                      Index("ix_universe_scans_ts_desc", text("ts DESC")))

    id: Mapped[UUIDPK]
    scan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    ts: Mapped[TSNow]
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reasons: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    feed: Mapped[str] = mapped_column(String(8), nullable=False, server_default="iex")
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (check_in("sentiment", SENTIMENTS),
                      check_in("sentiment_source", ("NONE", "LLM")))

    id: Mapped[UUIDPK]
    source_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    symbols: Mapped[list[str]] = mapped_column(ARRAY(String(16)), nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_tags: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False,
                                                  server_default="{}")
    sentiment: Mapped[str] = mapped_column(String(16), nullable=False, server_default="UNKNOWN")
    sentiment_source: Mapped[str] = mapped_column(String(8), nullable=False, server_default="NONE")
    fetched_at: Mapped[TSNow]


# ================================================================= decisions (audit chain)

class TradingSignal(Base):
    __tablename__ = "trading_signals"
    __table_args__ = (check_in("action", SIGNAL_ACTIONS), check_in("feed", FEEDS),
                      Index(None, "session_date", "symbol"))

    id: Mapped[UUIDPK]
    ts: Mapped[TSNow]
    bar_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    feed: Mapped[str] = mapped_column(String(8), nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    factor_scores: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    regime: Mapped[str] = mapped_column(String(16), nullable=False)
    signals: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)


class LlmCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (check_in("purpose", LLM_PURPOSES),)

    id: Mapped[UUIDPK]
    ts: Mapped[TSNow]
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response_raw: Mapped[str | None] = mapped_column(Text)
    parsed: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class AiDecision(Base):
    __tablename__ = "ai_decisions"
    __table_args__ = (
        check_in("decision_result", DECISION_RESULTS),
        check_in("ai_mode", AI_MODES),
        CheckConstraint("llm_verdict IS NULL OR llm_verdict IN ('APPROVE','REJECT','ERROR')",
                        name="llm_verdict_valid"),
        Index("ix_ai_decisions_ts_desc", text("ts DESC")),
        Index(None, "symbol", "ts"),
        Index(None, "decision_result"),
    )

    id: Mapped[UUIDPK]
    signal_id: Mapped[uuid.UUID] = _fk("trading_signals.id", nullable=False)
    ts: Mapped[TSNow]
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    market_price: Mapped[Price] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    signals: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning_llm: Mapped[str | None] = mapped_column(Text)
    entry_ref_price: Mapped[Price] = mapped_column(nullable=False)
    stop_loss: Mapped[Price] = mapped_column(nullable=False)
    take_profit: Mapped[Price] = mapped_column(nullable=False)
    risk_reward: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    expected_return: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    estimated_risk: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    holding_period: Mapped[str] = mapped_column(String(16), nullable=False,
                                                server_default="intraday")
    ai_mode: Mapped[str] = mapped_column(String(8), nullable=False)
    llm_verdict: Mapped[str | None] = mapped_column(String(8))
    llm_call_id: Mapped[uuid.UUID | None] = _fk("llm_calls.id")
    risk_amount: Mapped[MoneyCol | None]
    decision_result: Mapped[str] = mapped_column(String(16), nullable=False)
    block_reasons: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False,
                                                     server_default="{}")
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)


class RiskEvaluation(Base):
    __tablename__ = "risk_evaluations"

    id: Mapped[UUIDPK]
    decision_id: Mapped[uuid.UUID] = _fk("ai_decisions.id", nullable=False)
    ts: Mapped[TSNow]
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    sizing: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    position_value: Mapped[MoneyCol] = mapped_column(nullable=False)
    stop_price: Mapped[Price | None]
    take_profit_price: Mapped[Price | None]
    risk_reward: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    max_loss: Mapped[MoneyCol] = mapped_column(nullable=False)
    exposure_after: Mapped[MoneyCol] = mapped_column(nullable=False)
    open_positions_after: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_loss_worst_case_after: Mapped[MoneyCol] = mapped_column(nullable=False)
    gate_blocks: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False,
                                                   server_default="{}")
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)


# ================================================================= positions, orders, fills

class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        check_in("status", POSITION_STATUSES),
        CheckConstraint(
            "exit_reason IS NULL OR exit_reason IN ("
            + ", ".join(f"'{r}'" for r in EXIT_REASONS) + ")", name="exit_reason_valid"),
        CheckConstraint("qty >= 0", name="qty_non_negative"),
        Index(None, "session_date"),
        Index(None, "symbol", "session_date"),
        Index("ix_positions_not_closed", "status", postgresql_where=text("status <> 'CLOSED'")),
    )

    id: Mapped[UUIDPK]
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy: Mapped[str | None] = mapped_column(String(32))
    entry_decision_id: Mapped[uuid.UUID | None] = _fk("ai_decisions.id")
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_entry_price: Mapped[Price | None]
    entry_time: Mapped[TS | None]
    initial_stop: Mapped[Price | None]
    stop_price: Mapped[Price | None]
    take_profit_price: Mapped[Price | None]
    trailing_active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                  server_default=text("false"))
    high_water_price: Mapped[Price | None]
    risk_amount: Mapped[MoneyCol | None]
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    avg_exit_price: Mapped[Price | None]
    exit_time: Mapped[TS | None]
    exit_reason: Mapped[str | None] = mapped_column(String(32))
    exit_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    gross_pnl: Mapped[MoneyCol | None]
    fees: Mapped[MoneyCol | None]
    net_pnl: Mapped[MoneyCol | None]
    return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    holding_seconds: Mapped[int | None] = mapped_column(Integer)
    sector: Mapped[str | None] = mapped_column(String(64))
    config_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[TSNow]
    updated_at: Mapped[TSNow]


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        check_in("side", ORDER_SIDES),
        check_in("intent", ORDER_INTENTS),
        check_in("order_class", ORDER_CLASSES),
        check_in("status", ORDER_STATUSES),
        CheckConstraint("qty > 0", name="qty_positive"),
        Index("ix_orders_open_status", "status", postgresql_where=text(
            "status NOT IN ('FILLED','CANCELED','REJECTED','EXPIRED','FAILED','REPLACED')")),
        Index(None, "position_id"),
    )

    id: Mapped[UUIDPK]
    client_order_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    parent_order_id: Mapped[uuid.UUID | None] = _fk("orders.id")
    position_id: Mapped[uuid.UUID | None] = _fk("positions.id")
    decision_id: Mapped[uuid.UUID | None] = _fk("ai_decisions.id")
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    intent: Mapped[str] = mapped_column(String(16), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    order_class: Mapped[str] = mapped_column(String(8), nullable=False)
    tif: Mapped[str] = mapped_column(String(8), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[Price | None]
    stop_price: Mapped[Price | None]
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    broker_status: Mapped[str | None] = mapped_column(String(32))
    filled_qty: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    filled_avg_price: Mapped[Price | None]
    submitted_at: Mapped[TS | None]
    acked_at: Mapped[TS | None]
    terminal_at: Mapped[TS | None]
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[TSNow]
    updated_at: Mapped[TSNow]


class OrderEvent(Base):
    __tablename__ = "order_events"
    __table_args__ = (check_in("source", EVENT_SOURCES), Index(None, "order_id"))

    id: Mapped[UUIDPK]
    order_id: Mapped[uuid.UUID] = _fk("orders.id", nullable=False)
    ts: Mapped[TSNow]
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class Fill(Base):
    __tablename__ = "fills"
    __table_args__ = (CheckConstraint("qty > 0", name="qty_positive"), Index(None, "order_id"))

    id: Mapped[UUIDPK]
    order_id: Mapped[uuid.UUID] = _fk("orders.id", nullable=False)
    broker_execution_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Price] = mapped_column(nullable=False)
    fee: Mapped[MoneyCol] = mapped_column(nullable=False, server_default="0")
    liquidity: Mapped[str | None] = mapped_column(String(16))


class PositionEvent(Base):
    __tablename__ = "position_events"
    __table_args__ = (check_in("type", POSITION_EVENT_TYPES), Index(None, "position_id"))

    id: Mapped[UUIDPK]
    position_id: Mapped[uuid.UUID] = _fk("positions.id", nullable=False)
    ts: Mapped[TSNow]
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


# ================================================================= portfolio & P&L

class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"
    __table_args__ = (check_in("source", SNAPSHOT_SOURCES),)

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    equity: Mapped[MoneyCol] = mapped_column(nullable=False)
    cash: Mapped[MoneyCol] = mapped_column(nullable=False)
    buying_power: Mapped[MoneyCol] = mapped_column(nullable=False)
    long_market_value: Mapped[MoneyCol] = mapped_column(nullable=False)
    exposure: Mapped[MoneyCol] = mapped_column(nullable=False)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False)
    realized_pnl_today: Mapped[MoneyCol] = mapped_column(nullable=False)
    unrealized_pnl: Mapped[MoneyCol] = mapped_column(nullable=False)
    daily_pnl: Mapped[MoneyCol] = mapped_column(nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)


class DailyPnl(Base):
    __tablename__ = "daily_pnl"

    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    start_equity: Mapped[MoneyCol | None]
    end_equity: Mapped[MoneyCol | None]
    realized_gross: Mapped[MoneyCol] = mapped_column(nullable=False, server_default="0")
    fees: Mapped[MoneyCol] = mapped_column(nullable=False, server_default="0")
    realized_net: Mapped[MoneyCol] = mapped_column(nullable=False, server_default="0")
    trades: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    wins: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    losses: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    win_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    largest_win: Mapped[MoneyCol | None]
    largest_loss: Mapped[MoneyCol | None]
    shares_traded: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    notional_traded: Mapped[MoneyCol] = mapped_column(nullable=False, server_default="0")
    max_intraday_drawdown: Mapped[MoneyCol | None]
    daily_loss_limit_hit: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                       server_default=text("false"))
    finalized_at: Mapped[TS | None]


class StrategyPerformance(Base):
    __tablename__ = "strategy_performance"

    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    strategy: Mapped[str] = mapped_column(String(32), primary_key=True)
    trades: Mapped[int] = mapped_column(Integer, nullable=False)
    wins: Mapped[int] = mapped_column(Integer, nullable=False)
    losses: Mapped[int] = mapped_column(Integer, nullable=False)
    net_pnl: Mapped[MoneyCol] = mapped_column(nullable=False)
    avg_win: Mapped[MoneyCol | None]
    avg_loss: Mapped[MoneyCol | None]
    profit_factor: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    avg_holding_seconds: Mapped[int | None] = mapped_column(Integer)


# ================================================================= engine state, commands, events

class EngineState(Base):
    """Singleton row (id = 1) owned by the engine process."""

    __tablename__ = "engine_state"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False,
                                                         server_default="[]")
    latched: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    trading_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                  server_default=text("false"))
    session_date: Mapped[date | None] = mapped_column(Date)
    heartbeat_at: Mapped[TS | None]
    engine_version: Mapped[str | None] = mapped_column(String(32))
    config_version: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    updated_at: Mapped[TSNow]


class EngineCommand(Base):
    __tablename__ = "engine_commands"
    __table_args__ = (
        check_in("command", ENGINE_COMMANDS),
        check_in("status", COMMAND_STATUSES),
        Index("ix_engine_commands_pending", "ts", postgresql_where=text("status = 'PENDING'")),
    )

    id: Mapped[UUIDPK]
    ts: Mapped[TSNow]
    user_id: Mapped[uuid.UUID | None] = _fk("users.id")
    command: Mapped[str] = mapped_column(String(32), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PENDING")
    acked_at: Mapped[TS | None]
    completed_at: Mapped[TS | None]
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class RiskEvent(Base):
    __tablename__ = "risk_events"
    __table_args__ = (check_in("severity", SEVERITIES), Index(None, "ts"))

    id: Mapped[UUIDPK]
    ts: Mapped[TSNow]
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(16))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")


class SystemEvent(Base):
    __tablename__ = "system_events"
    __table_args__ = (check_in("severity", SEVERITIES), Index(None, "ts"))

    id: Mapped[UUIDPK]
    ts: Mapped[TSNow]
    component: Mapped[str] = mapped_column(String(32), nullable=False)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        check_in("severity", ALERT_SEVERITIES),
        Index("ix_alerts_unacked", "ts", postgresql_where=text("acknowledged_at IS NULL")),
    )

    id: Mapped[UUIDPK]
    ts: Mapped[TSNow]
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(128))
    count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    last_seen_at: Mapped[TSNow]
    acknowledged_at: Mapped[TS | None]
    acknowledged_by: Mapped[uuid.UUID | None] = _fk("users.id")
    channels_sent: Mapped[list[str]] = mapped_column(ARRAY(String(16)), nullable=False,
                                                     server_default="{}")


# ================================================================= backtesting

class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (check_in("status", BACKTEST_STATUSES), check_in("feed", FEEDS))

    id: Mapped[UUIDPK]
    created_at: Mapped[TSNow]
    created_by: Mapped[uuid.UUID | None] = _fk("users.id")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="QUEUED")
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    feed: Mapped[str] = mapped_column(String(8), nullable=False)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[TS | None]
    finished_at: Mapped[TS | None]


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"
    __table_args__ = (check_in("split", BACKTEST_SPLITS), Index(None, "run_id", "split"))

    id: Mapped[UUIDPK]
    run_id: Mapped[uuid.UUID] = _fk("backtest_runs.id", nullable=False, ondelete="CASCADE")
    split: Mapped[str] = mapped_column(String(16), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_entry_price: Mapped[Price] = mapped_column(nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    avg_exit_price: Mapped[Price] = mapped_column(nullable=False)
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    gross_pnl: Mapped[MoneyCol] = mapped_column(nullable=False)
    fees: Mapped[MoneyCol] = mapped_column(nullable=False)
    net_pnl: Mapped[MoneyCol] = mapped_column(nullable=False)
    return_pct: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    holding_seconds: Mapped[int] = mapped_column(Integer, nullable=False)


class BacktestEquity(Base):
    __tablename__ = "backtest_equity"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backtest_runs.id", ondelete="CASCADE"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    equity: Mapped[MoneyCol] = mapped_column(nullable=False)
    drawdown: Mapped[MoneyCol] = mapped_column(nullable=False)


# Tables the engine may only INSERT into (append-only audit; docs/design/04 conventions).
APPEND_ONLY_TABLES = ("audit_log", "order_events", "fills", "trading_signals", "llm_calls",
                      "risk_evaluations", "position_events", "risk_events", "system_events")
