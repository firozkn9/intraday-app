# 05 — API Specification

Base path: `/api/v1`. JSON everywhere except CSV export. FastAPI generates the OpenAPI document at `/api/v1/openapi.json` (authenticated), and the frontend's typed client is generated from it.

## 1. Cross-cutting rules

| Concern | Rule |
|---|---|
| Auth | Session cookie `sid` (`HttpOnly; Secure; SameSite=Strict; Path=/`), 12 h idle / 7 d absolute expiry. Every endpoint except `/auth/login`, `/auth/csrf` and `/health*` requires it. |
| CSRF | Double-submit: `GET /auth/csrf` sets a readable `csrf` cookie. Unsafe methods must send `X-CSRF-Token` matching the session-bound token. |
| Roles | `admin` required for all mutations (single user today; checked anyway). |
| Rate limits | Login: 5/min/IP and 20/h/account, lockout 15 min after 10 failures. Mutations: 30/min. Reads: 300/min. Returns `429` with `Retry-After`. |
| Errors | RFC 7807 `application/problem+json`: `{type, title, status, detail, instance, errors?}`. |
| Pagination | Cursor based: `?limit=50&cursor=…` → `{items, next_cursor}`. |
| Time | ISO-8601 UTC in payloads. Query params accept dates as `YYYY-MM-DD` (ET session dates). |
| Money | Decimal strings (`"123.45"`) so JS floats never round them. |
| Audit | Every mutating request writes `audit_log` with before/after. |
| Dangerous controls | Require a `confirm` field equal to a phrase shown in the UI (see §4). `RESET_*` also requires `password` re-entry. |

## 2. Auth

| Method | Path | Body / Query | Response |
|---|---|---|---|
| GET | `/auth/csrf` | — | `204`, sets `csrf` cookie |
| POST | `/auth/login` | `{email, password}` | `200 {user}`, sets `sid`; `401` generic message |
| POST | `/auth/logout` | — | `204` |
| GET | `/auth/me` | — | `{id, email, role}` |
| POST | `/auth/change-password` | `{current_password, new_password}` | `204`; revokes other sessions |

## 3. Status, account, dashboard

| Method | Path | Response (abridged) |
|---|---|---|
| GET | `/status` | `{trading_mode:"PAPER", status, blocks:[{reason, since, latched}], trading_enabled, engine_heartbeat_age_s, market:{is_open, next_open, next_close, early_close}, config_version, ai_mode, data_feed}` |
| GET | `/account` | Latest `account_snapshots` row + `as_of` |
| GET | `/dashboard/summary` | `{account, today:{pnl, realized, unrealized, trades, wins, losses, win_rate, daily_loss_limit, daily_loss_used}, total_pnl, open_positions_count, status}` |
| GET | `/dashboard/equity-intraday?date=` | Series from `account_snapshots` |

## 4. Controls (engine commands)

