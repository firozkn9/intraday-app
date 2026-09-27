# 02 — Technology Decisions

Format: decision, choice, alternatives considered, and why. Versions are minimum targets and get pinned in lockfiles in Phase 1.

## Backend and engine

| # | Decision | Choice | Alternatives | Rationale |
|---|---|---|---|---|
| T1 | Language | **Python 3.12** | 3.13 | Mature wheel support for numpy/pandas/asyncpg. 3.13 brings nothing we need. |
| T2 | Dependency mgmt | **uv** + `pyproject.toml` + `uv.lock` | Poetry, pip-tools | Fast, reproducible, good Docker story. |
| T3 | Web framework | **FastAPI** + Uvicorn | Litestar, Django | Required by spec. Native WebSockets and OpenAPI. |
| T4 | Validation / settings | **Pydantic v2** + pydantic-settings | attrs, marshmallow | One model system for API schemas, config and LLM output. |
| T5 | ORM / DB access | **SQLAlchemy 2.0 (async)** + **asyncpg** | raw asyncpg, SQLModel | Typed models plus explicit SQL where it matters. |
| T6 | Migrations | **Alembic** | — | Standard with SQLAlchemy. |
| T7 | Broker / data SDK | ~~alpaca-py~~ → **own thin async client** (httpx + websockets) over Alpaca's documented REST/WebSocket APIs *(changed in Phase 2)* | alpaca-py | alpaca-py's REST calls are synchronous (`requests`), which would block the engine's event loop, and its stream client runs its own connection and reconnect loop, which prevents controlling backoff, gap-fill after reconnect, feed tagging, and fake-server tests. The API surface we need is small and documented. Still wrapped behind our interfaces. |
| T8 | Indicators | **Own implementations** on numpy/pandas, tested against reference values | TA-Lib, pandas-ta | About ten indicators are needed. TA-Lib is a C dependency and pandas-ta is poorly maintained. Owning them lets us guarantee no look-ahead and incremental updates. |
| T9 | Scheduler | **APScheduler 3.x** `AsyncIOScheduler`, in the engine | Celery beat, cron | Required by CLAUDE.md. In-process, timezone-aware. |
| T10 | Market calendar | **Alpaca calendar API** (live); **exchange_calendars** (backtests, offline) | pandas_market_calendars | Early closes and holidays without hand-maintained tables. |
| T11 | Time | `zoneinfo` `America/New_York`; all storage UTC `timestamptz`; `session_date` in ET | pytz | Stdlib, DST-correct. |
| T12 | Money | `Decimal` in domain and risk code; `NUMERIC` in DB; floats only inside indicator math | floats everywhere | Sizing and P&L must be exact and reproducible. |
| T13 | Cache / bus | **Redis 7** via redis-py asyncio | NATS, Postgres LISTEN/NOTIFY | Pub/sub, rate-limit counters and heartbeats in one small service. Postgres stays the durable path. |
| T14 | LLM | **anthropic** Python SDK behind `AIProvider`; `client.messages.parse()` with `output_config.format` for schema-validated JSON | tool-use forcing, raw HTTP | Current recommended structured-output path. Model name comes from settings and is never hardcoded (09 §6). |
| T15 | Auth | **argon2-cffi** (argon2id), server-side sessions table, double-submit CSRF | JWT in localStorage | With a single user, revocable server sessions are simpler and safer than JWT refresh flows. |
| T16 | Rate limiting | Redis sliding window, in-memory fallback | slowapi | Keeps working if Redis blips. |
| T17 | Logging | **structlog**, JSON to stdout; Docker log rotation | stdlib logging | Structured and correlates on `decision_id` / `client_order_id`. |
| T18 | Metrics | **prometheus-client** `/metrics` on internal network, plus a DB/Redis-backed System Health page | full Prom+Grafana stack | CLAUDE.md fixes five services. Metrics are exposed so a scraper can be added later. |
| T19 | Email | `aiosmtplib`, generic SMTP | SES, SendGrid SDK | Provider-agnostic. |
| T20 | CLI | **Typer** (`intraday` command) | click | create-admin, backfill, replay, backtest, reconcile-now. |

