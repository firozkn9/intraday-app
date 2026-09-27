import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { useGet, useStatus } from "../api/hooks";
import { LineChart, Table } from "../components/LineChart";
import { Badge, Button, Card, Modal } from "../components/ui";
import { etTime, money, pct, pnlClass } from "../lib/format";

// ------------------------------------------------------------------ calendar

interface CalDay {
  date: string; status: "PROFIT" | "LOSS" | "FLAT" | "NO_TRADING" | "HOLIDAY"; realized_net: string;
  trades: number; wins: number; losses: number; win_rate?: number | null;
}

const CELL: Record<CalDay["status"], string> = {
  PROFIT: "bg-ok-bg text-ok", LOSS: "bg-danger-bg text-danger", FLAT: "bg-neutral-bg",
  NO_TRADING: "bg-surface", HOLIDAY: "bg-surface text-muted",
};

export function CalendarGrid({ days, onPick }: { days: CalDay[]; onPick: (d: string) => void }) {
  const first = days[0] ? new Date(`${days[0].date}T12:00:00`).getDay() : 1;   // 1 = Monday
  return (
    <div className="grid grid-cols-5 gap-1" role="grid" aria-label="Trading calendar">
      {["Mon", "Tue", "Wed", "Thu", "Fri"].map((d) => <div key={d} className="text-center text-xs text-muted">{d}</div>)}
      {Array.from({ length: Math.max(0, first - 1) }).map((_, i) => <div key={`pad${i}`} />)}
      {days.map((d) => (
        <button key={d.date} type="button" onClick={() => onPick(d.date)} disabled={!d.trades}
          className={`min-h-16 rounded-md border border-border p-1 text-left text-xs ${CELL[d.status]}`}
          aria-label={`${d.date} ${d.status}`}>
          <div className="font-semibold">{Number(d.date.slice(8))}</div>
          {d.trades > 0 ? (
            <>
              <div className="num">{money(d.realized_net, true)}</div>
              <div>{d.trades} trades</div>
            </>
          ) : d.status === "HOLIDAY" ? <div>holiday</div> : null}
        </button>
      ))}
    </div>
  );
}

