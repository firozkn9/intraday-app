# Alpaca fixtures

Synthetic responses in the formats documented at docs.alpaca.markets (checked
2026-09-26): `/v2/stocks/bars`, `/v2/stocks/quotes/latest`, `/v2/assets/{symbol}`,
`/v2/calendar`, and the market-data WebSocket protocol.

They are **not** captured from a live session: no API keys were used to build them.
Phase 7's manual paper-API check will replace or extend them with real recordings.
Tests never call the real Alpaca APIs (CLAUDE.md).

Session used: Monday 2026-09-21 (EDT, regular hours 13:30–20:00 UTC).
