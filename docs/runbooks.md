# Runbooks

These are for operating the app on its VM (`/opt/intraday`, user `intraday`). The app trades on **Alpaca paper only**. Every command below runs from `/opt/intraday`.

A rule that applies to every incident: **broker-side bracket orders protect open positions even when the engine is down.** Stopping the engine never closes positions.

## Everyday

| Task | Command |
|---|---|
| Status | `docker compose ps` · UI → System Health |
| Logs | `docker compose logs -f --tail=200 engine` (or `api`, `caddy`) · UI → Logs |
| Start or pause trading | UI status bar → START / PAUSE |
| Deploy an update | `infrastructure/scripts/deploy.sh` (outside market hours) |
| Restart the engine | `docker compose restart engine`. It reconciles before any new entry. |
| Manual backup | `infrastructure/scripts/backup.sh` |
| Restore test (monthly) | `infrastructure/scripts/restore.sh --test` |
| Create an admin | `docker compose exec api intraday create-admin` |
| Backfill history | UI → Market Scanner → Backfill, or `docker compose exec engine intraday backfill --days 30` |

## Incidents

### Engine offline (status "ENGINE OFFLINE")
1. Run `docker compose ps engine` and `docker compose logs --tail=200 engine`.
2. Run `docker compose up -d engine`. On boot the engine sets RECONCILING, compares against the broker, and allows entries only after a clean pass.
3. If it keeps crashing, leave it stopped. Open positions keep their broker stops and targets. Before the close, flatten from the Alpaca paper dashboard if needed.

### BROKER ERROR / BROKER_DOWN
- The Alpaca API or trade stream is unreachable, or an order's status is unknown. Entries are blocked automatically.
- Check https://status.alpaca.markets. The block clears after a successful reconcile. You can force one with UI → System Health → Reconcile now.

### RECONCILE_MISMATCH (latched)
1. Open the alert and System Health → Broker → last reconcile. It lists each symbol, the type, and the local and broker quantities.
2. Compare with the Alpaca paper dashboard (positions and orders).
3. `UNEXPECTED_POSITION` means the broker holds shares the engine didn't open. Close them from the Alpaca dashboard, or send CLOSE_POSITION with that symbol. The engine never adopts them.
4. After the states agree, use Reset latch (RECONCILE_MISMATCH) with your password, then press START.

### Not flat at the close (HIGH / CRITICAL "NOT_FLAT", "OVERNIGHT_POSITION")
1. Check the Alpaca paper dashboard for open positions and working orders.
2. Close them there (market sell). Paper positions left overnight carry gap risk in the simulation only, but they still invalidate that day's results.
3. Look in Logs for `LIQUIDATION` and `EXIT_FAILED` to find the cause.

### Daily loss limit
- Entries are blocked and pending entries cancelled. Stops stay in place unless `risk.daily_loss_action=FLATTEN`.
- The next session requires Risk → Reset daily lock (password), then START.

### Emergency stop
- The status bar's EMERGENCY STOP latches, cancels entries and, by default, closes every position.
- To resume, use Reset latch (EMERGENCY_STOP) with your password, then START.

### Database down (DATA ERROR, DB_DOWN)
- The engine keeps trade updates in memory and replays them when the database returns. A reconcile always follows.
- Run `docker compose ps postgres`, `docker compose logs postgres` and `df -h` (disk full is the usual cause).

### AI unavailable (VETO mode)
- After `ai.max_consecutive_failures` consecutive failures, AI_UNAVAILABLE blocks entries. Either fix the key or model, or set AI mode to OFF (paused first).

### Key rotation
1. Regenerate keys in the Alpaca paper dashboard (or the Anthropic console).
2. Edit `.env` and run `docker compose up -d engine`. The keys go to the engine only.
3. Revoke the old keys.

### Restore from backup
1. Run `infrastructure/scripts/restore.sh --test <dump>` first.
2. Outside market hours, run `infrastructure/scripts/restore.sh --apply <dump>` and type RESTORE.
3. The engine reconciles against the broker before any trade. Any broker activity after the dump shows up as mismatches.