export function CalendarPage() {
  const now = new Date();
  const [params] = useSearchParams();
  const initial = params.get("month");
  const [month, setMonth] = useState(initial && /^\d{4}-\d{2}$/.test(initial)
    ? initial : `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`);
  const [day, setDay] = useState<string | null>(null);
  const { data } = useGet<{ days: CalDay[]; totals: { realized_net: string; trades: number; wins: number; losses: number } }>(
    `/calendar?month=${month}`, 60000);
  const { data: detail } = useGet<{ summary: Record<string, unknown> | null; trades: {
    id: string; symbol: string; entry_time: string; exit_time: string; qty: number; net_pnl: string;
    return_pct: number | null; strategy: string; exit_reason: string }[] }>(day ? `/calendar/${day}` : null);
  const shift = (n: number) => {
    const [y = 2026, m = 1] = month.split("-").map(Number);
    const d = new Date(y, m - 1 + n, 1);
    setMonth(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`);
  };
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Calendar</h1>
      <Card title={month} actions={<div className="flex gap-2"><Button onClick={() => shift(-1)}>‹</Button><Button onClick={() => shift(1)}>›</Button></div>}>
        {data ? <CalendarGrid days={data.days} onPick={setDay} /> : <p className="text-muted">Loading…</p>}
        {data && (
          <p className="mt-3 text-sm">
            Month: <span className={pnlClass(data.totals.realized_net)}>{money(data.totals.realized_net, true)}</span> ·{" "}
            {data.totals.trades} trades ({data.totals.wins} W / {data.totals.losses} L)
          </p>
        )}
      </Card>
      <Modal title={`Trades on ${day}`} open={day !== null} onClose={() => setDay(null)}>
        <Table head={["Symbol", "Entry", "Exit", "Qty", "Reason", "Net P&L", "Return"]} empty={!detail?.trades.length}>
          {detail?.trades.map((t) => (
            <tr key={t.id} className="border-b border-border/60">
              <td className="px-2 py-1.5 font-semibold"><Link to={`/history/${t.id}`}>{t.symbol}</Link></td>
              <td className="px-2 py-1.5">{etTime(t.entry_time)}</td>
              <td className="px-2 py-1.5">{etTime(t.exit_time)}</td>
              <td className="num px-2 py-1.5">{t.qty}</td>
              <td className="px-2 py-1.5">{t.exit_reason}</td>
              <td className={`num px-2 py-1.5 ${pnlClass(t.net_pnl)}`}>{money(t.net_pnl, true)}</td>
              <td className="num px-2 py-1.5">{pct(t.return_pct)}</td>
            </tr>
          ))}
        </Table>
      </Modal>
    </div>
  );
}

// ------------------------------------------------------------------ performance

type Metrics = Record<string, number | string | null | Record<string, unknown>>;

const METRIC_LABELS: [string, string, "money" | "pct" | "num"][] = [
  ["cumulative_pnl", "Cumulative P&L", "money"], ["trades", "Trades", "num"], ["win_rate_pct", "Win rate", "pct"],
  ["profit_factor", "Profit factor", "num"], ["average_trade", "Avg trade", "money"], ["average_win", "Avg win", "money"],
  ["average_loss", "Avg loss", "money"], ["max_drawdown", "Max drawdown", "money"], ["sharpe", "Sharpe (daily)", "num"],
  ["average_holding_minutes", "Avg holding (min)", "num"],
];

export function PerformancePage() {
  const [by, setBy] = useState("strategy");
  const { data: s } = useGet<Metrics>("/performance/summary", 60000);
  const { data: curve } = useGet<{ date: string; equity: string }[]>("/performance/equity-curve", 60000);
  const { data: pnl } = useGet<{ period: string; net_pnl: string; trades: number }[]>("/performance/pnl?period=week", 60000);
  const { data: rows } = useGet<{ key: string; trades: number; win_rate_pct: number; net_pnl: string; avg_pnl: string;
    profit_factor: number | null }[]>(`/performance/breakdown?by=${by}`, 60000);
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Performance</h1>
      <Card title="Summary (paper trading)">
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {METRIC_LABELS.map(([k, label, kind]) => {
            const v = s?.[k] as number | string | null | undefined;
            return (
              <div key={k}>
                <dt className="text-xs text-muted">{label}</dt>
                <dd className={`num font-semibold ${kind === "money" ? pnlClass(v) : ""}`}>
                  {v == null ? "—" : kind === "money" ? money(v, true) : kind === "pct" ? `${Number(v).toFixed(1)}%` : String(v)}
                </dd>
              </div>
            );
          })}
        </dl>
      </Card>
      <Card title="Equity curve (realized)">
        <LineChart label="Equity curve" points={(curve ?? []).map((c) => ({ x: c.date, y: Number(c.equity) }))} />
      </Card>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Weekly P&L">
          <Table head={["Week of", "Trades", "Net P&L"]} empty={!pnl?.length}>
            {pnl?.map((b) => (
              <tr key={b.period} className="border-b border-border/60">
                <td className="px-2 py-1.5">{b.period}</td><td className="num px-2 py-1.5">{b.trades}</td>
                <td className={`num px-2 py-1.5 ${pnlClass(b.net_pnl)}`}>{money(b.net_pnl, true)}</td>
              </tr>
            ))}
          </Table>
        </Card>
        <Card title="Breakdown" actions={
          <select className="rounded-md border border-border bg-surface-2 px-2 py-1 text-sm" value={by}
            onChange={(e) => setBy(e.target.value)} aria-label="Breakdown by">
            {["strategy", "symbol", "hour", "confidence_bucket", "exit_reason"].map((b) => <option key={b}>{b}</option>)}
          </select>}>
          <Table head={[by, "Trades", "Win %", "Net P&L", "Avg", "PF"]} empty={!rows?.length}>
            {rows?.map((r) => (
              <tr key={r.key} className="border-b border-border/60">
                <td className="px-2 py-1.5">{r.key}</td><td className="num px-2 py-1.5">{r.trades}</td>
                <td className="num px-2 py-1.5">{r.win_rate_pct.toFixed(0)}%</td>
                <td className={`num px-2 py-1.5 ${pnlClass(r.net_pnl)}`}>{money(r.net_pnl, true)}</td>
                <td className="num px-2 py-1.5">{money(r.avg_pnl, true)}</td>
                <td className="num px-2 py-1.5">{r.profit_factor ?? "—"}</td>
              </tr>
            ))}
          </Table>
        </Card>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ strategies & risk

export function StrategiesPage() {
  const { data: rows } = useGet<{ key: string; trades: number; win_rate_pct: number; net_pnl: string; profit_factor: number | null }[]>(
    "/performance/breakdown?by=strategy", 60000);
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Strategies</h1>
      <p className="text-sm text-muted">
        Parameters are edited under <Link className="text-accent" to="/settings">Settings → Strategies</Link> (versioned, with
        the same safety rules as every other setting).
      </p>
      <Card title="All-time results by strategy">
        <Table head={["Strategy", "Trades", "Win %", "Net P&L", "Profit factor"]} empty={!rows?.length}>
          {rows?.map((r) => (
            <tr key={r.key} className="border-b border-border/60">
              <td className="px-2 py-1.5">{r.key}</td><td className="num px-2 py-1.5">{r.trades}</td>
              <td className="num px-2 py-1.5">{r.win_rate_pct.toFixed(0)}%</td>
              <td className={`num px-2 py-1.5 ${pnlClass(r.net_pnl)}`}>{money(r.net_pnl, true)}</td>
              <td className="num px-2 py-1.5">{r.profit_factor ?? "—"}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  );
}

export function RiskPage() {
  const { data: status } = useStatus();
  const { data: summary } = useGet<{ today: { daily_loss_limit: string | null; daily_loss_used: string | null } }>(
    "/dashboard/summary", 10000);
  const used = Number(summary?.today.daily_loss_used ?? 0);
  const limit = Number(summary?.today.daily_loss_limit ?? 0);
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Risk</h1>
      <Card title="Daily loss">
        <p className="num text-lg">{money(used)} of {limit ? money(limit) : "—"}</p>
        <div className="mt-2 h-2 w-full rounded bg-surface-2">
          <div className={`h-2 rounded ${used / (limit || 1) > 0.8 ? "bg-danger" : "bg-accent"}`}
            style={{ width: `${Math.min(100, (used / (limit || 1)) * 100)}%` }} />
        </div>
      </Card>
      <Card title="Latched blocks">
        {status?.latched.length ? status.latched.map((l) => <Badge key={l} tone="danger">{l}</Badge>)
          : <p className="text-sm text-muted">None.</p>}
      </Card>
      <p className="text-sm text-muted">
        Limits are edited under <Link className="text-accent" to="/settings">Settings → Capital &amp; risk</Link>. Loosening a
        limit requires trading to be paused; hard limits can never be exceeded.
      </p>
    </div>
  );
}