All return `202 {command_id, status:"PENDING"}`. Poll `GET /commands/{id}` or watch the WebSocket `command` channel.

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/controls/start` | `{}` | Sets `trading_enabled=true`. Refused (`409`) while a latch is active. |
| POST | `/controls/pause` | `{}` | Blocks new entries; positions managed normally. |
| POST | `/controls/emergency-stop` | `{confirm:"EMERGENCY STOP", close_positions?: bool}` | Default `close_positions` comes from `EMERGENCY_STOP_ACTION` (CLOSE_ALL). Latches. |
| POST | `/controls/close-all` | `{confirm:"CLOSE ALL"}` | Cancels open orders, flattens. Does not latch; trading stays paused until START. |
| POST | `/positions/{id}/close` | `{confirm:"<SYMBOL>"}` | Manual close (exit_reason `MANUAL`). |
| POST | `/controls/reset-daily-lock` | `{confirm:"RESET", password}` | Clears `DAILY_LOSS_LIMIT` latch. Trading remains paused until START. |
| POST | `/controls/reset-latch` | `{latch, confirm:"RESET", password}` | For `EMERGENCY_STOP`, `RECONCILE_MISMATCH`, `RISK_ENGINE_ERROR`. `409` if the underlying condition persists (e.g. mismatch still present). |
| POST | `/controls/reconcile-now` | `{}` | |
| GET | `/commands/{id}` | — | Command row |
| GET | `/commands?limit=` | — | Recent commands |

There is intentionally **no** endpoint that disables a risk check, stop-loss requirement or paper mode.

## 5. Positions, trades, calendar

| Method | Path | Query / Response |
|---|---|---|
| GET | `/positions` | Open positions with live marks: `{symbol, qty, avg_entry, current, unrealized_pnl, pnl_pct, stop, target, confidence, strategy, holding_seconds, trailing_active}` |
| GET | `/positions/{id}` | Full position incl. orders, events |
| GET | `/trades` | Closed round trips. Filters: `date_from, date_to, symbol, strategy, outcome=win|loss, pnl_min, pnl_max, confidence_min, confidence_max, entry_after, entry_before (HH:MM ET), exit_after, exit_before, exit_reason`; `sort`; paginated |
| GET | `/trades/export.csv` | Same filters, streamed CSV |
| GET | `/trades/{id}` | **Trade detail / audit chain**: `{position, entry:{signal, features, conditions, factor_scores, decision, llm_call?, risk_evaluation}, orders:[{…, events, fills}], exit:{reason, detail, signal?}, explanation:{entry_text, exit_text, source:"TEMPLATE"|"LLM"}, bars:[…] (session bars for chart)}` |
| GET | `/calendar?month=2026-09` | `{month, days:[{date, status:"PROFIT"|"LOSS"|"FLAT"|"NO_TRADING"|"HOLIDAY", realized_net, trades, wins, losses, win_rate, largest_win, largest_loss, shares_traded, notional_traded}] , totals}` |
| GET | `/calendar/{date}` | `{summary (daily_pnl row), trades:[{id, symbol, entry_time, exit_time, side, qty, net_pnl, return_pct, strategy}]}` |

## 6. Decisions and scanner

| Method | Path | Notes |
|---|---|---|
| GET | `/decisions` | `ai_decisions` list. Filters: date range, symbol, strategy, `result`, `ai_mode`, confidence range |
| GET | `/decisions/{id}` | Decision + signal + risk evaluation + llm call |
| GET | `/signals?symbol=&date=` | Raw `trading_signals` (incl. NO_TRADE) for debugging |
| GET | `/scanner` | Latest universe scan: per symbol pass/fail with every filter's reason (U1–U9) and metrics (price, change %, ADV, spread). RVOL/VWAP/scores join in Phase 4. |
| GET | `/scanner/history?symbol=&limit=` | Past scan results for one symbol *(Phase 3)* |
| POST | `/scanner/run` | → `202 {command_id}`; the engine runs a scan (it holds the keys for quotes) *(Phase 3)* |
| GET | `/regime` | Current regime + inputs, last 60 min history |
| GET | `/news?symbol=&since=` | News items with tags |
| GET | `/market/calendar?start=&end=` | Sessions (UTC open/close, early close) + current session status *(Phase 2)* |
| GET | `/market/bars?symbol=&start=&end=&feed=` | Stored regular-session 1-min bars, ≤ 10 days per request *(Phase 2)* |
| POST | `/market/backfill` | `{days ≤ 60, full}` → `202 {command_id}`; runs in the engine (it holds the keys) *(Phase 2)* |

## 7. Watchlist, settings, strategies

| Method | Path | Notes |
|---|---|---|
| GET | `/watchlist` | Symbols with metadata, cached Alpaca asset data, eligibility (E1–E7), and `stream_budget {used, limit, subscribed, dropped}` |
| POST | `/watchlist` | `{symbol, role, asset_type, country, sector, sector_etf, market_cap?, notes?}`. Validated against the Alpaca asset cache. A `TRADABLE` entry that fails eligibility (07 §2a) returns `422` with the failing rules. The API does not accept a `TRADABLE` entry unless it's `COMMON_STOCK`, `US`, and on NYSE/Nasdaq. Warns if the stream budget is exceeded. |
| PATCH | `/watchlist/{symbol}` | Edit metadata / enable |
| DELETE | `/watchlist/{symbol}` | `409` if an open position exists |
| GET | `/settings` | `{version, config, schema}`. The schema includes type, default, hard bounds, apply policy and description per field so the UI renders forms generically. |
| POST | `/settings/validate` | Dry-run: returns field errors and a diff with apply-policy per change |
| PUT | `/settings` | `{base_version, config, comment, confirm?}`. `409` if `base_version` isn't active (optimistic concurrency). `423 Locked` if a change *loosens* a risk limit while the engine isn't paused (G9). Creates new version. |
| GET | `/settings/history` | Versions with author, comment, diff to previous |
| POST | `/settings/rollback` | `{version, comment}` → creates a new version equal to an old one (same rules as PUT) |
| GET | `/settings/export` | YAML download (same shape as `config/defaults.yaml`) |
| POST | `/settings/import` | YAML upload → validate → same rules as PUT |
| GET | `/strategies` | Per-strategy config + today's and 30-day stats. This is a view onto `settings.strategies` plus `strategy_performance`. Edits go through `PUT /settings`. |

## 8. Performance and backtests

| Method | Path | Notes |
|---|---|---|
| GET | `/performance/summary?from=&to=` | Win rate, profit factor, avg trade/win/loss, max drawdown, Sharpe (daily returns), trades, avg holding time, cumulative P&L |
| GET | `/performance/pnl?period=day\|week\|month&from=&to=` | Bucketed P&L |
| GET | `/performance/equity-curve?from=&to=` | |
| GET | `/performance/breakdown?by=strategy\|symbol\|hour\|confidence_bucket\|exit_reason` | |
| POST | `/backtests` | `{symbols?, train:{from,to}, validation:{from,to}, test:{from,to}, starting_capital, strategies, config_overrides, costs:{commission_per_share, sec_fee, slippage_bps}}` → `202 {id}`. At most one run at a time (`409`). Requires data already in `bars_1m`; missing ranges return `422` listing gaps, and the UI offers a backfill command. |
| GET | `/backtests` / `/backtests/{id}` | Status + metrics per split |
| GET | `/backtests/{id}/trades?split=` / `/backtests/{id}/equity` | |
| POST | `/backtests/{id}/cancel` | |

## 9. Health, logs, alerts

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/health` | none | Liveness: `{status:"ok"}` |
| GET | `/health/ready` | none | DB + Redis reachable (used by Docker healthcheck) |
| GET | `/system/health` | yes | Per component: engine heartbeat age, broker REST/WS state, market data freshness per symbol, DB latency, Redis, AI provider (last latency, failure streak), order latency p50/p95, rejection rate, CPU/mem per process, disk free, last backup time |
| GET | `/metrics` | internal network only (Caddy doesn't proxy it) | Prometheus text |
| GET | `/logs?component=&severity=&from=&to=&q=` | yes | `system_events` + `risk_events` merged view |
| GET | `/audit?from=&to=&action=` | yes | `audit_log` |
| GET | `/alerts?state=open\|all` | yes | |
| POST | `/alerts/{id}/ack` | yes | |

## 10. WebSocket `/ws`

- Authenticated by the session cookie during the handshake. `Origin` must match the site origin. The socket closes with 4401 when the session expires.
- The server pushes `{channel, type, ts, data}`. The client can send `{"subscribe": ["positions", …]}`. Default is all channels.

| Channel | Events |
|---|---|
| `status` | `status.changed` (status, blocks) |
| `account` | `account.snapshot` (every 60 s + on fill) |
| `positions` | `position.opened`, `position.updated` (mark, stop move), `position.closed` |
| `orders` | `order.updated` |
| `decisions` | `decision.created` (with result) |
| `alerts` | `alert.raised`, `alert.acked` |
| `commands` | `command.updated` |
| `scanner` | `scanner.updated` (per bar) |

Source: the engine publishes to Redis `events:<channel>`, and the API subscribes and fans out. Marks are throttled to 1/s per position.

## 11. Engine ↔ API contract (internal, not HTTP)

| Direction | Mechanism |
|---|---|
| API → engine | Insert `engine_commands` row, then `PUBLISH commands:new <id>`. The engine also polls `PENDING` rows every 1 s. |
| Engine → API | Postgres tables (durable) + Redis `events:*` (real-time) + `engine_state` row + Redis key `engine:heartbeat` (TTL 15 s). |
| Settings | API writes `settings_versions`, then `PUBLISH settings:changed <version>`. The engine loads and validates it, applies it according to apply policy (06 §4), and writes the version it is now running into `engine_state.config_version`. |

## As built (Phases 8–12)

- Implemented: every endpoint in §3–§9, plus `GET /positions?scope=open|today`. `/trades` is paginated with `page` and `page_size`.
- **WebSocket `/ws` is not built in V1.** The UI polls every 5–60 s through TanStack Query. Caddy no longer proxies `/ws`.
- `/metrics` is served at `/api/v1/metrics`, and Caddy returns 404 for it.
