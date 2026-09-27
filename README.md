# Intraday App

An autonomous intraday stock-trading system that runs **only in paper-trading mode**
against the Alpaca paper API. A deterministic scoring engine proposes trades, an
optional LLM may veto them, and a deterministic risk engine has final authority.

> **PAPER TRADING — NO REAL MONEY.** Trading involves substantial risk. This system
> does not guarantee profits or prevent losses. Paper-trading results may differ
> materially from live trading.

**Status: Phases 1–15 built.** The full pipeline runs against the Alpaca **paper** API:
market data, scanner, signals, risk engine, backtesting, the paper broker, order and
position management with reconciliation and end-of-day liquidation, the optional AI
layer, the dashboard, calendar and performance pages, monitoring and alerts, production
hardening, and VM deployment scripts. The steps that need you (paper checks during
market hours, the VM) are listed in [docs/deploy.md](docs/deploy.md). See the
[roadmap](docs/design/12-roadmap.md).

| Document | Purpose |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Project rules. These override the spec. |
| [docs/SPEC.md](docs/SPEC.md) | Original specification |
| [docs/design/](docs/design/README.md) | Design package: architecture, schema, API, risk, roadmap, decisions log |
| [docs/deploy.md](docs/deploy.md) | VM deployment, backups, go-live checklist |
| [docs/runbooks.md](docs/runbooks.md) | Operations and incident runbooks |
| [docs/security-review.md](docs/security-review.md) | Phase 13 security review |

## Architecture

Five Docker Compose services on one VM ([details](docs/design/01-architecture.md)):

| Service | Role |
|---|---|
| `caddy` | HTTPS; serves the built React app; proxies `/api` and `/ws` |
| `api` | FastAPI: auth, settings, status, commands. **Holds no broker credentials.** |
| `engine` | Trading runtime; the only container with Alpaca/Anthropic keys and internet egress |
| `postgres` | System of record, with separate least-privilege roles for api and engine |
| `redis` | Notifications, rate limits, heartbeats. Optional: everything falls back to DB polling. |

## Prerequisites

