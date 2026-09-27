"""Assembles risk-engine inputs from the database and live sources (engine only).

The risk engine itself stays pure (intraday.risk); this adapter does the I/O:
portfolio/account from the DB, a fresh quote for each proposed symbol, the
watchlist's eligibility and sector, the gate, the clock, and correlation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.ai.news import blocked_tags
from intraday.core.assets import Eligibility, RuleResult
from intraday.core.logging import get_logger
from intraday.core.runtime_config import TradingConfig
from intraday.core.timeutil import ET
from intraday.db.models import Symbol
from intraday.db.repositories import portfolio as pf_repo
from intraday.db.repositories import signals as sig_repo
from intraday.features.regime import Regime
from intraday.marketdata.calendar import MarketCalendar, entry_window
from intraday.marketdata.news import recent_news
from intraday.marketdata.types import Quote
from intraday.marketdata.watchlist import eligibility_of
from intraday.risk.correlation import CorrelationModel
from intraday.risk.correlation import build as build_correlation
from intraday.risk.models import (
    Clock,
    GateSnapshot,
    MarketSnapshot,
    PendingEntry,
    PortfolioSnapshot,
    RiskDecision,
    RiskInputs,
    RiskProposal,
)
from intraday.risk.risk_manager import safe_evaluate
from intraday.trading.pricing import price_entry

log = get_logger(__name__)

QUOTE_MAX_AGE = timedelta(seconds=60)
SUSPICIOUS_JUMP_PCT = Decimal("10")
_NOT_LISTED = Eligibility({"E1": RuleResult(False, "symbol is not on the watchlist")})
QuoteFetcher = Callable[[Sequence[str]], Awaitable[dict[str, Quote]]]


class RiskAdapter:
    def __init__(self, sm: async_sessionmaker[AsyncSession], *, feed: str,
                 quotes: QuoteFetcher | None, clock: Callable[[], datetime]) -> None:
        self.sm = sm
        self.feed = feed
        self.quotes = quotes
        self.clock = clock
        self.correlation = CorrelationModel()

    async def evaluate(self, proposals: Sequence[tuple[RiskProposal, dict[str, float | None]]],
                       *, cfg: TradingConfig, regime: Regime, gate: GateSnapshot,
                       calendar: MarketCalendar, stale_symbols: Sequence[str]
                       ) -> list[RiskDecision]:
        """Evaluate proposals in order; each approved one counts as pending for the next."""
        if not proposals:
            return []
        now = self.clock()
        today = now.astimezone(ET).date()
        session = calendar.session(today)
        clock = Clock(now, calendar.is_open(now),
                      entry_window(session, cfg.hours) if session else None)
        symbols = [p.symbol for p, _ in proposals]

        quotes: dict[str, Quote] = {}
        if self.quotes is not None:
            try:
                quotes = await self.quotes(symbols)
            except Exception as exc:  # any quote failure → R7 fails (no fresh quote)
                log.warning("risk_quotes_failed", error=str(exc))

        async with self.sm() as s:
            news = (await recent_news(s, symbols, now - timedelta(
                hours=cfg.news.lookback_hours)) if cfg.news.enabled else [])
            portfolio = await pf_repo.load_portfolio(s, today, self.feed)
            account = await pf_repo.load_account(s, cfg.risk.paper_initial_balance)
            rows = {r.symbol: r for r in (await s.execute(
                select(Symbol).where(Symbol.symbol.in_(symbols)))).scalars()}

        decisions: list[RiskDecision] = []
        for raw, feats in proposals:
            proposal = _priced(raw, quotes.get(raw.symbol), now,
                               cfg.execution.entry_limit_offset_bps)
            row = rows.get(proposal.symbol)
            market = replace(self._market(proposal, feats, row, quotes.get(proposal.symbol),
                                          now, stale_symbols),
                             blocked_news_tags=blocked_tags(news, proposal.symbol,
                                                            cfg.news.block_event_tags))
            exposures = {p.symbol: p.value for p in portfolio.positions}
            for pend in portfolio.pending:
                exposures[pend.symbol] = exposures.get(pend.symbol, Decimal(0)) + pend.value
            corr = self.correlation.cluster_exposure(proposal.symbol, exposures,
                                                     cfg.risk.correlation_threshold)
            inputs = RiskInputs(proposal, portfolio, account, market, gate, clock, regime,
                                self.feed, corr)
            decision = replace(safe_evaluate(inputs, cfg), proposal=proposal)
            decisions.append(decision)
            if decision.approved:
                portfolio = _with_pending(portfolio, proposal, decision.qty, market.sector)
        return decisions

    async def refresh_correlation(self, symbols: Sequence[str], calendar: MarketCalendar,
                                  lookback_days: int) -> None:
        """5-minute closes over the lookback sessions → correlation model (nightly/boot)."""
        now = self.clock()
        sessions = calendar.last_n_sessions(lookback_days, now - timedelta(days=1))
        if not sessions or not symbols:
            self.correlation = CorrelationModel()
            return
        async with self.sm() as s:
            rows = await sig_repo.minute_bars(s, self.feed, symbols, sessions[0].open_at,
                                              sessions[-1].close_at)
            sector = await pf_repo.sectors(s, symbols)
        buckets: dict[str, dict[datetime, float]] = {sym: {} for sym in symbols}
        for r in rows:
            b = r.ts.replace(minute=r.ts.minute - r.ts.minute % 5, second=0, microsecond=0)
            buckets[r.symbol][b] = float(r.close)   # rows are ts-ordered: last close wins
        grid = sorted({b for m in buckets.values() for b in m})
        closes: dict[str, list[float]] = {}
        for sym, m in buckets.items():
            series, last = [], None
            for b in grid:
                last = m.get(b, last)
                if last is not None:
                    series.append(last)
            closes[sym] = series if len(m) else []
        expected = len(sessions) * 78
        present = {sym: len(m) for sym, m in buckets.items()}
        self.correlation = build_correlation(
            {s: c for s, c in closes.items()}, expected, sector, coverage=present)
        log.info("correlation_refreshed", symbols=len(symbols), sessions=len(sessions),
                 insufficient=sorted(self.correlation.insufficient))

    def _market(self, p: RiskProposal, feats: dict[str, float | None], row: Symbol | None,
                quote: Quote | None, now: datetime, stale: Sequence[str]) -> MarketSnapshot:
        problems: list[str] = []
        if p.symbol in stale:
            problems.append("market data for the symbol is stale")
        prev = feats.get("prev_close")
        price = feats.get("price")
        if prev and price and abs(price - prev) / prev * 100 >= float(SUSPICIOUS_JUMP_PCT):
            problems.append(f"price moved {abs(price - prev) / prev * 100:.1f}% vs previous close")
        if row is None:
            problems.append("symbol is not on the watchlist")
        spread = None
        if quote is not None and now - quote.ts <= QUOTE_MAX_AGE:
            spread = quote.spread_pct
        elig = eligibility_of(row, now) if row is not None else _NOT_LISTED
        return MarketSnapshot(p.symbol, Decimal(str(price if price is not None else p.entry)),
                              spread, tuple(problems), elig, row.sector if row else None)


def _priced(p: RiskProposal, quote: Quote | None, now: datetime, offset_bps: int
            ) -> RiskProposal:
    """The marketable limit actually submitted, with stop/target moved with it (08 §6)."""
    ask = quote.ask if quote is not None and now - quote.ts <= QUOTE_MAX_AGE else None
    px = price_entry(p.entry, p.stop, p.target, ask, offset_bps)
    return replace(p, entry=px.limit, stop=px.stop, target=px.target)


def _with_pending(pf: PortfolioSnapshot, p: RiskProposal, qty: int, sector: str | None
                  ) -> PortfolioSnapshot:
    counts = dict(pf.symbol_trades_today)
    counts[p.symbol] = counts.get(p.symbol, 0) + 1
    return replace(pf, pending=(*pf.pending, PendingEntry(p.symbol, qty, p.entry, p.stop, sector)),
                   trades_today=pf.trades_today + 1, symbol_trades_today=counts)
