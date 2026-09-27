import { useState } from "react";
import { Link, useParams } from "react-router";
import { API_BASE } from "../api/client";
import { useGet } from "../api/hooks";
import { LineChart, Table } from "../components/LineChart";
import { Badge, Button, Card } from "../components/ui";
import { duration, etDateTime, money, pct, pnlClass } from "../lib/format";
import type { PositionRow } from "./PositionsPage";

interface TradeRow extends PositionRow {
  session_date: string;
  entry_time: string | null;
  exit_time: string | null;
  avg_exit: string | null;
  return_pct: number | null;
}

const FILTERS = ["date_from", "date_to", "symbol", "strategy", "outcome", "exit_reason"] as const;

export function TradeHistoryPage() {
  const [f, setF] = useState<Record<string, string>>({});
  const [page, setPage] = useState(1);
  const qs = new URLSearchParams(Object.entries(f).filter(([, v]) => v));
  const { data } = useGet<{ total: number; items: TradeRow[] }>(
    `/trades?${qs.toString()}&page=${page}&page_size=50`, 30000);
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Trade history</h1>
      <Card title="Filters" actions={
        <a className="text-sm text-accent hover:underline" href={`${API_BASE}/trades/export.csv?${qs.toString()}`}>
          Export CSV
        </a>}>
        <div className="flex flex-wrap gap-2 text-sm">
          {FILTERS.map((k) => (
            <label key={k} className="flex flex-col">
              <span className="text-xs text-muted">{k.replace("_", " ")}</span>
              {k === "outcome" ? (
                <select className="rounded-md border border-border bg-surface-2 px-2 py-1" value={f[k] ?? ""}
                  onChange={(e) => { setF({ ...f, [k]: e.target.value }); setPage(1); }}>
                  <option value="">any</option><option value="win">win</option><option value="loss">loss</option>
                </select>
              ) : (
                <input type={k.startsWith("date") ? "date" : "text"} value={f[k] ?? ""}
                  className="rounded-md border border-border bg-surface-2 px-2 py-1"
                  onChange={(e) => { setF({ ...f, [k]: e.target.value }); setPage(1); }} />
              )}
            </label>
          ))}
        </div>
      </Card>
      <Card title={`${data?.total ?? 0} trades`}>
        <Table head={["Date", "Symbol", "Strategy", "Qty", "Entry", "Exit", "Held", "Reason", "Net P&L", "Return"]}
          empty={!data?.items.length}>
          {data?.items.map((t) => (
            <tr key={t.id} className="border-b border-border/60">
              <td className="px-2 py-1.5">{t.session_date}</td>
              <td className="px-2 py-1.5 font-semibold"><Link to={`/history/${t.id}`} className="hover:underline">{t.symbol}</Link></td>
              <td className="px-2 py-1.5">{t.strategy}</td>
              <td className="num px-2 py-1.5">{t.qty}</td>
              <td className="num px-2 py-1.5">{money(t.avg_entry)}</td>
              <td className="num px-2 py-1.5">{money(t.avg_exit)}</td>
              <td className="px-2 py-1.5">{duration(t.holding_seconds)}</td>
              <td className="px-2 py-1.5"><Badge>{t.exit_reason ?? "—"}</Badge></td>
              <td className={`num px-2 py-1.5 ${pnlClass(t.net_pnl)}`}>{money(t.net_pnl, true)}</td>
              <td className={`num px-2 py-1.5 ${pnlClass(t.return_pct)}`}>{pct(t.return_pct)}</td>
            </tr>
          ))}
        </Table>
        <div className="mt-3 flex gap-2">
          <Button disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</Button>
          <Button disabled={!data || page * 50 >= data.total} onClick={() => setPage(page + 1)}>Next</Button>
        </div>
      </Card>
    </div>
  );
}

interface TradeDetail {
  position: TradeRow;
  entry: {
    signal: { features: Record<string, unknown>; conditions: Record<string, unknown>;
      factor_scores: Record<string, number>; score: number; regime: string; signals: string[] } | null;
    decision: { decision_result: string; ai_mode: string; llm_verdict: string | null; confidence: number;
      entry_ref_price: string; stop_loss: string; take_profit: string; risk_reward: number } | null;
    llm_call: { model: string; valid: boolean; parsed: unknown; error: string | null } | null;
    risk_evaluation: { approved: boolean; checks: { id: string; name: string; passed: boolean; detail: string }[];
      qty: number; max_loss: string } | null;
  };
  orders: { id: string; intent: string; type: string; qty: number; filled_qty: number; status: string;
    limit_price: string | null; stop_price: string | null; filled_avg_price: string | null; client_order_id: string;
    events: { ts: string; source: string; event: string }[] }[];
  explanation: { entry_text: string | null; exit_text: string | null; source: string };
  bars: { t: string; c: number }[];
}

