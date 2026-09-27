import { useState, type FormEvent } from "react";
import { ApiError } from "../api/client";
import { useBacktest, useBacktests, useBacktestTrades, useCancelBacktest, useCreateBacktest } from "../api/hooks";
import type { BacktestMetrics, BacktestRun, Split } from "../api/types";
import { Alert, Badge, Button, Card, type Tone } from "../components/ui";

const SPLITS: Split[] = ["TRAIN", "VALIDATION", "TEST"];
const STATUS_TONE: Record<BacktestRun["status"], Tone> = {
  QUEUED: "neutral",
  RUNNING: "accent",
  DONE: "ok",
  FAILED: "danger",
  CANCELED: "neutral",
};

const ROWS: [keyof BacktestMetrics, string, (v: never) => string][] = [
  ["sessions", "Sessions", (v: number) => String(v)],
  ["trades", "Trades", (v: number) => String(v)],
  ["total_return_pct", "Total return", (v: number) => `${v.toFixed(2)}%`],
  ["annualized_return_pct", "Annualized return", (v: number) => `${v.toFixed(2)}%`],
  ["max_drawdown_pct", "Max drawdown", (v: number) => `${v.toFixed(2)}%`],
  ["sharpe", "Sharpe", (v: number) => v.toFixed(2)],
  ["sortino", "Sortino", (v: number) => v.toFixed(2)],
  ["win_rate_pct", "Win rate", (v: number) => `${v.toFixed(1)}%`],
  ["profit_factor", "Profit factor", (v: number) => v.toFixed(2)],
  ["average_win", "Average win", (v: number) => `$${v.toFixed(2)}`],
  ["average_loss", "Average loss", (v: number) => `$${v.toFixed(2)}`],
  ["largest_loss", "Largest loss", (v: number) => `$${v.toFixed(2)}`],
  ["largest_winning_streak", "Longest win streak", (v: number) => String(v)],
  ["largest_losing_streak", "Longest loss streak", (v: number) => String(v)],
  ["average_holding_minutes", "Avg holding (min)", (v: number) => v.toFixed(1)],
  ["fees", "Fees", (v: number) => `$${v.toFixed(2)}`],
  ["net_pnl", "Net P&L", (v: number) => `$${v.toFixed(2)}`],
];

