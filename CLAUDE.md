# Intraday App — Project Rules

The full specification is in docs/SPEC.md. The decisions below were made after
that spec was written. Where they conflict with SPEC.md, THIS FILE WINS.

## Absolute rules
- Paper trading only. Backend refuses to start the engine unless TRADING_MODE=PAPER
  and the Alpaca base URL is the paper endpoint. FutureLiveBroker is an interface
  stub that raises NotImplementedError. Never write live-order code.
- Never commit secrets. .env is in .gitignore; only .env.example is committed.
- Tests never call real Alpaca or Anthropic APIs; use mocks and recorded fixtures.
- Build one phase at a time. At the end of each phase: run tests, fix failures,
  summarize what was built, list files changed, give run commands, then STOP and
  wait for my approval before starting the next phase.

## Default configuration (all editable in the Settings UI)
- MIN_STOCK_PRICE = 10
- PAPER_INITIAL_BALANCE = 100000
- MAX_CAPITAL_PER_TRADE = 10000
- MAX_TOTAL_EXPOSURE = 50000
- MAX_OPEN_POSITIONS = 5
- MAX_DAILY_LOSS = 500
- MAX_LOSS_PER_TRADE = 100
- All other values as in SPEC.md section 4.
- The $2,000 example in SPEC.md section 5 is a unit-test case, not a default.
- Long only. Bar interval: 1-minute bars.

## Deployment (replaces the AWS architecture in the spec)
- One production environment running paper trading. No staging/dev deployments.
  Automated tests and the replay/simulation mode are still required.
- Target: a single Ubuntu cloud VM (2 vCPU / 4 GB, US-East) running Docker Compose.
- Services: caddy (HTTPS + serves built frontend), api (FastAPI), engine (separate
  trading-engine process, so an API crash never interrupts position management),
  postgres, redis.
- No Celery, no load balancer, no CloudFront. Use a scheduler (e.g. APScheduler)
  inside the engine process.
- Nightly pg_dump backups with retention. Phase 14 = VM deployment, not AWS.
- Keep modules cleanly separated so moving to AWS later needs no rewrite.

## Market data
- Start on Alpaca's free IEX feed. ALPACA_DATA_FEED=iex|sip is a setting.
- Compute relative volume and similar metrics against history from the SAME feed,
  so switching to SIP later doesn't break strategy thresholds.
- Universe: a curated, editable watchlist, then the SPEC filters applied within it.

## AI
- Default provider: AnthropicProvider; model name is a setting, never hardcoded.
  The API key is optional.
- AI_MODE = OFF | EXPLAIN | VETO (default OFF).
  - OFF: no LLM calls; the scoring engine alone decides.
  - EXPLAIN: LLM rewrites explanations only; no effect on trading.
  - VETO: LLM reviews each proposed trade and may reject it. It can never approve
    a trade the risk engine rejected, and never originates trades.
- "AI unavailable → stop new trades" applies only in VETO mode.
- Explanations are generated from recorded decision data with templates. In
  EXPLAIN/VETO mode the LLM may rephrase them but must not contradict the numbers.
- Send the LLM only structured market data: no account balances, no credentials.

## Orders and exits
- Every entry is submitted as a broker-side bracket order (stop + take-profit) as
  a safety net; the engine may exit earlier via its own exit rules.
- Daily loss limit: cancel entries, block new trades, keep protective stops
  active (configurable: optionally flatten). Emergency stop: default closes all.

## Users and alerts
- Single user. One admin account, argon2 password hashing, secure session/JWT,
  CSRF protection, rate limiting, audit log. Keep a role column for later.
- Alerts: pluggable notifier; start with dashboard alerts + email.

## Tradable assets
- V1 trades U.S.-listed common stocks ONLY (NYSE/Nasdaq), long only.
- The universe filter rejects everything else: ETFs (including leveraged/inverse),
  options, crypto, futures, forex, bonds, OTC, warrants, preferred shares,
  and non-U.S. securities. No settings toggle can enable these.
- SPY, QQQ, sector ETFs, and a volatility proxy are READ-ONLY market-context
  inputs for the regime detector and are never tradable.
- The risk engine independently re-checks asset type before every order,
  so a bad universe entry can't slip through.