export function TradeDetailPage() {
  const { id } = useParams();
  const { data } = useGet<TradeDetail>(id ? `/trades/${id}` : null);
  if (!data) return <p className="text-muted">Loading…</p>;
  const p = data.position;
  const risk = data.entry.risk_evaluation;
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">
        {p.symbol} · {p.strategy} <Badge>{p.status}</Badge>{" "}
        <span className={pnlClass(p.net_pnl)}>{money(p.net_pnl, true)}</span>
      </h1>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title={`Explanation (${data.explanation.source.toLowerCase()})`}>
          <pre className="whitespace-pre-wrap text-sm">{data.explanation.entry_text ?? "—"}</pre>
          {data.explanation.exit_text && <p className="mt-2 text-sm"><strong>Exit:</strong> {data.explanation.exit_text}</p>}
        </Card>
        <Card title="Price (session)">
          <LineChart label={`${p.symbol} session closes`}
            points={data.bars.map((b) => ({ x: new Date(b.t).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" }), y: b.c }))} />
          <p className="mt-2 text-sm">Entry {money(p.avg_entry)} · Exit {money(p.avg_exit)} · Stop {money(p.stop)} · Target {money(p.target)}</p>
        </Card>
        <Card title="Decision">
          {data.entry.decision ? (
            <dl className="grid grid-cols-2 gap-y-1 text-sm">
              <dt className="text-muted">Result</dt><dd>{data.entry.decision.decision_result}</dd>
              <dt className="text-muted">Confidence</dt><dd>{Math.round(data.entry.decision.confidence * 100)}%</dd>
              <dt className="text-muted">Reward/risk</dt><dd>{data.entry.decision.risk_reward.toFixed(2)}</dd>
              <dt className="text-muted">AI mode</dt><dd>{data.entry.decision.ai_mode}{data.entry.decision.llm_verdict ? ` (${data.entry.decision.llm_verdict})` : ""}</dd>
              <dt className="text-muted">Regime</dt><dd>{data.entry.signal?.regime ?? "—"}</dd>
            </dl>
          ) : <p className="text-sm text-muted">No decision recorded.</p>}
          {data.entry.signal && (
            <ul className="mt-2 flex flex-wrap gap-1">
              {Object.entries(data.entry.signal.factor_scores).map(([k, v]) => (
                <Badge key={k}>{k} {v.toFixed(2)}</Badge>
              ))}
            </ul>
          )}
        </Card>
        <Card title={`Risk checks${risk ? (risk.approved ? " (approved)" : " (rejected)") : ""}`}>
          {risk ? (
            <ul className="max-h-64 space-y-0.5 overflow-y-auto text-xs">
              {risk.checks.map((c) => (
                <li key={c.id}><Badge tone={c.passed ? "ok" : "danger"}>{c.id}</Badge> {c.name}: <span className="text-muted">{c.detail}</span></li>
              ))}
            </ul>
          ) : <p className="text-sm text-muted">—</p>}
        </Card>
      </div>
      <Card title="Orders">
        <Table head={["Intent", "Type", "Qty", "Filled", "Limit", "Stop", "Avg fill", "Status", "Events"]}>
          {data.orders.map((o) => (
            <tr key={o.id} className="border-b border-border/60 align-top">
              <td className="px-2 py-1.5" title={o.client_order_id}>{o.intent}</td>
              <td className="px-2 py-1.5">{o.type}</td>
              <td className="num px-2 py-1.5">{o.qty}</td>
              <td className="num px-2 py-1.5">{o.filled_qty}</td>
              <td className="num px-2 py-1.5">{money(o.limit_price)}</td>
              <td className="num px-2 py-1.5">{money(o.stop_price)}</td>
              <td className="num px-2 py-1.5">{money(o.filled_avg_price)}</td>
              <td className="px-2 py-1.5"><Badge>{o.status}</Badge></td>
              <td className="px-2 py-1.5 text-xs text-muted">
                {o.events.map((e) => `${etDateTime(e.ts)} ${e.event} (${e.source})`).join(" · ")}
              </td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  );
}

interface DecisionRow {
  id: string; ts: string; symbol: string; strategy: string; confidence: number; decision_result: string;
  block_reasons: string[]; ai_mode: string; llm_verdict: string | null; reasoning: string; reasoning_llm: string | null;
}

export function DecisionsPage() {
  const [result, setResult] = useState("");
  const { data } = useGet<DecisionRow[]>(`/decisions?limit=200${result ? `&result=${result}` : ""}`, 15000);
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">AI decisions</h1>
      <p className="text-sm text-muted">
        Every proposal the strategies made, what the (optional) LLM said, and what the risk engine decided.
        Confidence is the model score, not a probability of profit.
      </p>
      <select className="rounded-md border border-border bg-surface-2 px-2 py-1 text-sm" value={result}
        onChange={(e) => setResult(e.target.value)} aria-label="Result filter">
        <option value="">all results</option>
        {["ORDER_SUBMITTED", "ORDER_FAILED", "RISK_REJECTED", "BLOCKED", "VETOED"].map((r) => <option key={r}>{r}</option>)}
      </select>
      <Card>
        <Table head={["Time (ET)", "Symbol", "Strategy", "Conf.", "Result", "Reasons", "AI", "Explanation"]} empty={!data?.length}>
          {data?.map((d) => (
            <tr key={d.id} className="border-b border-border/60 align-top">
              <td className="px-2 py-1.5">{etDateTime(d.ts)}</td>
              <td className="px-2 py-1.5 font-semibold">{d.symbol}</td>
              <td className="px-2 py-1.5">{d.strategy}</td>
              <td className="num px-2 py-1.5">{Math.round(d.confidence * 100)}%</td>
              <td className="px-2 py-1.5"><Badge tone={d.decision_result === "ORDER_SUBMITTED" ? "ok" : "warn"}>{d.decision_result}</Badge></td>
              <td className="px-2 py-1.5 text-xs">{d.block_reasons.join(", ")}</td>
              <td className="px-2 py-1.5 text-xs">{d.ai_mode}{d.llm_verdict ? ` · ${d.llm_verdict}` : ""}</td>
              <td className="px-2 py-1.5 text-xs whitespace-pre-line">{d.reasoning_llm ?? d.reasoning}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  );
}