- **Docker** with Compose v2 (Docker Desktop on Windows/macOS, Docker Engine on Linux)
- For development without Docker: **Python 3.12** with [uv](https://docs.astral.sh/uv/),
  and **Node 22+**

## Configuration

There are three layers ([details](docs/design/06-configuration-model.md)):

1. **Hard limits**, in code (`backend/src/intraday/core/hard_limits.py`). No setting can exceed them.
2. **Environment** (`.env`): secrets, the safety switches, and infrastructure.
   `cp .env.example .env` and replace every `CHANGE_ME`. Never commit `.env`.
   For local runs outside Docker, put overrides (e.g. `localhost` database URLs)
   in `.env.local`. Precedence: real environment variables > `.env.local` > `.env`.
   Both files are git-ignored, and the app refuses placeholder secrets.
3. **Runtime settings**: seeded from `config/defaults.yaml` into the database as
   version 1. After that they're edited in the **Settings** page, where every save
   creates a new audited version. Loosening a risk limit is refused while trading is
   enabled; pause trading first.

Safety switches the engine checks at boot:

```
TRADING_MODE=PAPER
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

If either value is anything else, the engine refuses to trade and reports
`CONFIG_ERROR` in the UI.

## Alpaca paper account setup

1. Create an account at alpaca.markets and switch the dashboard to **Paper Trading**.
2. Generate **paper** API keys and put them in `.env` (`ALPACA_API_KEY_ID`,
   `ALPACA_API_SECRET_KEY`).
3. The paper balance is set in Alpaca's dashboard. `PAPER_INITIAL_BALANCE` is only
   the starting capital for backtests and replay.

(Broker integration arrives in Phase 7. In Phase 1 the keys aren't used yet.)

## Running locally with Docker

```bash
cp .env.example .env                      # then edit values
docker compose -f compose.yaml -f compose.dev.yaml up -d --build postgres redis
docker compose -f compose.yaml -f compose.dev.yaml --profile ops run --rm migrate
docker compose -f compose.yaml -f compose.dev.yaml up -d --build api engine
docker compose -f compose.yaml -f compose.dev.yaml exec api intraday create-admin
cd frontend && npm install && npm run dev  # http://localhost:5173
```

In dev, source code is bind-mounted and reloads automatically. Postgres (5432), Redis
(6379) and the API (8000) are published on `127.0.0.1` only.

## Running locally without Docker

```bash
cd backend
uv sync
export DATABASE_URL=postgresql+asyncpg://USER:PASS@localhost:5432/intraday   # any Postgres 16
export DATABASE_URL_OWNER=$DATABASE_URL
export TRADING_MODE=PAPER ALPACA_BASE_URL=https://paper-api.alpaca.markets
export SESSION_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
export PUBLIC_ORIGIN=http://localhost:5173 COOKIE_SECURE=false
uv run intraday db-upgrade
uv run intraday create-admin
uv run uvicorn intraday.api.main:app --port 8000     # terminal 1
uv run python -m intraday.engine.main                # terminal 2
cd ../frontend && npm install && npm run dev         # terminal 3 → http://localhost:5173
```

## Market data

- **Feed:** Alpaca IEX, the free plan (setting `data.feed`; switching to `sip` takes effect on
  engine restart). Free-plan limits: 30 streamed symbols and 200 API calls/min. They're
  configurable via `ALPACA_STREAM_SYMBOL_LIMIT` and `ALPACA_*_RATE_LIMIT`.
- **Watchlist:** seeded from `config/watchlist.seed.yaml` on the API's first start. It holds
  17 U.S. stocks, plus SPY/QQQ/VIXY and sector ETFs as read-only context. Only U.S.-issuer
  common stock on NYSE/Nasdaq can be tradable, and each symbol's eligibility (rules E1–E7)
  is shown per symbol.
- **Stream budget:** SPY and QQQ first, then the other core context symbols, then sector
  ETFs that a tradable symbol references, then tradable symbols, oldest first. If the
  budget overflows, the newest additions are the ones not streamed, and the API warns.
- **Bars:** only regular-session 1-minute bars are stored, plus daily bars. After every
  stream (re)connect the engine backfills whatever it missed. Scheduled jobs run at
  08:45 ET (calendar, assets, backfill) and 16:20 ET (re-fetch of the last 2 sessions).
- **Commands** (run where the Alpaca keys are, i.e. the engine container):
  `intraday backfill --days 30`, `intraday seed-watchlist`. `POST /api/v1/market/backfill`
  queues a backfill in the engine.
- **Without keys** the engine runs but reports `DATA_STALE` ("Market data disabled").

## Universe scanner

Every tradable watchlist symbol is checked against nine filters. Each result is
recorded with its reason and shown on the **Market Scanner** page:

| ID | Filter | Rule (defaults) |
|---|---|---|
| U1 | Enabled | on the watchlist and enabled |
| U2 | Eligible | U.S.-issuer common stock on NYSE/Nasdaq (rules E1–E7) |
| U3 | Price | ≥ `universe.min_stock_price` ($10; never below the $5 hard floor) |
| U4 | Avg daily volume | ≥ `universe.min_average_daily_volume` over 20 sessions, **in the configured feed's units**; needs ≥ 16 sessions of history |
| U5 | Market cap | ≥ $2B, only if entered on the watchlist |
| U6 | Spread | fresh quote with spread ≤ 0.30% (IEX) / 0.10% (SIP) during the session |
| U7 | Bar coverage | ≥ 80% of the last 30 minutes have a bar |
| U8 | Data quality | consistent OHLC; no ≥ 10% jump vs previous close |
| U9 | Trading activity | a bar within the last 5 minutes (else possibly halted) |

Checks that can't be evaluated off-hours (spread, coverage, activity) show as
"not evaluated" and are re-checked at order time. Scans run at engine start, 09:25 ET,
every `universe.scan_interval_minutes` during the session, and on demand
(`POST /api/v1/scanner/run` or the page's button).

**Calibrating the volume threshold:** IEX volume is only about 2–3% of consolidated
volume. After a backfill, run `intraday adv-report` to see each symbol's IEX ADV, then
set `universe.min_average_daily_volume` in Settings.

## Signals and proposals (Phase 4)

Each completed minute during the session, the engine evaluates every symbol that passed
the latest scan and printed a bar that minute:

1. **Features**: VWAP, EMA 9/20/50, RSI, MACD, ATR, ROC, relative volume against the
   same feed's 20-session time-of-day profile, and price-action flags.
2. **Regime** from SPY/QQQ and breadth: BULLISH, BEARISH, SIDEWAYS, HIGH/LOW
   volatility, or UNKNOWN. Longs are disabled in BEARISH and UNKNOWN regimes by default.
3. **Strategies**: Momentum Breakout, VWAP Reclaim, Pullback. Every hard condition is
   recorded with its value.
4. **Score**: weighted factors 0–1 ("model confidence"; a score, not a probability).
   At or above `scoring.min_trade_score` (0.70) a candidate becomes a **proposal** with
   entry, stop, target and a template explanation.

Everything is written to `trading_signals` and `ai_decisions`, and every proposal goes
through the risk engine (`risk_evaluations`, below). **No proposal is traded yet:** the
order manager arrives in Phase 8, so each proposal ends as `BLOCKED` (only the gate
stopped it) or `RISK_REJECTED` (with the failed check IDs).

Replay a stored day (dry run, writes nothing): `intraday signals --date 2026-09-21`
(add `--all` to list non-trades too). API: `GET /api/v1/signals`, `/decisions`,
`/decisions/{id}`, `/regime`.

## Risk engine (Phase 5)

The deterministic risk engine has final authority on every entry. It runs **all 26
checks** and records each one: gate, paper mode, hours, long-only, price, data quality,
spread, stop, target, reward/risk, trade counts, cooldown, open positions, size,
exposure, loss per trade, worst-case daily loss, buying power, sector and correlated
exposure, regime, news, duplicate entry, AI veto, and asset eligibility.

- **Sizing** is the minimum of seven bounds: risk, capital, exposure, buying power,
  daily-loss headroom, sector and correlation. Every bound is recorded along with the
  binding one. Shares are whole; Decimal math throughout.
- **Worst-case daily loss:** a new trade must fit in `max_daily_loss` minus the loss
  already taken minus what every open stop could still lose.
- **Latches:** `DAILY_LOSS_LIMIT` when realized plus unrealized loss reaches the limit,
  and `RISK_ENGINE_ERROR` on any exception, which fails closed. Reset with
  `POST /api/v1/controls/reset-daily-lock` or `/reset-latch`, which need `"confirm":
  "RESET"` and your password. After a reset, trading stays paused until START.

## Backtesting and replay (Phase 6)

Backtests replay stored bars (same feed as live) through the **same** signal pipeline
and risk engine, with a conservative fill model:
- entries fill on the next bar at their limit or better;
- a stop fills at the worse of the stop and the open;
- if one bar touches both stop and target, the stop counts first;
- fees (SEC/TAF on sells) and slippage are included.

Train, validation and test splits each run independently. The metrics follow SPEC §24:
return, annualized return, drawdown, Sharpe, Sortino, win rate, profit factor,
averages, streaks and holding time.

```bash
intraday backtest --train 2026-06-01:2026-07-31 --validation 2026-08-01:2026-08-31                   --test 2026-09-01:2026-09-25 [--symbol AAPL] [--persist]
intraday replay --date 2026-09-21         # one session, every event printed
```

The **Backtesting** page and `POST /api/v1/backtests` run the same thing in a background
worker process, one run at a time. Assumptions: historical spreads are taken as 0.05%,
correlation clusters are not modelled, and the daily-loss latch resets each session.
Past results do not predict future results.

## Trading: orders, positions, exits (Phases 7–8)

- **Broker:** `AlpacaPaperBroker` (REST and the binary trade-update stream). It is
  guarded three times (engine boot, factory, constructor). At boot the engine configures
  the account as long only, whole shares, no margin, and verifies the result.
- **Entries:** approved proposals become **bracket orders**: a marketable limit (ask +
  `execution.entry_limit_offset_bps`, never above the signal close plus the offset) with
  a take-profit leg and a stop leg. The risk engine sizes exactly these prices.
  Unfilled entries are cancelled after `execution.entry_timeout_seconds`.
- **Idempotency:** client order IDs look like `ia-YYYYMMDD-EN-xxxxxxxx-0`. Each order
  is written to the database before it's sent. After a timeout the engine looks the
  order up by that ID before retrying with the same ID. If it's still unknown, the order
  is marked `UNKNOWN`, `BROKER_DOWN` blocks entries, and the reconciler settles it.
- **Exits:** stop and target sit at the broker. The engine only exits earlier (time,
  momentum reversal, VWAP loss, regime reversal) or raises the trailing stop. An early
  exit cancels the legs, confirms, then sells `min(local qty, broker qty)` at market.
  An exit that doesn't fill in time raises a HIGH alert and the protective stop is
  re-placed.
- **Reconciliation** runs at boot (entries wait for a clean pass), every 30 s and after
  stream reconnects. Mismatches latch `RECONCILE_MISMATCH`, and any open position
  without a live stop gets one.
- **End of day:** at `force_close` (15:55 ET, or earlier on half days) the engine
  cancels every order and sells every broker position. It verifies flat, sends a HIGH
  alert if not flat 3 minutes later and a CRITICAL one 30 s before the close. At 16:15
  it checks for overnight positions.
- **Controls:** EMERGENCY STOP (latches; closes all by default), CLOSE ALL, per-position
  Close, and Reconcile now. Each dangerous action needs a typed confirmation.
- `ENGINE_BROKER=sim` (SimBroker) is allowed only together with `ENGINE_DATA=replay`.

## AI layer (Phase 9)

`ai.mode` OFF (default), EXPLAIN or VETO, with `ai.model` and `ANTHROPIC_API_KEY`.
- **VETO** reviews each proposal before the risk engine. It can only reject. A failure
  counts as a rejection, and repeated failures set `AI_UNAVAILABLE`.
- **EXPLAIN** rephrases the template explanation. The number guard throws the text away
  if any number or direction disagrees with the facts. It never changes a decision.
- Requests carry no account data (a whitelist DTO). Every call is stored in `llm_calls`.
- News arrives from Alpaca News every 2 minutes with deterministic event tags. Tags in
  `news.block_event_tags` block the symbol (R23). Sentiment stays UNKNOWN (neutral)
  unless VETO mode classifies it.

## Dashboard, reports, monitoring (Phases 10–12)

Pages: Dashboard (account, today's P&L, positions, intraday equity), Calendar (monthly
P&L, day drill-down), Positions, Trade History (filters, CSV export, full audit chain per
trade), AI Decisions, Strategies, Risk, Performance (summary, weekly P&L, breakdowns),
System Health (components, alerts with acknowledge) and Logs. The UI polls the API
every 5–60 s; there is no WebSocket in V1. Alerts are stored for the dashboard and
emailed via SMTP (`SMTP_*`, `ALERT_EMAIL_TO`) at or above `alerts.email_min_severity`.
`/api/v1/metrics` serves Prometheus text on the internal network only.

## Replay and end-to-end tests (Phase 15)

```bash
# Full trading stack over a stored session, writing to a SIMULATION database only:
DATABASE_URL=postgresql+asyncpg://.../intraday_sim intraday demo-seed      # synthetic day
DATABASE_URL=postgresql+asyncpg://.../intraday_sim intraday replay --date 2026-09-21 --engine

cd frontend && npx playwright install chromium && npm run e2e   # starts API, engine, SPA
```

The Playwright suite starts its own stack: a fresh `intraday_e2e` database, the
SimBroker, a replayed synthetic session, and no API keys. It covers login and the
daily-loss lock and reset, START/PAUSE, a blocked settings loosening, calendar → day →
trade detail, CSV export and emergency stop. `tests/integration/test_failsafe.py`
injects failures from SPEC §27: engine restart mid-session, broker REST and stream
loss, a database outage (spooled updates), AI unavailable, and positions found only
at the broker. Set `REDIS_URL=none` to run without Redis.

## Database setup

- Migrations live in `backend/migrations` and run as the schema owner:
  `intraday db-upgrade` (or the `migrate` compose service).
- On first start, `infrastructure/postgres/init/01-roles.sh` creates the login roles
  `intraday_api` and `intraday_engine`. Migration `0002` grants them least privilege:
  audit tables are insert-only for the engine, and the API can't write orders,
  positions or fills.
- A database trigger refuses any buy order for a symbol that isn't a tradable U.S.
  common stock (CLAUDE.md "Tradable assets").

## Running tests

```bash
cd backend
uv run pytest            # unit, property, and integration tests (real PostgreSQL)
uv run ruff check src tests migrations
uv run mypy src
uv run lint-imports      # architecture rules, e.g. "AI code cannot import the broker"

cd ../frontend
npm test && npm run typecheck && npm run build

python infrastructure/scripts/check_secrets.py   # from the repo root
```

Integration tests need PostgreSQL. With `TEST_DATABASE_URL` set (for example
`docker compose -f compose.test.yaml up -d`, then
`TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres`) they use
that server. Otherwise they start an embedded PostgreSQL via `pgserver`. No test calls
Alpaca or Anthropic.

## Starting and stopping trading

- **START / PAUSE** are in the status bar. They're sent to the engine as audited
  commands. After any restart or reset the engine comes up **paused**.
- Entries open only when nothing blocks them: broker connected and reconciled, fresh
  data, inside the entry window, no latch, and (in VETO mode) the AI available.
- **EMERGENCY STOP**, **CLOSE ALL** and per-position **Close** are in the status bar
  and on the Positions page. Latches are reset with your password, then START.

## Backtests, AWS/VM deployment, troubleshooting

Deployment: [docs/deploy.md](docs/deploy.md) (a single Ubuntu VM running Docker
Compose). Operations: [docs/runbooks.md](docs/runbooks.md). Backups:
`infrastructure/scripts/backup.sh` and `restore.sh --test`, run nightly by a systemd
timer.

| Symptom | Check |
|---|---|
| Status shows `ENGINE OFFLINE` | Is the engine container running? Run `docker compose logs engine`. |
| Status shows `EMERGENCY STOP` with `CONFIG_ERROR` | Check `TRADING_MODE` and `ALPACA_BASE_URL`. The dashboard lists the exact problem. |
| Settings save returns *Pause trading…* | Loosening a risk limit needs PAUSE first (by design). |
| Login returns 429 | Rate limit (5/min/IP) or lockout after 10 failures (15 min). |

## Security

- Single admin account with argon2id password hashing and server-side sessions
  (`HttpOnly`, `Secure`, `SameSite=Strict` cookies). Unsafe requests need a CSRF token
  and a matching origin. Login and mutations are rate-limited, and every mutation is
  written to `audit_log`.
- Secrets live only in `.env`. Alpaca and Anthropic keys are passed to the engine
  container alone, so the browser and the API never see them.
- CI runs `check_secrets.py` on every push.

## Risk limitations

This is experimental software for paper trading. Backtests and paper results are not
indicative of live results: fills, slippage and liquidity differ. There is no live
trading path. `FutureLiveBroker` raises `NotImplementedError`, and any future live
trading would be a separate project with its own safety review.
