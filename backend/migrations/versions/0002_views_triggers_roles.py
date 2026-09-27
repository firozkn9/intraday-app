"""Views, safety trigger, retention function, and per-service role grants.

- open_positions / closed_positions views (SPEC table names).
- orders trigger: a buy can only be recorded for a TRADABLE U.S. common stock
  (CLAUDE.md "Tradable assets"; docs/design/04).
- prune_retention(): SECURITY DEFINER so the engine can prune without holding
  DELETE on append-only tables.
- Group roles intraday_api_role / intraday_engine_role with least privilege.
  Login roles (intraday_api, intraday_engine) are created by the postgres init
  script and made members of these groups.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

API_ROLE = "intraday_api_role"
ENGINE_ROLE = "intraday_engine_role"

ALL_TABLES = (
    "users", "sessions", "audit_log", "settings_versions", "symbols", "bars_1m", "bars_1d",
    "universe_scans", "news_items", "trading_signals", "llm_calls", "ai_decisions",
    "risk_evaluations", "positions", "orders", "order_events", "fills", "position_events",
    "account_snapshots", "daily_pnl", "strategy_performance", "engine_state", "engine_commands",
    "risk_events", "system_events", "alerts", "backtest_runs", "backtest_trades",
    "backtest_equity",
)

# api: no writes to orders/positions/fills (docs/design/06 §6).
API_WRITES = {
    "users": "INSERT, UPDATE",
    "sessions": "INSERT, UPDATE, DELETE",
    "audit_log": "INSERT",
    "settings_versions": "INSERT, UPDATE",
    "engine_commands": "INSERT",
    "symbols": "INSERT, UPDATE, DELETE",
    "alerts": "UPDATE",
    "backtest_runs": "INSERT, UPDATE, DELETE",
    "backtest_trades": "INSERT, DELETE",
    "backtest_equity": "INSERT, DELETE",
}

# engine: INSERT-only on append-only audit tables; full DML on its working tables.
ENGINE_WRITES = {
    "audit_log": "INSERT",
    "order_events": "INSERT",
    "fills": "INSERT",
    "trading_signals": "INSERT",
    "llm_calls": "INSERT",
    "risk_evaluations": "INSERT",
    "position_events": "INSERT",
    "risk_events": "INSERT",
    "system_events": "INSERT",
    "settings_versions": "INSERT",
    "symbols": "UPDATE",
    "bars_1m": "INSERT, UPDATE",
    "bars_1d": "INSERT, UPDATE",
    "universe_scans": "INSERT",
    "news_items": "INSERT, UPDATE",
    "ai_decisions": "INSERT, UPDATE",
    "positions": "INSERT, UPDATE",
    "orders": "INSERT, UPDATE",
    "account_snapshots": "INSERT",
    "daily_pnl": "INSERT, UPDATE",
    "strategy_performance": "INSERT, UPDATE, DELETE",
    "engine_state": "INSERT, UPDATE",
    "engine_commands": "UPDATE",
    "alerts": "INSERT, UPDATE",
}


def upgrade() -> None:
    # asyncpg executes one statement per call.
    op.execute("CREATE VIEW open_positions AS "
               "SELECT * FROM positions WHERE status IN ('OPEN', 'EXITING')")
    op.execute("CREATE VIEW closed_positions AS "
               "SELECT * FROM positions WHERE status = 'CLOSED'")

    op.execute("""
        CREATE FUNCTION enforce_buy_tradable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.side = 'buy' AND NOT EXISTS (
                SELECT 1 FROM symbols s
                WHERE s.symbol = NEW.symbol
                  AND s.role = 'TRADABLE'
                  AND s.asset_type = 'COMMON_STOCK'
                  AND s.country = 'US'
            ) THEN
                RAISE EXCEPTION 'buy order refused: % is not a tradable U.S. common stock',
                    NEW.symbol USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER orders_buy_tradable
            BEFORE INSERT OR UPDATE OF symbol, side ON orders
            FOR EACH ROW EXECUTE FUNCTION enforce_buy_tradable()
    """)

    op.execute("""
        CREATE FUNCTION prune_retention() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
        DECLARE
            n_signals bigint; n_events bigint; n_scans bigint; n_sessions bigint;
        BEGIN
            DELETE FROM trading_signals t
             WHERE t.action = 'NO_TRADE' AND t.ts < now() - interval '90 days'
               AND NOT EXISTS (SELECT 1 FROM ai_decisions d WHERE d.signal_id = t.id);
            GET DIAGNOSTICS n_signals = ROW_COUNT;
            DELETE FROM system_events
             WHERE severity IN ('DEBUG', 'INFO', 'WARN') AND ts < now() - interval '90 days';
            GET DIAGNOSTICS n_events = ROW_COUNT;
            DELETE FROM universe_scans WHERE ts < now() - interval '180 days';
            GET DIAGNOSTICS n_scans = ROW_COUNT;
            DELETE FROM sessions WHERE expires_at < now() OR revoked_at < now() - interval '1 day';
            GET DIAGNOSTICS n_sessions = ROW_COUNT;
            RETURN jsonb_build_object('trading_signals', n_signals, 'system_events', n_events,
                                      'universe_scans', n_scans, 'sessions', n_sessions);
        END $$
    """)

    for role in (API_ROLE, ENGINE_ROLE):
        op.execute(f"""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role} NOLOGIN;
                END IF;
            END $$
        """)
        op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
        op.execute(f"GRANT SELECT ON {', '.join(ALL_TABLES)} TO {role}")
        op.execute(f"GRANT SELECT ON open_positions, closed_positions TO {role}")
        op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")

    for table, privs in API_WRITES.items():
        op.execute(f"GRANT {privs} ON {table} TO {API_ROLE}")
    for table, privs in ENGINE_WRITES.items():
        op.execute(f"GRANT {privs} ON {table} TO {ENGINE_ROLE}")

    op.execute("REVOKE ALL ON FUNCTION prune_retention() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION prune_retention() TO {ENGINE_ROLE}")


def downgrade() -> None:
    for role in (API_ROLE, ENGINE_ROLE):
        op.execute(f"""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}';
                    EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role}';
                    EXECUTE 'REVOKE ALL ON SCHEMA public FROM {role}';
                END IF;
            END $$
        """)
    op.execute("DROP FUNCTION IF EXISTS prune_retention()")
    op.execute("DROP TRIGGER IF EXISTS orders_buy_tradable ON orders")
    op.execute("DROP FUNCTION IF EXISTS enforce_buy_tradable()")
    op.execute("DROP VIEW IF EXISTS closed_positions")
    op.execute("DROP VIEW IF EXISTS open_positions")
    # Group roles are cluster-wide and may serve other databases (e.g. intraday_sim);
    # they are intentionally not dropped here.
