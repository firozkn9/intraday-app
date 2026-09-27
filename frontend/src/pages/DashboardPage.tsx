import { useState } from "react";
import { useGet, useRecentDecisions, useRegime, useStatus } from "../api/hooks";
import type { Decision, RegimeResponse } from "../api/types";
import { statusLabel } from "../components/StatusBar";
import { LineChart } from "../components/LineChart";
import { Alert, Badge, Card } from "../components/ui";
import { money, pnlClass } from "../lib/format";
import { ClosePositionModal, PositionsTable, type PositionRow } from "./PositionsPage";

interface Summary {
  account: { as_of: string; equity: string; cash: string; buying_power: string; exposure: string } | null;
  today: { pnl: string; realized: string; unrealized: string; trades: number; wins: number; losses: number;
    win_rate: number | null; daily_loss_limit: string | null; daily_loss_used: string | null };
  total_pnl: string;
  open_positions_count: number;
}

function Stat({ label, value, className = "" }: { label: string; value: string; className?: string }) {
  return (
    <div>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className={`num text-lg font-semibold ${className}`}>{value}</dd>
    </div>
  );
}

const STRATEGY: Record<string, string> = {
  momentum_breakout: "Breakout",
  vwap_reclaim: "VWAP Reclaim",
  pullback: "Pullback",
};