export function MetricsTable({ metrics }: { metrics: Partial<Record<Split, BacktestMetrics>> }) {
  const present = SPLITS.filter((s) => metrics[s]);
  return (
    <table className="w-full text-sm">
      <thead className="text-xs text-muted">
        <tr>
          <th className="py-1 text-left font-medium">Metric</th>
          {present.map((s) => (
            <th key={s} className={`py-1 text-right font-medium ${s === "TEST" ? "text-accent" : ""}`}>
              {s === "TEST" ? "TEST (out-of-sample)" : s}
            </th>
          ))}
        </tr>
      </thead>
      <tbody className="divide-y divide-border">
        {ROWS.map(([key, label, fmt]) => (
          <tr key={key}>
            <td className="py-1 text-muted">{label}</td>
            {present.map((s) => {
              const v = metrics[s]?.[key];
              return (
                <td key={s} className={`num py-1 text-right ${s === "TEST" ? "font-semibold" : ""}`}>
                  {v == null ? "—" : (fmt as (x: unknown) => string)(v)}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function NewRun() {
  const create = useCreateBacktest();
  const [f, setF] = useState({ trainStart: "", trainEnd: "", valStart: "", valEnd: "", testStart: "", testEnd: "",
    symbols: "", capital: "", slippage: "5" });
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const range = (a: string, b: string) => (a && b ? { start: a, end: b } : null);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate({
      train: range(f.trainStart, f.trainEnd),
      validation: range(f.valStart, f.valEnd),
      test: range(f.testStart, f.testEnd),
      symbols: f.symbols.trim() ? f.symbols.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean) : null,
      starting_capital: f.capital ? Number(f.capital) : null,
      costs: { slippage_bps: Number(f.slippage || 0) },
    });
  };
  const err = create.error instanceof ApiError ? create.error : null;
  const input = "rounded-md border border-border bg-bg px-2 py-1 text-sm";
  return (
    <Card title="New backtest">
      <form onSubmit={submit} className="space-y-3 text-sm">
        {(
          [
            ["Train", "trainStart", "trainEnd"],
            ["Validation", "valStart", "valEnd"],
            ["Test (out-of-sample)", "testStart", "testEnd"],
          ] as const
        ).map(([label, a, b]) => (
          <div key={label} className="flex flex-wrap items-center gap-2">
            <span className="w-40 font-medium">{label}</span>
            <input type="date" aria-label={`${label} start`} className={input} value={f[a]} onChange={set(a)} />
            <span>to</span>
            <input type="date" aria-label={`${label} end`} className={input} value={f[b]} onChange={set(b)} />
          </div>
        ))}
        <div className="flex flex-wrap items-center gap-2">
          <span className="w-40 font-medium">Symbols</span>
          <input className={`${input} w-64`} placeholder="default: tradable watchlist" value={f.symbols} onChange={set("symbols")} />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="w-40 font-medium">Starting capital</span>
          <input className={`${input} w-32`} inputMode="decimal" placeholder="from settings" value={f.capital} onChange={set("capital")} />
          <span className="ml-4 font-medium">Slippage (bps)</span>
          <input className={`${input} w-20`} inputMode="numeric" value={f.slippage} onChange={set("slippage")} />
        </div>
        <p className="text-xs text-muted">
          Splits must be sequential. Tune on train/validation only; treat the test split as the honest estimate.
        </p>
        {err && (
          <Alert tone="danger">
            {err.title}
            {err.errors.length > 0 && (
              <ul className="mt-1 list-disc pl-5">
                {err.errors.map((x) => (
                  <li key={x.key + x.message}>{x.message}</li>
                ))}
              </ul>
            )}
          </Alert>
        )}
        <Button type="submit" variant="primary" disabled={create.isPending}>
          {create.isPending ? "Starting…" : "Run backtest"}
        </Button>
      </form>
    </Card>
  );
}

function RunDetail({ id }: { id: string }) {
  const run = useBacktest(id);
  const trades = useBacktestTrades(run.data?.status === "DONE" ? id : null);
  if (!run.data) return <p className="text-muted">Loading…</p>;
  return (
    <div className="space-y-4">
      {run.data.error && <Alert tone="danger">{run.data.error}</Alert>}
      {run.data.metrics && (
        <Card title="Metrics">
          <MetricsTable metrics={run.data.metrics} />
        </Card>
      )}
      {trades.data && (
        <Card title={`Trades (${trades.data.length})`}>
          <div className="max-h-96 overflow-auto">
            <table className="w-full text-xs">
              <thead className="text-muted">
                <tr>
                  {["Split", "Date", "Symbol", "Strategy", "Qty", "Entry", "Exit", "Reason", "Net"].map((h) => (
                    <th key={h} className="py-1 text-left font-medium">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {trades.data.map((t, i) => (
                  <tr key={i}>
                    <td className="py-1">{t.split}</td>
                    <td>{t.session_date}</td>
                    <td className="font-semibold">{t.symbol}</td>
                    <td>{t.strategy}</td>
                    <td className="num">{t.qty}</td>
                    <td className="num">{Number(t.entry_price).toFixed(2)}</td>
                    <td className="num">{Number(t.exit_price).toFixed(2)}</td>
                    <td>{t.exit_reason}</td>
                    <td className={`num ${Number(t.net_pnl) >= 0 ? "text-ok" : "text-danger"}`}>{Number(t.net_pnl).toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}

export function BacktestingPage() {
  const runs = useBacktests();
  const cancel = useCancelBacktest();
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Backtesting</h1>
      <Alert tone="warn">
        Backtests replay stored {`IEX`} bars through the live pipeline and risk engine with simulated fills, fees and
        slippage. Past results do not predict future results.
      </Alert>
      <NewRun />
      <Card title="Runs">
        {!runs.data?.length ? (
          <p className="text-sm text-muted">No runs yet.</p>
        ) : (
          <ul className="divide-y divide-border text-sm">
            {runs.data.map((r) => (
              <li key={r.id} className="flex flex-wrap items-center gap-3 py-2">
                <Badge tone={STATUS_TONE[r.status]}>{r.status}</Badge>
                <span className="text-xs text-muted">{new Date(r.created_at).toLocaleString()}</span>
                <span className="num text-xs">
                  {SPLITS.filter((s) => r.summary[s]).map((s) => `${s} ${r.summary[s]?.total_return_pct?.toFixed(2) ?? "—"}% (${r.summary[s]?.trades ?? 0} trades)`).join(" · ")}
                </span>
                <span className="ml-auto flex gap-2">
                  {(r.status === "QUEUED" || r.status === "RUNNING") && (
                    <Button onClick={() => cancel.mutate(r.id)}>Cancel</Button>
                  )}
                  <Button onClick={() => setSelected(selected === r.id ? null : r.id)}>
                    {selected === r.id ? "Hide" : "Details"}
                  </Button>
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
      {selected && <RunDetail id={selected} />}
    </div>
  );
}
