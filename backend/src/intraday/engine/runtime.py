"""Engine runtime: gate, heartbeat, commands, settings, market data, trading.

Owns the gate and every loop: heartbeat, commands, settings, market data,
universe scans, the decision cycle (signals, risk, entries, exits), and the
trading loop (entry/exit timeouts, order poll, reconciliation, account
snapshots, end-of-day liquidation). Without a broker (paper keys missing) the
NO_BROKER block keeps entries closed. Every loop runs under a supervisor: an
unexpected error is logged and the loop continues.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday import __version__
from intraday.ai.providers import AIProvider, AnthropicProvider
from intraday.ai.service import AIService
from intraday.alerts.notifier import EmailNotifier, Notifier, SmtpConfig, dispatch
from intraday.core.env import EnvSettings, is_placeholder
from intraday.core.gate import Gate
from intraday.core.logging import get_logger
from intraday.core.runtime_config import (
    FIELD_SPECS,
    ConfigError,
    TradingConfig,
    diff_configs,
    hard_limit_violations,
    parse_config,
)
from intraday.core.safety import PaperModeViolation, paper_mode_problems
from intraday.core.timeutil import ET, session_date, utcnow
from intraday.db.models import AccountSnapshot, DailyPnl, EngineCommand, NewsItem
from intraday.db.repositories import engine as engine_repo
from intraday.db.repositories import marketdata as md_repo
from intraday.db.repositories import portfolio as pf_repo
from intraday.db.repositories import settings as settings_repo
from intraday.db.repositories import trading as trading_repo
from intraday.db.session import transaction
from intraday.engine.decisions import DecisionService
from intraday.engine.risk_adapter import RiskAdapter
from intraday.engine.spool import TradeUpdateSpool
from intraday.marketdata.alpaca.http import AlpacaError
from intraday.marketdata.calendar import MarketCalendar, entry_window
from intraday.marketdata.news import NewsService
from intraday.marketdata.scanner import UniverseScanner
from intraday.marketdata.service import MAX_BACKFILL_DAYS, MarketDataService
from intraday.risk import daily_loss
from intraday.risk.limits import effective_limits
from intraday.risk.models import RiskDecision
from intraday.trading.broker.base import Broker, BrokerError
from intraday.trading.broker.factory import BrokerConfigError
from intraday.trading.exit_engine import ExitEngine
from intraday.trading.liquidation import Liquidator
from intraday.trading.order_manager import EntryRequest, OrderManager
from intraday.trading.reconciler import Reconciler, ReconcileReport

log = get_logger(__name__)

HEARTBEAT_KEY = "engine:heartbeat"
HEARTBEAT_TTL_SECONDS = 15
COMMAND_CHANNEL = "commands:new"
SETTINGS_CHANNEL = "settings:changed"
WATCHLIST_CHANNEL = "watchlist:changed"
AVAILABLE_COMMANDS = frozenset({"START", "PAUSE", "BACKFILL", "SCAN", "RESET_DAILY_LOCK",
                                "RESET_LATCH", "EMERGENCY_STOP", "CLOSE_ALL", "CLOSE_POSITION",
                                "RECONCILE_NOW"})
RESETTABLE_LATCHES = frozenset({"EMERGENCY_STOP", "RECONCILE_MISMATCH", "RISK_ENGINE_ERROR"})
MARKET_COMMANDS = frozenset({"BACKFILL", "SCAN"})
BROKER_COMMANDS = frozenset({"CLOSE_ALL", "CLOSE_POSITION", "RECONCILE_NOW"})
# Long-running: acknowledged at once, completed in the background.
DEFERRED_COMMANDS = MARKET_COMMANDS | BROKER_COMMANDS | {"EMERGENCY_STOP"}
DEFAULT_STALE_SECONDS = 120
ORDER_POLL_SECONDS = 10
ACCOUNT_SNAPSHOT_SECONDS = 60
# Recoverable failures: logged, then retried on the next tick or job.
RECOVERABLE = (AlpacaError, BrokerError, httpx.HTTPError, SQLAlchemyError, OSError,
               TimeoutError)

MarketFactory = Callable[[TradingConfig], tuple[MarketDataService | None, str | None]]
BrokerFactory = Callable[[], Broker]


class EngineRuntime:
    def __init__(self, env: EnvSettings, sessionmaker: async_sessionmaker[AsyncSession],
                 redis: Redis | None, defaults: TradingConfig, *,
                 heartbeat_interval: float = 5.0, command_poll_interval: float = 1.0,
                 settings_poll_interval: float = 5.0, market_interval: float = 5.0,
                 watchlist_interval: float = 30.0, scan_check_interval: float = 60.0,
                 decision_interval: float = 5.0, trading_interval: float = 5.0,
                 market_factory: MarketFactory | None = None,
                 broker_factory: BrokerFactory | None = None,
                 order_manager_options: dict[str, Any] | None = None,
                 ai_provider_factory: Callable[[TradingConfig], AIProvider | None] | None = None,
                 news_interval: float = 120.0,
                 enable_scheduler: bool = False,
                 clock: Callable[[], datetime] = utcnow,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.env = env
        self.sessionmaker = sessionmaker
        self.redis = redis
        self.defaults = defaults
        self.clock = clock
        self.heartbeat_interval = heartbeat_interval
        self.command_poll_interval = command_poll_interval
        self.settings_poll_interval = settings_poll_interval
        self.market_interval = market_interval
        self.watchlist_interval = watchlist_interval
        self.scan_check_interval = scan_check_interval
        self.decision_interval = decision_interval
        self.trading_interval = trading_interval
        self.market_factory = market_factory
        self.broker_factory = broker_factory
        self.ai_provider_factory = ai_provider_factory or self._default_ai_provider
        self.news_interval = news_interval
        self.ai: AIService | None = None
        self._ai_key: tuple[str, str] | None = None
        self.news: NewsService | None = None
        self.notifiers: list[Notifier] = self._default_notifiers()
        self._alerted_blocks: set[str] = set()
        self.order_manager_options = order_manager_options or {}
        self.enable_scheduler = enable_scheduler
        self.sleep = sleep

        self.gate = Gate()
        self.trading_enabled = False
        self.config: TradingConfig | None = None
        self.config_version: int | None = None
        self.boot_config: TradingConfig | None = None
        self.pending_restart: list[str] = []
        self.paper_problems: list[str] = []
        self._wake_commands = asyncio.Event()
        self._wake_settings = asyncio.Event()
        self._wake_watchlist = asyncio.Event()

        self.market: MarketDataService | None = None
        self.scanner: UniverseScanner | None = None
        self.decisions: DecisionService | None = None
        self.risk: RiskAdapter | None = None
        self.correlation_date: Any = None
        self.daily_loss: dict[str, Any] | None = None
        self.market_disabled_reason: str | None = "market data not configured"
        self.calendar = MarketCalendar([])
        self._tasks: set[asyncio.Future[Any]] = set()
        self._scheduler: AsyncIOScheduler | None = None

        self.broker: Broker | None = None
        self.om: OrderManager | None = None
        self.reconciler: Reconciler | None = None
        self.liquidator: Liquidator | None = None
        self.exit_engine: ExitEngine | None = None
        self.spool: TradeUpdateSpool | None = None
        self.account: dict[str, Any] | None = None
        self.stream_state = "idle"
        self._last_poll: datetime | None = None
        self._last_reconcile: datetime | None = None
        self._last_snapshot: datetime | None = None
        self._entries_closed_for: Any = None
        self._wake_reconcile = asyncio.Event()

    # ------------------------------------------------------------------ boot / state

    async def boot(self) -> None:
        now = self.clock()
        self.paper_problems = paper_mode_problems(self.env.trading_mode, self.env.alpaca_base_url)
        if self.paper_problems:
            self.gate.set("CONFIG_ERROR", now, "Paper guard: " + "; ".join(self.paper_problems))
            log.critical("paper_guard_failed", problems=self.paper_problems)

        async with transaction(self.sessionmaker) as s:
            state = await engine_repo.ensure_state(s, status="PAUSED")
            self.trading_enabled = state.trading_enabled
            for block in state.blocks or []:  # latches survive restarts
                if block.get("latched"):
                    since = datetime.fromisoformat(block["since"]) if block.get("since") else now
                    self.gate.set(block["reason"], since, block.get("detail"))
            await settings_repo.ensure_initial(s, self.defaults)
            await engine_repo.system_event(
                s, component="engine", type="ENGINE_STARTED", severity="INFO",
                message=f"Engine {__version__} started",
                details={"paper_guard_ok": not self.paper_problems,
                         "trading_enabled": self.trading_enabled})
        if not self.trading_enabled:
            self.gate.set("PAUSED_BY_USER", now)
        await self.check_settings_once()
        await self._init_market()
        await self._init_broker()
        await self.market_tick()
        await self.write_state()

    @property
    def stale_seconds(self) -> int:
        return self.config.data.stale_seconds if self.config else DEFAULT_STALE_SECONDS

    def _details(self) -> dict[str, Any]:
        now = self.clock()
        market: dict[str, Any] = self.calendar.status(now)
        session = self.calendar.current_or_next(now)
        if session is not None and self.config is not None:
            market["window"] = entry_window(session, self.config.hours).to_json()
        market_data = (self.market.snapshot(now, self.stale_seconds) if self.market
                       else {"enabled": False, "reason": self.market_disabled_reason})
        universe = (self.scanner.last.to_json() if self.scanner and self.scanner.last
                    else None)
        return {"paper_guard": {"ok": not self.paper_problems, "problems": self.paper_problems},
                "pending_restart": self.pending_restart, "market": market,
                "market_data": market_data, "universe": universe,
                "signals": self.decisions.snapshot() if self.decisions else None,
                "daily_loss": self.daily_loss, "broker": self._broker_details(),
                "ai": {"mode": self.config.ai.mode if self.config else None,
                       "enabled": self.ai is not None,
                       "consecutive_failures": self.ai.consecutive_failures if self.ai else 0,
                       "last_error": self.ai.last_error if self.ai else None}}

    def _broker_details(self) -> dict[str, Any]:
        if self.broker is None:
            return {"enabled": False}
        last = self.reconciler.last if self.reconciler else None
        return {"enabled": True, "name": self.broker.name, "stream": self.stream_state,
                "account": self.account, "last_reconcile": last.to_json() if last else None,
                "spooled_updates": len(self.spool) if self.spool else 0,
                "broker_down_reason": self.om.broker_down_reason if self.om else None}

    async def write_state(self) -> None:
        now = self.clock()
        async with transaction(self.sessionmaker) as s:
            await engine_repo.upsert_state(
                s, status=self.gate.display_status(), blocks=self.gate.to_json(),
                latched=self.gate.latched, trading_enabled=self.trading_enabled,
                session_date=session_date(now), heartbeat_at=now, engine_version=__version__,
                config_version=self.config_version, details=self._details(), updated_at=now)

    async def heartbeat_once(self) -> None:
        try:
            await self.write_state()
        except (SQLAlchemyError, OSError) as exc:
            if "DB_DOWN" not in self.gate.blocks:
                log.error("db_unavailable", error=str(exc))
            self.gate.set("DB_DOWN", self.clock(), f"Database unavailable: {type(exc).__name__}")
        else:
            if self.gate.clear("DB_DOWN"):
                log.info("db_recovered")
                await self.write_state()
        if self.redis is not None:
            with contextlib.suppress(RedisError, OSError):
                await self.redis.set(HEARTBEAT_KEY, self.clock().isoformat(),
                                     ex=HEARTBEAT_TTL_SECONDS)

    # ------------------------------------------------------------------ settings

    async def _reference_equity(self, s: AsyncSession, cfg: TradingConfig) -> Decimal:
        res = await s.execute(
            select(AccountSnapshot.equity).where(AccountSnapshot.source == "BROKER")
            .order_by(AccountSnapshot.ts.desc()).limit(1))
        equity = res.scalar_one_or_none()
        return Decimal(equity) if equity is not None else cfg.risk.paper_initial_balance

    async def check_settings_once(self) -> None:
        async with transaction(self.sessionmaker) as s:
            active = await settings_repo.get_active(s)
            if active is None or active.version == self.config_version:
                return
            try:
                cfg = parse_config(active.config)
                violations = hard_limit_violations(cfg, await self._reference_equity(s, cfg))
                if violations:
                    raise ConfigError(violations)
            except ConfigError as exc:
                log.error("settings_rejected", version=active.version, error=str(exc))
                await engine_repo.system_event(
                    s, component="engine", type="SETTINGS_REJECTED", severity="ERROR",
                    message=f"Settings version {active.version} rejected; keeping "
                            f"version {self.config_version}",
                    details={"errors": [{"key": e.key, "message": e.message}
                                        for e in exc.errors]})
                if self.config is None:
                    self.gate.set("CONFIG_ERROR", self.clock(), f"Settings invalid: {exc}")
                return
            if self.boot_config is None:
                self.boot_config = cfg
            else:
                self.pending_restart = sorted(
                    c.key for c in diff_configs(self.boot_config, cfg)
                    if FIELD_SPECS[c.key].policy == "RESTART")
            previous = self.config_version
            self.config, self.config_version = cfg, active.version
            await engine_repo.system_event(
                s, component="engine", type="SETTINGS_APPLIED", severity="INFO",
                message=f"Running settings version {active.version}",
                details={"previous": previous, "pending_restart": self.pending_restart})
        log.info("settings_applied", version=self.config_version,
                 pending_restart=self.pending_restart)

    # ------------------------------------------------------------------ market data

    async def _init_market(self) -> None:
        # The data feed is a RESTART setting: the service is built once, from boot config.
        if self.market_factory is not None and self.config is not None:
            self.market, self.market_disabled_reason = self.market_factory(self.config)
        if self.market is None:
            log.warning("market_data_disabled", reason=self.market_disabled_reason)
            await self._load_db_calendar()
            return
        try:
            await self.market.load_calendar()
            await self.market.sync_calendar()
        except RECOVERABLE as exc:
            log.warning("calendar_sync_failed", error=str(exc),
                        cached_until=str(self.market.calendar.last))
        self.calendar = self.market.calendar
        await self._safe(self.market.refresh_watchlist(), "watchlist_init")
        await self._safe(self.market.load_latest_from_db(), "latest_bars_init")
        self.scanner = UniverseScanner(self.sessionmaker, self.market, clock=self.clock)
        self.decisions = DecisionService(self.sessionmaker, feed=self.market.feed,
                                         clock=self.clock)
        market = self.market
        self.risk = RiskAdapter(self.sessionmaker, feed=market.feed, clock=self.clock,
                                quotes=lambda syms: market.data.get_latest_quotes(
                                    list(syms), market.feed))
        self._spawn(self._sync_then_scan())

    async def _sync_then_scan(self) -> None:
        """Boot: refresh asset data, then run a first scan so reasons are visible at once."""
        if self.market is None:
            return
        await self._safe(self.market.sync_assets(), "asset_sync")
        await self.scan_once()

    async def scan_once(self) -> None:
        if self.scanner is None or self.config is None or self.config_version is None:
            return
        await self._safe(self.scanner.run(self.config, self.config_version), "universe_scan")

    async def scan_tick(self) -> None:
        """During the session, rescan every universe.scan_interval_minutes."""
        if self.scanner is None or self.config is None:
            return
        now = self.clock()
        if self.calendar.is_open(now) and self.scanner.due(
                now, self.config.universe.scan_interval_minutes):
            await self.scan_once()

    async def _load_db_calendar(self) -> None:
        today = self.clock().astimezone(ET).date()
        async with self.sessionmaker() as s:
            self.calendar = await md_repo.load_calendar(s, today - timedelta(days=7),
                                                        today + timedelta(days=30))

    async def market_tick(self) -> None:
        """Clock- and data-driven gate blocks: entry window and market-wide staleness."""
        now = self.clock()
        if self.market is not None:
            self.calendar = self.market.calendar
        elif not self.calendar.covers(now.astimezone(ET).date()):
            await self._load_db_calendar()

        session = self.calendar.current_or_next(now)
        if session is None or self.config is None:
            self.gate.set("OUTSIDE_ENTRY_WINDOW", now)
            self.gate.details["OUTSIDE_ENTRY_WINDOW"] = "Market calendar unavailable"
        else:
            window = entry_window(session, self.config.hours)
            if window.entries_open(now):
                self.gate.clear("OUTSIDE_ENTRY_WINDOW")
            else:
                self.gate.set("OUTSIDE_ENTRY_WINDOW", now)
                self.gate.details["OUTSIDE_ENTRY_WINDOW"] = (
                    f"Next entry window opens "
                    f"{window.trading_start.astimezone(ET):%a %Y-%m-%d %H:%M} ET"
                    if now < window.trading_start else "Entry window closed for today")

        if self.market is None:
            self.gate.set("DATA_STALE", now)
            self.gate.details["DATA_STALE"] = f"Market data disabled: {self.market_disabled_reason}"
            return
        health = self.market.health(now, self.stale_seconds)
        if health.stale:
            if "DATA_STALE" not in self.gate.blocks:
                log.warning("market_data_stale", reasons=health.reasons)
            self.gate.set("DATA_STALE", now)
            self.gate.details["DATA_STALE"] = "; ".join(health.reasons)
        elif self.gate.clear("DATA_STALE"):
            log.info("market_data_fresh")

    async def watchlist_tick(self) -> None:
        if self.market is None:
            return
        await self.market.refresh_watchlist()
        await self.market.sync_assets()  # only symbols never (or long ago) synced

    async def premarket_job(self) -> None:
        """08:45 ET weekdays: calendar, full asset resync, incremental backfill."""
        if self.market is None:
            return
        await self._safe(self.market.sync_calendar(), "premarket_calendar")
        await self._safe(self.market.sync_assets(force=True), "premarket_assets")
        await self._safe(self.market.backfill(), "premarket_backfill")

    async def decision_tick(self) -> None:
        """One signal-pipeline cycle per completed minute: entries, then exit rules.

        Approved proposals are submitted as bracket entries (the order manager
        re-checks the gate right before each submission); exit rules then run
        on every open position with the same bar.
        """
        if (self.decisions is None or self.scanner is None or self.config is None
                or self.config_version is None or "DATA_STALE" in self.gate.blocks):
            return
        universe = sorted(self.scanner.passed)
        held = await self._live_symbols() if self.om is not None else []
        if not universe and not held:
            return
        await self._refresh_correlation(universe)
        blocks = [b["reason"] for b in self.gate.to_json()]
        stale = (self.market.health(self.clock(), self.stale_seconds).stale_symbols
                 if self.market else [])
        self._ensure_ai()
        result = await self._safe(self.decisions.cycle(
            self.calendar, universe, self.config, self.config_version, blocks, risk=self.risk,
            paper_mode_ok=not self.paper_problems, stale_symbols=stale,
            submit=self.submit_decision if self.om is not None else None,
            extra_symbols=held, ai=self.ai), "decision_cycle")
        if self.ai is not None and self.decisions.explain_queue:
            ai, queue = self.ai, self.decisions.explain_queue
            self.decisions.explain_queue = []
            for did, rec, template in queue:   # asynchronous: never delays entries
                self._spawn(self._safe(ai.explain(did, rec, template), "ai_explain"))
        if self.decisions.last_risk_error:
            await self._latch("RISK_ENGINE_ERROR", self.decisions.last_risk_error,
                              severity="CRITICAL")
        if result is not None:
            await self._safe(self._run_exits(), "exit_rules")

    async def _refresh_correlation(self, universe: list[str]) -> None:
        """Once per session: correlation clusters over the lookback (SPEC §35)."""
        today = self.clock().astimezone(ET).date()
        if self.risk is None or self.config is None or self.correlation_date == today:
            return
        await self._safe(self.risk.refresh_correlation(
            universe, self.calendar, self.config.risk.correlation_lookback_days),
            "correlation_refresh")
        self.correlation_date = today

    async def daily_loss_tick(self) -> None:
        """Latch DAILY_LOSS_LIMIT when realized + unrealized loss reaches the limit (SPEC §6).

        The latch blocks new entries immediately and pending entries are
        cancelled. Protective stops stay in place; with risk.daily_loss_action
        FLATTEN every position is closed as well.
        """
        if self.risk is None or self.config is None:
            return
        today = self.clock().astimezone(ET).date()
        async with self.sessionmaker() as s:
            pf = await pf_repo.load_portfolio(s, today, self.risk.feed)
            acct = await pf_repo.load_account(s, self.config.risk.paper_initial_balance)
        limit = effective_limits(self.config, acct.equity)["max_daily_loss"]
        st = daily_loss.status(pf, limit)
        self.daily_loss = {"session_date": today.isoformat(), "daily_pnl": str(st.daily_pnl),
                           "loss_used": str(st.loss_used), "limit": str(limit),
                           "remaining": str(st.remaining), "breached": st.breached}
        if st.breached and "DAILY_LOSS_LIMIT" not in self.gate.blocks:
            await self._latch("DAILY_LOSS_LIMIT",
                              f"Daily loss ${st.loss_used} reached the ${limit} limit "
                              f"(action: {self.config.risk.daily_loss_action})",
                              severity="HIGH", risk_event="DAILY_LOSS_LIMIT_REACHED")
            if self.om is not None:
                await self._safe(self.om.cancel_open_entries("daily loss limit"),
                                 "daily_loss_cancel")
            if self.liquidator is not None and self.config.risk.daily_loss_action == "FLATTEN":
                self._spawn(self.liquidator.run_until_flat("DAILY_LOSS_FLATTEN", "FORCE_CLOSE"))

    async def _latch(self, reason: str, detail: str, *, severity: str,
                     risk_event: str | None = None) -> None:
        if reason in self.gate.blocks:
            return
        self.gate.set(reason, self.clock(), detail)
        log.error("latched", reason=reason, detail=detail)
        async with transaction(self.sessionmaker) as s:
            await engine_repo.raise_alert(s, severity=severity, type=reason, message=detail,
                                          dedupe_key=f"{reason}:{self.clock().date()}")
            if risk_event:
                await engine_repo.risk_event(s, type=risk_event, severity="CRITICAL"
                                             if severity == "CRITICAL" else "ERROR",
                                             details={"detail": detail})
        await self.write_state()

    async def prescan_job(self) -> None:
        """09:25 ET weekdays: scan just before the open with fresh assets and history."""
        await self.scan_once()

    async def postclose_job(self) -> None:
        """16:20 ET weekdays: re-fetch the last 2 sessions fully to capture late corrections."""
        if self.market is None:
            return
        await self._safe(self.market.backfill(days=2, full=True), "postclose_backfill")

    async def _safe(self, coro: Awaitable[Any], name: str) -> Any:
        try:
            return await coro
        except RECOVERABLE as exc:
            log.warning("market_job_failed", job=name, error=str(exc))
            return None

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_background(self) -> None:
        """Wait for spawned work (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        if self.market is not None:
            await self.market.wait_idle()

    # ------------------------------------------------------------------ alerts

    def _default_notifiers(self) -> list[Notifier]:
        e = self.env
        to = [x.strip() for x in e.alert_email_to.split(",") if x.strip()]
        if not e.smtp_host or not e.smtp_from or not to:
            return []
        return [EmailNotifier(SmtpConfig(e.smtp_host, e.smtp_port, e.smtp_user or None,
                                         e.smtp_password.get_secret_value() or None,
                                         e.smtp_from, to))]

    ALERTING_BLOCKS: ClassVar[dict[str, str]] = {
        "BROKER_DOWN": "HIGH", "DATA_STALE": "WARN", "AI_UNAVAILABLE": "WARN",
        "RECONCILING": "WARN", "NO_BROKER": "WARN", "CONFIG_ERROR": "CRITICAL"}

    async def alerts_tick(self) -> None:
        """Raise alerts for degraded states (once per episode), then deliver pending ones."""
        now = self.clock()
        in_session = self.calendar.is_open(now)
        active = {r for r in self.gate.blocks if r in self.ALERTING_BLOCKS
                  and (in_session or r in ("CONFIG_ERROR", "NO_BROKER"))}
        for reason in sorted(active - self._alerted_blocks):
            async with transaction(self.sessionmaker) as s:
                await engine_repo.raise_alert(
                    s, severity=self.ALERTING_BLOCKS[reason], type=reason,
                    message=self.gate.details.get(reason, reason),
                    dedupe_key=f"{reason}:{now.astimezone(ET):%Y%m%d}")
        self._alerted_blocks = active
        cfg = self.config
        if cfg is not None:
            await dispatch(self.sessionmaker, self.notifiers,
                           min_severity=cfg.alerts.email_min_severity,
                           enabled=cfg.alerts.email_enabled)

    # ------------------------------------------------------------------ AI & news

    def _default_ai_provider(self, cfg: TradingConfig) -> AIProvider | None:
        key = self.env.anthropic_api_key
        if key is None or is_placeholder(key.get_secret_value()) or not cfg.ai.model:
            return None
        return AnthropicProvider(key.get_secret_value(), cfg.ai.model,
                                 timeout_seconds=cfg.ai.timeout_seconds)

    def _ensure_ai(self) -> None:
        """(Re)build the AI service when mode/model change; gate AI_UNAVAILABLE in VETO."""
        cfg = self.config
        if cfg is None:
            return
        key = (cfg.ai.mode, cfg.ai.model)
        if key != self._ai_key:
            self._ai_key = key
            provider = self.ai_provider_factory(cfg) if cfg.ai.mode != "OFF" else None
            self.ai = (AIService(self.sessionmaker, provider,
                                 max_consecutive_failures=cfg.ai.max_consecutive_failures)
                       if provider is not None else None)
        now = self.clock()
        if cfg.ai.mode == "VETO" and (self.ai is None or self.ai.unavailable):
            self.gate.set("AI_UNAVAILABLE", now,
                          "AI provider not configured (ANTHROPIC_API_KEY / ai.model)"
                          if self.ai is None else
                          f"AI failed {self.ai.consecutive_failures} times in a row: "
                          f"{self.ai.last_error}")
        else:
            self.gate.clear("AI_UNAVAILABLE")

    async def news_tick(self) -> None:
        cfg = self.config
        if cfg is None or not cfg.news.enabled or self.market is None or self.scanner is None:
            return
        if self.news is None:
            self.news = NewsService(self.sessionmaker, self.market.data, clock=self.clock)
        symbols = sorted(set(self.scanner.passed) | set(await self._live_symbols()))
        if not symbols:
            return
        inserted = await self.news.poll(symbols, cfg.news.lookback_hours)
        self._ensure_ai()
        if (inserted and self.ai is not None and cfg.ai.mode == "VETO"
                and cfg.ai.news_sentiment_enabled):
            async with self.sessionmaker() as s:
                rows = list((await s.execute(select(NewsItem).where(
                    NewsItem.id.in_(inserted[:10])))).scalars())
            for row in rows:
                await self.ai.classify(row)

    # ------------------------------------------------------------------ broker & trading

    async def _init_broker(self) -> None:
        """Build the paper broker (never when the paper guard failed), configure, reconcile."""
        now = self.clock()
        if self.paper_problems:
            self.gate.set("NO_BROKER", now, "Paper guard failed: no broker is built")
            return
        if self.broker_factory is None:
            self.gate.set("NO_BROKER", now, "No broker configured")
            return
        try:
            self.broker = self.broker_factory()
        except PaperModeViolation as exc:
            self.gate.set("CONFIG_ERROR", now, f"Paper guard: {exc}")
            self.gate.set("NO_BROKER", now, "Paper guard failed: no broker is built")
            return
        except BrokerConfigError as exc:
            self.gate.set("NO_BROKER", now, str(exc))
            log.warning("broker_unavailable", reason=str(exc))
            return
        self.gate.clear("NO_BROKER")
        if hasattr(self.broker, "on_stream_state"):
            self.broker.on_stream_state = self._on_stream_state
        self.om = OrderManager(self.sessionmaker, self.broker, clock=self.clock,
                               entry_blocks=lambda: list(self.gate.blocks),
                               **self.order_manager_options)
        self.reconciler = Reconciler(self.sessionmaker, self.broker, self.om, clock=self.clock)
        self.liquidator = Liquidator(self.sessionmaker, self.broker, self.om, clock=self.clock,
                                     sleep=self.sleep)
        self.exit_engine = ExitEngine(self.sessionmaker, self.om, clock=self.clock)
        self.spool = TradeUpdateSpool(self.om.on_trade_update)
        # The engine never assumes flat: entries wait for a clean reconcile.
        self.gate.set("RECONCILING", now)
        await self._check_account(configure=True)
        await self.reconcile_once(boot=True)

    async def _check_account(self, *, configure: bool = False) -> None:
        if self.broker is None:
            return
        try:
            if configure:
                cfg = await self.broker.configure_account()
                log.info("account_configured", config=cfg)
            acct = await self.broker.get_account()
        except BrokerError as exc:
            self.gate.set("BROKER_DOWN", self.clock(), f"Account check failed: {exc.message}")
            log.error("account_check_failed", error=exc.message)
            return
        warnings = []
        if (self.broker.name == "alpaca_paper" and acct.account_number
                and not acct.account_number.startswith("PA")):
            # Alpaca does not document the prefix; the URL guard is the real check.
            warnings.append("account number does not start with PA")
        if acct.trading_blocked or acct.status not in ("ACTIVE", ""):
            self.gate.set("BROKER_DOWN", self.clock(),
                          f"Account not tradable (status {acct.status}, "
                          f"trading_blocked={acct.trading_blocked})")
        self.account = {"status": acct.status, "equity": str(acct.equity),
                        "buying_power": str(acct.buying_power), "multiplier": acct.multiplier,
                        "shorting_enabled": acct.shorting_enabled, "warnings": warnings}
        for w in warnings:
            log.warning("account_warning", warning=w)

    async def _on_stream_state(self, state: str) -> None:
        previous, self.stream_state = self.stream_state, state
        if state == "connected" and previous == "disconnected":
            self._wake_reconcile.set()   # reconcile after every reconnect (stream gap)

    async def reconcile_once(self, *, boot: bool = False) -> ReconcileReport | None:
        if self.reconciler is None:
            return None
        report = await self.reconciler.run(boot=boot)
        self._last_reconcile = self.clock()
        await self._apply_reconcile(report, boot=boot)
        return report

    async def _apply_reconcile(self, report: ReconcileReport, *, boot: bool = False) -> None:
        now = self.clock()
        if not report.ok:
            self.gate.set("BROKER_DOWN", now, f"Broker unavailable: {report.error}")
            return
        if self.om is not None and self.om.broker_down_reason:
            self.gate.set("BROKER_DOWN", now, self.om.broker_down_reason)
        elif self.gate.clear("BROKER_DOWN"):
            log.info("broker_recovered")
        if report.mismatches:
            detail = "; ".join(f"{m['symbol']} {m['type']} (local {m['local_qty']}, "
                               f"broker {m['broker_qty']})" for m in report.mismatches)
            await self._latch("RECONCILE_MISMATCH", detail, severity="HIGH",
                              risk_event="RECONCILE_MISMATCH")
        if report.clean and self.gate.clear("RECONCILING"):
            log.info("reconciled", boot=boot)
        if boot or not report.clean or report.actions:
            async with transaction(self.sessionmaker) as s:
                await engine_repo.system_event(
                    s, component="trading", type="RECONCILE",
                    severity="INFO" if report.clean else "WARN",
                    message="Reconciled with the broker" if report.clean
                    else "Reconciliation found differences", details=report.to_json())

    async def trading_tick(self) -> None:
        """Every few seconds: spool, timeouts, entry-window close, EOD, poll, reconcile."""
        if self.broker is None or self.om is None or self.config is None:
            return
        now = self.clock()
        cfg = self.config
        if self.spool is not None and len(self.spool) and "DB_DOWN" not in self.gate.blocks:
            await self.spool.drain()
            if self.spool.overflowed:
                self.spool.overflowed = False
                self._wake_reconcile.set()
        try:
            await self.om.check_entry_timeouts(cfg.execution.entry_timeout_seconds)
            await self.om.check_exit_timeouts(cfg.execution.exit_fill_timeout_seconds)
            session = self.calendar.session(now.astimezone(ET).date())
            if session is not None:
                window = entry_window(session, cfg.hours)
                if (now >= window.no_new_trades_after
                        and self._entries_closed_for != session.session_date):
                    await self.om.cancel_open_entries("entry window closed")
                    self._entries_closed_for = session.session_date
                if self.liquidator is not None and now >= window.force_close:
                    await self.liquidator.eod_tick(now, window)
            if self._due(self._last_poll, ORDER_POLL_SECONDS):
                await self.om.poll_open_orders()
                self._last_poll = now
            if self._wake_reconcile.is_set() or self._due(
                    self._last_reconcile, cfg.execution.reconcile_interval_seconds):
                self._wake_reconcile.clear()
                await self.reconcile_once()
            if self._due(self._last_snapshot, ACCOUNT_SNAPSHOT_SECONDS):
                await self.account_snapshot()
        except BrokerError as exc:
            self.gate.set("BROKER_DOWN", now, f"Broker unavailable: {exc.message}")
            log.warning("broker_error", error=exc.message)
        if self.om.broker_down_reason:
            self.gate.set("BROKER_DOWN", now, self.om.broker_down_reason)

    def _due(self, last: datetime | None, seconds: float) -> bool:
        return last is None or (self.clock() - last).total_seconds() >= seconds

    async def account_snapshot(self) -> None:
        if self.broker is None or self.config is None:
            return
        acct = await self.broker.get_account()
        positions = await self.broker.get_positions()
        now = self.clock()
        today = now.astimezone(ET).date()
        unrealized = sum((((p.current_price or p.avg_entry_price) - p.avg_entry_price) * p.qty
                          for p in positions), Decimal(0))
        feed = self.market.feed if self.market else self.config.data.feed
        async with transaction(self.sessionmaker) as s:
            pf = await pf_repo.load_portfolio(s, today, feed)
            s.add(AccountSnapshot(ts=now, equity=acct.equity, cash=acct.cash,
                                  buying_power=acct.buying_power,
                                  long_market_value=acct.long_market_value,
                                  exposure=acct.long_market_value,
                                  open_positions=sum(1 for p in positions if p.qty),
                                  realized_pnl_today=pf.realized_pnl_today,
                                  unrealized_pnl=unrealized.quantize(Decimal("0.01")),
                                  daily_pnl=acct.equity - acct.last_equity, source="BROKER"))
            row = await s.get(DailyPnl, today)
            if row is None or row.start_equity is None:
                await trading_repo.set_daily_pnl(s, today, start_equity=acct.last_equity)
        self._last_snapshot = now
        self.account = {**(self.account or {}), "equity": str(acct.equity),
                        "buying_power": str(acct.buying_power), "as_of": now.isoformat()}

    async def submit_decision(self, decision_id: Any, rd: RiskDecision, rec: Any
                              ) -> tuple[str, list[str]]:
        """Approved proposal -> bracket entry. Returns (decision_result, block_reasons)."""
        if self.om is None or rd.proposal is None:
            return "BLOCKED", ["NO_BROKER"]
        p = rd.proposal
        req = EntryRequest(decision_id=decision_id,
                           session_date=self.clock().astimezone(ET).date(), symbol=p.symbol,
                           qty=rd.qty, limit_price=p.entry, stop_price=p.stop,
                           take_profit_price=p.target, strategy=rec.strategy,
                           score=Decimal(str(round(rec.score, 4))),
                           config_version=self.config_version)
        res = await self.om.submit_entry(req)
        if res.status == "SUBMITTED":
            return "ORDER_SUBMITTED", []
        if res.status == "UNKNOWN":
            self.gate.set("BROKER_DOWN", self.clock(), self.om.broker_down_reason or "unknown")
            return "ORDER_SUBMITTED", ["ORDER_STATUS_UNKNOWN"]
        if res.status == "BLOCKED":
            return "BLOCKED", [r[:32] for r in (res.detail or "").split(",") if r]
        return "ORDER_FAILED", ["BROKER_REJECTED" if res.status == "REJECTED"
                                else "ORDER_INVARIANT"]

    async def _live_symbols(self) -> list[str]:
        async with self.sessionmaker() as s:
            return sorted({p.symbol for p in await trading_repo.live_positions(s)})

    async def _run_exits(self) -> None:
        d = self.decisions
        if (self.exit_engine is None or d is None or d.last is None or self.config is None
                or d.last_session is None):
            return
        await self.exit_engine.evaluate(bar_ts=d.last.bar_ts, bars=d.last_bars,
                                        contexts=d.contexts, session=d.last_session,
                                        regime=d.last.regime, cfg=self.config)

    async def overnight_job(self) -> None:
        """16:15 ET: final reconcile, overnight-position alert, finalize daily P&L."""
        if self.broker is None or self.liquidator is None:
            return
        await self._safe(self.reconcile_once(), "final_reconcile")
        await self._safe(self.liquidator.overnight_check(), "overnight_check")
        try:
            acct = await self.broker.get_account()
        except BrokerError:
            return
        today = self.clock().astimezone(ET).date()
        async with transaction(self.sessionmaker) as s:
            await trading_repo.refresh_daily_pnl(s, today)
            await trading_repo.set_daily_pnl(s, today, end_equity=acct.equity,
                                             finalized_at=self.clock())

    # ------------------------------------------------------------------ commands

    def _handle(self, cmd: EngineCommand) -> tuple[bool, dict[str, Any]]:
        now = self.clock()
        if cmd.command not in AVAILABLE_COMMANDS:
            return False, {"error": f"{cmd.command} is not available until a later phase"}
        if cmd.command == "EMERGENCY_STOP":
            # Latch first, synchronously: no new entry can start from here on.
            self.gate.set("EMERGENCY_STOP", now, "Emergency stop was triggered")
            self.trading_enabled = False
            self.gate.set("PAUSED_BY_USER", now)
            return True, {"deferred": True}
        if cmd.command in MARKET_COMMANDS and self.market is None:
            return False, {"error": f"market data disabled: {self.market_disabled_reason}"}
        if cmd.command in BROKER_COMMANDS and self.broker is None:
            return False, {"error": "no broker: " + self.gate.details.get(
                "NO_BROKER", "not configured")}
        if cmd.command == "CLOSE_ALL":
            self.trading_enabled = False       # stays paused until START (05 §4)
            self.gate.set("PAUSED_BY_USER", now)
        if cmd.command in DEFERRED_COMMANDS:
            return True, {"deferred": True}
        if cmd.command == "RESET_DAILY_LOCK":
            return self._reset("DAILY_LOSS_LIMIT")
        if cmd.command == "RESET_LATCH":
            latch = str((cmd.params or {}).get("latch", ""))
            if latch not in RESETTABLE_LATCHES:
                return False, {"error": f"{latch or 'latch'} cannot be reset this way"}
            return self._reset(latch)
        if cmd.command == "PAUSE":
            self.trading_enabled = False
            self.gate.set("PAUSED_BY_USER", now)
            return True, {"trading_enabled": False}
        # START
        if "CONFIG_ERROR" in self.gate.blocks:
            return False, {"error": "configuration error; see blocks"}
        if self.gate.latched:
            return False, {"error": f"reset required: {', '.join(self.gate.latched)}"}
        self.trading_enabled = True
        self.gate.clear("PAUSED_BY_USER")
        return True, {"trading_enabled": True, "entries_allowed": self.gate.entries_allowed,
                      "blocks": [b["reason"] for b in self.gate.to_json()]}

    def _reset(self, latch: str) -> tuple[bool, dict[str, Any]]:
        """Clear a latch by hand. Trading stays paused until an explicit START."""
        if latch not in self.gate.blocks:
            return False, {"error": f"{latch} is not active"}
        self.gate.clear(latch, manual=True)
        self.trading_enabled = False
        self.gate.set("PAUSED_BY_USER", self.clock())
        return True, {"cleared": latch, "trading_enabled": False,
                      "note": "trading remains paused until START"}

    async def process_commands_once(self) -> int:
        async with transaction(self.sessionmaker) as s:
            cmds = await engine_repo.claim_pending_commands(s, self.clock())
            for cmd in cmds:
                ok, result = self._handle(cmd)
                if ok and result.get("deferred"):
                    # Long-running: completes the command row when done.
                    self._spawn(self._run_deferred(cmd.id, cmd.command, dict(cmd.params)))
                    continue
                await engine_repo.complete_command(s, cmd.id, ok=ok, result=result,
                                                   now=self.clock())
                await engine_repo.system_event(
                    s, component="engine", type=f"COMMAND_{cmd.command}",
                    severity="INFO" if ok else "WARN",
                    message=f"{cmd.command} {'done' if ok else 'refused'}", details=result)
                log.info("command_processed", command=cmd.command, ok=ok, result=result)
        if cmds:
            await self.write_state()
        return len(cmds)

    async def _run_deferred(self, command_id: uuid.UUID, command: str,
                            params: dict[str, Any]) -> None:
        if command in BROKER_COMMANDS or command == "EMERGENCY_STOP":
            await self._run_trading_command(command_id, command, params)
            return
        market = self.market
        if market is None:  # pragma: no cover - refused in _handle
            return
        try:
            if command == "BACKFILL":
                days = int(params.get("days") or 0) or None
                if days is not None:
                    days = max(1, min(days, MAX_BACKFILL_DAYS))
                report = await market.backfill(days, full=bool(params.get("full")))
                ok, result = True, report.to_json()
            else:  # SCAN
                if self.scanner is None or self.config is None or self.config_version is None:
                    raise RuntimeError("scanner not ready")
                summary = await self.scanner.run(self.config, self.config_version)
                ok, result = True, summary.to_json()
        except Exception as exc:  # any failure is reported on the command
            ok, result = False, {"error": f"{type(exc).__name__}: {exc}"[:500]}
        async with transaction(self.sessionmaker) as s:
            await engine_repo.complete_command(s, command_id, ok=ok, result=result,
                                               now=self.clock())
            await engine_repo.system_event(
                s, component="market_data", type=f"COMMAND_{command}",
                severity="INFO" if ok else "WARN",
                message=f"{command.title()} {'done' if ok else 'failed'}", details=result)

    async def _run_trading_command(self, command_id: uuid.UUID, command: str,
                                   params: dict[str, Any]) -> None:
        ok, result = True, dict[str, Any]()
        try:
            if command == "EMERGENCY_STOP":
                ok, result = await self._emergency_stop(params)
            elif command == "CLOSE_ALL" and self.liquidator is not None:
                flat = await self.liquidator.run_until_flat("MANUAL", "MANUAL_CLOSE")
                ok, result = flat, {"flat": flat}
            elif command == "CLOSE_POSITION" and self.om is not None:
                ok, result = await self._close_position(params)
            elif command == "RECONCILE_NOW":
                report = await self.reconcile_once()
                result = report.to_json() if report else {}
                ok = bool(report and report.ok)
        except Exception as exc:  # any failure is reported on the command
            log.exception("trading_command_failed", command=command)
            ok, result = False, {"error": f"{type(exc).__name__}: {exc}"[:500]}
        async with transaction(self.sessionmaker) as s:
            await engine_repo.complete_command(s, command_id, ok=ok, result=result,
                                               now=self.clock())
            await engine_repo.system_event(
                s, component="trading", type=f"COMMAND_{command}",
                severity="WARN" if command == "EMERGENCY_STOP" or not ok else "INFO",
                message=f"{command.replace('_', ' ').title()} {'done' if ok else 'failed'}",
                details=result)
        await self.write_state()

    async def _emergency_stop(self, params: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Latched already; cancel entries, then close everything unless told to keep stops."""
        default = (self.config.risk.emergency_stop_action == "CLOSE_ALL"
                   if self.config else True)
        close = bool(params.get("close_positions", default))
        async with transaction(self.sessionmaker) as s:
            await engine_repo.raise_alert(s, severity="HIGH", type="EMERGENCY_STOP",
                                          message="Emergency stop triggered"
                                          + (" - closing all positions" if close else ""),
                                          dedupe_key=f"EMERGENCY_STOP:{self.clock():%Y%m%d%H%M}")
        await self.write_state()
        if self.om is None or self.liquidator is None:
            return True, {"latched": True, "close_positions": close, "note": "no broker"}
        canceled = await self.om.cancel_open_entries("emergency stop")
        if not close:
            return True, {"latched": True, "close_positions": False, "entries_canceled": canceled}
        flat = await self.liquidator.run_until_flat("EMERGENCY", "EMERGENCY_CLOSE")
        return flat, {"latched": True, "close_positions": True, "entries_canceled": canceled,
                      "flat": flat}

    async def _close_position(self, params: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        if self.om is None:
            return False, {"error": "no broker"}
        pid = params.get("position_id")
        if pid:
            outcome = await self.om.exit_position(uuid.UUID(str(pid)), "MANUAL", "MANUAL_CLOSE")
            return outcome in ("SUBMITTED", "CLOSED_BY_LEG", "ALREADY_EXITING"), {
                "outcome": outcome}
        # A broker position with no local record (UNEXPECTED_POSITION): sell what is held.
        symbol = str(params.get("symbol") or "")
        if self.broker is None or not symbol:
            return False, {"error": "position_id or symbol required"}
        qty = next((p.qty for p in await self.broker.get_positions() if p.symbol == symbol), 0)
        if qty <= 0:
            return False, {"error": f"broker holds no long {symbol} position"}
        for o in await self.broker.get_open_orders():
            for x in (o, *o.legs):
                if x.symbol == symbol and not x.terminal:
                    with contextlib.suppress(BrokerError):
                        await self.broker.cancel_order(x.id)
        status = await self.om.submit_sell(symbol, qty, "MANUAL_CLOSE", "MANUAL", None)
        return status == "SUBMITTED", {"outcome": status, "qty": qty}

    # ------------------------------------------------------------------ loops

    async def _loop(self, name: str, interval: float, fn: Callable[[], Awaitable[Any]],
                    wake: asyncio.Event | None, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await fn()
            except RECOVERABLE as exc:
                log.warning("loop_iteration_failed", loop=name, error=str(exc))
            except Exception:  # supervisor: an unexpected error never kills the loop
                log.exception("loop_iteration_crashed", loop=name)
            waiters = [asyncio.ensure_future(stop.wait())]
            if wake is not None:
                waiters.append(asyncio.ensure_future(wake.wait()))
            _, pending = await asyncio.wait(waiters, timeout=interval,
                                            return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
            if wake is not None:
                wake.clear()

    async def _listen(self, stop: asyncio.Event) -> None:
        """Wake loops on Redis notifications. Optional: polling covers Redis outages."""
        if self.redis is None:
            return
        wakes = {COMMAND_CHANNEL: self._wake_commands, SETTINGS_CHANNEL: self._wake_settings,
                 WATCHLIST_CHANNEL: self._wake_watchlist}
        backoff = 1.0
        while not stop.is_set():
            try:
                pubsub = self.redis.pubsub()
                await pubsub.subscribe(*wakes)
                backoff = 1.0
                while not stop.is_set():
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if msg is None:
                        continue
                    channel = msg["channel"].decode() if isinstance(msg["channel"], bytes) \
                        else msg["channel"]
                    if channel in wakes:
                        wakes[channel].set()
            except (RedisError, OSError, json.JSONDecodeError) as exc:
                log.warning("redis_listener_error", error=str(exc), retry_in=backoff)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, 30.0)

    async def _supervise(self, name: str, factory: Callable[[], Awaitable[None]],
                         stop: asyncio.Event) -> None:
        """Run a long-lived task (a stream); restart it with backoff if it crashes."""
        backoff = 1.0
        while not stop.is_set():
            try:
                await factory()
                return
            except Exception:
                log.exception("task_crashed", task=name, restart_in=backoff)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, 60.0)

    def _start_scheduler(self) -> None:
        sched = AsyncIOScheduler(timezone=ET)
        sched.add_job(self.premarket_job, CronTrigger(day_of_week="mon-fri", hour=8, minute=45,
                                                      timezone=ET),
                      id="premarket", coalesce=True, misfire_grace_time=1800)
        sched.add_job(self.prescan_job, CronTrigger(day_of_week="mon-fri", hour=9, minute=25,
                                                    timezone=ET),
                      id="prescan", coalesce=True, misfire_grace_time=600)
        sched.add_job(self.postclose_job, CronTrigger(day_of_week="mon-fri", hour=16, minute=20,
                                                      timezone=ET),
                      id="postclose", coalesce=True, misfire_grace_time=3600)
        sched.add_job(self.overnight_job, CronTrigger(day_of_week="mon-fri", hour=16, minute=15,
                                                      timezone=ET),
                      id="overnight", coalesce=True, misfire_grace_time=3600)
        sched.start()
        self._scheduler = sched

    async def run(self, stop: asyncio.Event) -> None:
        await self.boot()
        log.info("engine_running", status=self.gate.display_status(),
                 blocks=[b["reason"] for b in self.gate.to_json()])
        if self.enable_scheduler:
            self._start_scheduler()
        loops = [
            self._loop("heartbeat", self.heartbeat_interval, self.heartbeat_once, None, stop),
            self._loop("commands", self.command_poll_interval, self.process_commands_once,
                       self._wake_commands, stop),
            self._loop("settings", self.settings_poll_interval, self.check_settings_once,
                       self._wake_settings, stop),
            self._loop("market", self.market_interval, self.market_tick, None, stop),
            self._loop("watchlist", self.watchlist_interval, self.watchlist_tick,
                       self._wake_watchlist, stop),
            self._loop("scan", self.scan_check_interval, self.scan_tick, None, stop),
            self._loop("decisions", self.decision_interval, self.decision_tick, None, stop),
            self._loop("daily_loss", self.decision_interval, self.daily_loss_tick, None, stop),
            self._loop("trading", self.trading_interval, self.trading_tick, None, stop),
            self._loop("news", self.news_interval, self.news_tick, None, stop),
            self._loop("alerts", 15.0, self.alerts_tick, None, stop),
            self._listen(stop),
        ]
        if self.market is not None:
            market = self.market
            loops.append(self._supervise(
                "market_stream", lambda: market.stream.run(stop, market.on_stream_event), stop))
        if self.broker is not None and self.spool is not None:
            broker, spool = self.broker, self.spool
            loops.append(self._supervise(
                "trade_updates", lambda: broker.run_trade_updates(stop, spool.handle), stop))
        await asyncio.gather(*loops)
        await self.shutdown()

    async def shutdown(self) -> None:
        """Graceful stop. Never flattens positions (broker brackets protect them)."""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
        for task in list(self._tasks):
            task.cancel()
        if self.market is not None:
            with contextlib.suppress(Exception):
                await self.market.aclose()
        aclose = getattr(self.broker, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()
        with contextlib.suppress(SQLAlchemyError, OSError):
            async with transaction(self.sessionmaker) as s:
                await engine_repo.system_event(s, component="engine", type="ENGINE_STOPPED",
                                               severity="INFO", message="Engine stopped")
        log.info("engine_stopped")
