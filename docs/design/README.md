# Design Package — Intraday App

Status: **DRAFT — awaiting owner review.** No Phase 1 code will be written until approved.

Source documents, in precedence order:

1. `CLAUDE.md` (project rules, wins on conflict)
2. The decisions log below (owner answers from the design kickoff, 2026-09-26)
3. `docs/SPEC.md`

## Contents

| # | Document | Covers |
|---|---|---|
| 01 | [Architecture](01-architecture.md) | System context, containers, decision pipeline, process model, failure domains |
| 02 | [Technology decisions](02-technology-decisions.md) | ADR-style choices with alternatives and rationale |
| 03 | [Repository structure](03-repository-structure.md) | Directory tree, module boundaries, mapping to SPEC §30 |
| 04 | [Database schema](04-database-schema.md) | Tables, ER diagram, retention, audit chain |
| 05 | [API specification](05-api-spec.md) | REST + WebSocket endpoints, auth, errors |
| 06 | [Configuration model](06-configuration-model.md) | Env vs. runtime settings, defaults, hard bounds, apply policy |
| 07 | [Risk-engine design](07-risk-engine.md) | Checks, sizing, daily-loss, trading gate |
| 08 | [Trading-engine design](08-trading-engine.md) | Runtime loop, order manager, exits, reconciliation, EOD |
| 09 | [AI decision-engine design](09-ai-decision-engine.md) | Features, strategies, scoring, regime, AI_MODE, explanations, news |
| 10 | [UI wireframes](10-ui-wireframes.md) | 14 pages, layout, controls |
| 11 | [Docker architecture](11-docker-architecture.md) | Compose services, networks, images, backups, VM |
| 12 | [Development roadmap](12-roadmap.md) | Reordered phases, exit criteria, validation stages |

## Decisions log

### Owner answers (2026-09-26)

| ID | Question | Decision |
|---|---|---|
| D1 | ML model in V1? | **Rules now, ML later.** V1 scores with configurable weighted deterministic factors. A `Scorer` interface lets an ML scorer plug in later, trained on backtest/paper data with train/validation/OOS splits. |
| D2 | Meaning of "AI confidence" | **Score = confidence.** "Model confidence" is the normalized 0–1 trade score. `MIN_TRADE_SCORE` is the only entry gate; `MIN_AI_CONFIDENCE` is removed. The LLM veto is binary. |
| D3 | Symbol metadata | **Manual watchlist fields.** role (TRADABLE/CONTEXT), asset_type, country, sector, sector_etf, optional market_cap stored per entry, editable in UI, seeded from a file. |
| D5 | "Non-U.S. securities" (CLAUDE.md "Tradable assets") | **U.S. issuers only.** Tradable entries need `country = US`; ADRs and foreign ordinary shares (e.g. SHOP, SPOT) are excluded. |
| D4 | Roadmap order | **Reorder.** Compose from Phase 1; risk engine before backtesting; replay/SimBroker inside the backtesting phase; Phase 13 = production hardening; Phase 14 = VM deploy. |

### CLAUDE.md update: "Tradable assets" (2026-09-26)

These changes follow from the new section:

- Tradable: U.S.-issuer common stock on NYSE/Nasdaq, long only. SPEC §7's "ETFs unless explicitly enabled" toggles are removed (06 §3.9).
- SPY, QQQ, sector ETFs and the volatility proxy are `role=CONTEXT` symbols: read-only inputs to the regime detector (04, 09 §4).
- One eligibility function (07 §2a, rules E1–E7) is checked independently by the universe filter, the risk engine (**R26**) and the order manager at submit time. A DB constraint and trigger back them up (04).

### Resolved contradictions and adopted defaults (open to review)

| ID | Topic | Resolution |
|---|---|---|
| C1 | Fractional shares vs. mandatory bracket orders | Alpaca brackets don't support fractional qty → **whole shares only**. `ALLOW_FRACTIONAL_SHARES` locked to `false` (shown read-only). |
| C2 | LLM output schema (§9, §32) vs. "LLM never originates trades" | Deterministic engine builds the whole proposal. In VETO mode the LLM gets a *review* prompt and returns `{verdict, reasons, contradictory_signals}`. The LLM plays no part in exits. |
| C3 | §27 "AI fails → stop new trades" | Applies only when `AI_MODE=VETO`. |
| C4 | Emergency stop / daily-loss policy | Emergency stop default **CLOSE_ALL**; daily loss default **KEEP_STOPS** (optional FLATTEN). |
| C5 | RBAC vs. single user | Single admin; `role` column kept. |
| C6 | AWS / Celery / ALB | Replaced by single-VM Docker Compose + APScheduler in engine. |
| C7 | `MAX_OPEN_POSITIONS` vs `MAX_CONCURRENT_POSITIONS` | Merged into `MAX_OPEN_POSITIONS`. |
| C8 | `PAPER_INITIAL_BALANCE` | Starting capital for backtest/replay. In paper mode the broker account is the source of truth; the engine warns on startup if they differ. |
| G1 | Holidays / early closes | Alpaca market calendar. On early-close days, time cutoffs become offsets from actual close. |
| G2 | IEX feed limits | Every bar/metric tagged with its feed. Spread threshold is feed-specific. Missing bars = data-quality failure. |
| G3 | VIX / breadth | Proxy: SPY/QQQ realized volatility (+ optional VIXY). Breadth = % of watchlist above VWAP. |
| G4 | News | Alpaca News API; deterministic keyword event tagging; sentiment `UNKNOWN` unless `AI_MODE=VETO`. |
| G5 | Entry order type | Marketable limit (ask + offset bps), TIF DAY, bracket legs attached. |
| G6 | Early exit with live bracket | Cancel legs → confirm → market sell. Trailing stop = replace stop leg. |
| G7 | Replay mode | Local `SimBroker` shared with backtesting; live, backtest, replay share strategy + risk code. |
| G8 | API → engine commands | Durable rows in Postgres + Redis notify; engine also polls DB each loop. |
| G9 | Risk-setting edits | Hard bounds server-side; confirm dialog; audited; *loosening* a risk limit only while paused. |
| G10 | SELL in long-only system | SELL = exit only. |
| G11 | Email | Generic SMTP via env. |
| G12 | §25 phase-name clash | Called "validation stages". |
| G13 | Repo layout | One Python package with subpackages + two entrypoints; boundaries enforced with import-linter. |

### Items flagged for your attention during review

These came up while writing the design. Each has a default; none blocks review.

1. **Alpaca free-plan limits.** At the time of writing the Basic plan caps real-time streaming at ~30 symbols and REST at ~200 req/min. Watchlist + SPY/QQQ + sector ETFs must fit in the stream budget. Verified in Phase 2. *Default:* watchlist capped at 20 symbols, and the UI warns if the stream budget is exceeded.
2. **Average daily volume on IEX.** Following CLAUDE.md literally (same-feed history), ADV measured on IEX is roughly 2–3% of consolidated volume, so thresholds are in IEX units and get recalibrated on a switch to SIP. *Alternative:* take ADV from SIP *daily historical* bars, which the free plan allows for data older than 15 minutes. That would make ADV feed-independent. *Default:* same-feed, per CLAUDE.md.
3. **Latched daily-loss lock.** SPEC §6 requires a manual reset. *Default:* the lock persists across days until you reset it.
4. **Broker credentials live only in the engine container.** The API never holds Alpaca keys, so "Close position" and similar actions go through engine commands. This is intentional.