export function SignalsCard({ regime, decisions }: { regime?: RegimeResponse; decisions?: Decision[] }) {
  const r = regime?.regime;
  return (
    <Card title="Latest proposals">
      <p className="text-sm">
        Regime: <Badge tone={r?.trend === "BULLISH" ? "ok" : r?.trend === "BEARISH" ? "danger" : "neutral"}>
          {r?.label ?? "—"}
        </Badge>
        {r?.breadth != null && <span className="ml-2 text-muted">breadth {(r.breadth * 100).toFixed(0)}%</span>}
      </p>
      {decisions && decisions.length > 0 ? (
        <ul className="mt-3 space-y-2 text-sm">
          {decisions.map((d) => (
            <li key={d.id} className="flex flex-wrap items-center gap-2">
              <span className="num text-xs text-muted">
                {new Date(d.ts).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" })}
              </span>
              <strong>{d.symbol}</strong>
              <span>{STRATEGY[d.strategy] ?? d.strategy}</span>
              <span className="num">{(d.confidence * 100).toFixed(0)}%</span>
              <Badge tone={d.decision_result === "ORDER_SUBMITTED" ? "ok" : d.decision_result === "BLOCKED" ? "neutral" : "warn"}
                title={d.block_reasons.join(", ")}>
                {d.decision_result}
              </Badge>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 text-sm text-muted">No proposals yet.</p>
      )}
    </Card>
  );
}

export function DashboardPage() {
  const { data: status } = useStatus();
  const { data: regime } = useRegime();
  const { data: decisions } = useRecentDecisions();
  const { data: summary } = useGet<Summary>("/dashboard/summary", 10000);
  const { data: positions } = useGet<PositionRow[]>("/positions", 5000);
  const { data: equity } = useGet<{ ts: string; equity: string }[]>("/dashboard/equity-intraday", 60000);
  const [closing, setClosing] = useState<PositionRow | null>(null);
  if (!status) return <p className="text-muted">Loading…</p>;
  const { label, tone } = statusLabel(status);

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Dashboard</h1>
      <Alert tone="accent">Paper trading only. Every entry is a broker-side bracket order.</Alert>
      <Card title="Account & today">
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-7">
          <Stat label="Equity" value={money(summary?.account?.equity)} />
          <Stat label="Buying power" value={money(summary?.account?.buying_power)} />
          <Stat label="Today P&L" value={money(summary?.today.pnl, true)} className={pnlClass(summary?.today.pnl)} />
          <Stat label="Realized" value={money(summary?.today.realized, true)} className={pnlClass(summary?.today.realized)} />
          <Stat label="Unrealized" value={money(summary?.today.unrealized, true)} className={pnlClass(summary?.today.unrealized)} />
          <Stat label="Trades (W/L)" value={summary ? `${summary.today.trades} (${summary.today.wins}/${summary.today.losses})` : "—"} />
          <Stat label="Daily loss used" value={summary?.today.daily_loss_limit
            ? `${money(summary.today.daily_loss_used)} / ${money(summary.today.daily_loss_limit)}` : "—"} />
        </dl>
        {!summary?.account && <p className="mt-2 text-xs text-muted">No broker account snapshot yet.</p>}
      </Card>
      <Card title={`Open positions (${positions?.length ?? 0})`}>
        <PositionsTable rows={positions ?? []} onClose={setClosing} />
      </Card>
      <ClosePositionModal position={closing} onDone={() => setClosing(null)} />
      <Card title="Equity today">
        <LineChart label="Intraday equity"
          points={(equity ?? []).map((e) => ({ x: new Date(e.ts).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" }), y: Number(e.equity) }))} />
      </Card>
      {status.paper_guard && !status.paper_guard.ok && (
        <Alert tone="danger">
          <strong>Paper guard refused to start trading:</strong>
          <ul className="mt-1 list-disc pl-5">
            {status.paper_guard.problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </Alert>
      )}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        <Card title="Engine">
          <dl className="grid grid-cols-2 gap-y-2 text-sm">
            <dt className="text-muted">Status</dt>
            <dd>
              <Badge tone={tone}>{label}</Badge>
            </dd>
            <dt className="text-muted">Trading enabled</dt>
            <dd>{status.trading_enabled ? "Yes" : "No (paused)"}</dd>
            <dt className="text-muted">Heartbeat</dt>
            <dd className="num">
              {status.engine_heartbeat_age_s == null ? "never" : `${status.engine_heartbeat_age_s}s ago`}
            </dd>
            <dt className="text-muted">Version</dt>
            <dd className="num">{status.engine_version ?? "—"}</dd>
          </dl>
        </Card>
        <Card title="Why entries are blocked">
          {status.blocks.length === 0 ? (
            <p className="text-sm text-muted">Nothing is blocking new entries.</p>
          ) : (
            <ul className="space-y-2 text-sm">
              {status.blocks.map((b) => (
                <li key={b.reason}>
                  <code className="text-xs font-semibold">{b.reason}</code>
                  {b.latched && (
                    <>
                      {" "}
                      <Badge tone="danger">latched</Badge>
                    </>
                  )}
                  <p className="text-muted">{b.detail}</p>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <SignalsCard regime={regime} decisions={decisions} />
        <Card title="Market data">
          <dl className="grid grid-cols-2 gap-y-2 text-sm">
            <dt className="text-muted">Session</dt>
            <dd>{status.market.is_open ? "Open" : "Closed"}{status.market.early_close ? " (early close)" : ""}</dd>
            <dt className="text-muted">Data</dt>
            <dd>{status.market.data.enabled ? "Enabled" : `Disabled — ${status.market.data.reason ?? "unknown"}`}</dd>
            <dt className="text-muted">Stream</dt>
            <dd>{status.market.data.stream ?? "—"}</dd>
            <dt className="text-muted">Streamed symbols</dt>
            <dd className="num">{status.market.data.subscribed ?? "—"}</dd>
            <dt className="text-muted">Stale symbols</dt>
            <dd>{status.market.data.stale_symbols.length ? status.market.data.stale_symbols.join(", ") : "none"}</dd>
          </dl>
        </Card>
        <Card title="Configuration">
          <dl className="grid grid-cols-2 gap-y-2 text-sm">
            <dt className="text-muted">Active settings</dt>
            <dd className="num">v{status.active_config_version ?? "—"}</dd>
            <dt className="text-muted">Engine running</dt>
            <dd className="num">v{status.running_config_version ?? "—"}</dd>
            <dt className="text-muted">AI mode</dt>
            <dd>{status.ai_mode ?? "—"}</dd>
            <dt className="text-muted">Data feed</dt>
            <dd>{status.data_feed?.toUpperCase() ?? "—"}</dd>
          </dl>
        </Card>
      </div>
    </div>
  );
}