## Testing and quality

| # | Decision | Choice | Rationale |
|---|---|---|---|
| T21 | Unit/integration tests | **pytest**, pytest-asyncio, **hypothesis** for property tests on sizing and risk | Risk invariants are best expressed as properties. |
| T22 | External API isolation | **respx** (httpx mocks) + recorded JSON fixtures in `tests/fixtures/`; a fake WebSocket server for stream tests | CLAUDE.md: tests never call real Alpaca/Anthropic. |
| T23 | DB in tests | Ephemeral Postgres via `compose.test.yaml` (or testcontainers) | Real SQL semantics (NUMERIC, timestamptz, constraints). |
| T24 | Lint / type | **ruff** (lint + format), **mypy --strict** on `risk`, `trading`, `core` | Catch unit/None bugs in money code. |
| T25 | Architecture rules | **import-linter** contracts (03 §3) | Enforces "AI cannot reach the broker" at CI time. |
| T26 | Frontend tests | **Vitest** + Testing Library; **Playwright** for E2E | Standard for Vite. |
| T27 | CI | GitHub Actions: lint, type, unit, integration (compose), build images | Tests only, with no deploys, per CLAUDE.md. |

## Frontend

| # | Decision | Choice | Alternatives | Rationale |
|---|---|---|---|---|
| T28 | Framework | **React 19 + TypeScript + Vite** (Phase 1 resolved to Vite 8, TypeScript 7, Tailwind 4, Vitest 5) | Next.js | Spec requirement. It's an SPA served statically, so no SSR is needed. |
| T29 | Data fetching | **TanStack Query** + typed client generated from OpenAPI (`openapi-typescript` + `openapi-fetch`) | hand-written fetch | Keeps types in sync with backend. *Phase 1 uses hand-written types in `frontend/src/api/types.ts`, because the Phase 1 endpoints return untyped dicts. Generation is switched on in Phase 10, once responses have Pydantic models (`intraday openapi` already exports the document).* |
| T30 | Routing | **React Router** | TanStack Router | Familiar. |
| T31 | UI kit / styling | **Tailwind CSS + shadcn/ui** (Radix primitives) | MUI, Mantine | Accessible primitives, small bundle, easy dense tables. |
| T32 | Tables | **TanStack Table** | AG Grid | Filtering, sorting, virtualisation; free. |
| T33 | Charts | **TradingView lightweight-charts** (price/trade markers), **Recharts** (analytics) | ECharts | Best-in-class candlesticks. Recharts is enough for bars and lines. |
| T34 | Real-time | Native WebSocket + reconnect/backoff, fallback to polling | socket.io | No extra server dependency. |

## Infrastructure

| # | Decision | Choice | Rationale |
|---|---|---|---|
| T35 | Runtime | Docker Engine + Compose v2 on Ubuntu 24.04 LTS | CLAUDE.md. |
| T36 | Reverse proxy / TLS | **Caddy 2** | Automatic HTTPS, tiny config, serves static files. |
| T37 | Database | **PostgreSQL 16** | Spec. No TimescaleDB needed at ~3M bars/yr. |
| T38 | Backups | Host **systemd timer** → `pg_dump -Fc` → local retention + encrypted (age) off-site copy | Honours the 5-service limit. An off-site copy protects against VM disk loss. |
| T39 | Images | One backend image (api + engine, different commands), one caddy image with built frontend; multi-stage, non-root, pinned digests | Smaller surface, consistent code in both processes. |

## Explicitly not used

Celery, Kubernetes, AWS services, ALB/CloudFront, an ML framework in V1 (D1), TA-Lib, and OpenAI/Local providers beyond interface stubs. The interface exists so they can be added later without touching callers.
