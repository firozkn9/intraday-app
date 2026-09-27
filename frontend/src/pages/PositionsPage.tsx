import { useState } from "react";
import { Link } from "react-router";
import { ApiError } from "../api/client";
import { useClosePosition, useGet } from "../api/hooks";
import { Table } from "../components/LineChart";
import { Alert, Badge, Button, Card, Modal } from "../components/ui";
import { duration, money, pct, pnlClass } from "../lib/format";

export interface PositionRow {
  id: string;
  symbol: string;
  status: string;
  qty: number;
  avg_entry: string | null;
  current: string | null;
  unrealized_pnl: string | null;
  pnl_pct: number | null;
  stop: string | null;
  target: string | null;
  confidence: number | null;
  strategy: string | null;
  holding_seconds: number | null;
  trailing_active: boolean;
  exit_reason?: string | null;
  net_pnl?: string | null;
}

export function PositionsTable({ rows, onClose }: { rows: PositionRow[]; onClose?: (p: PositionRow) => void }) {
  return (
    <Table
      head={["Symbol", "Status", "Qty", "Entry", "Last", "Unrealized", "Stop", "Target", "Conf.", "Strategy", "Held", ""]}
      empty={rows.length === 0}
    >
      {rows.map((p) => (
        <tr key={p.id} className="border-b border-border/60">
          <td className="px-2 py-1.5 font-semibold">
            <Link to={`/history/${p.id}`} className="hover:underline">{p.symbol}</Link>
          </td>
          <td className="px-2 py-1.5">
            <Badge tone={p.status === "OPEN" ? "ok" : p.status === "EXITING" ? "warn" : "neutral"}>{p.status}</Badge>
          </td>
          <td className="num px-2 py-1.5">{p.qty}</td>
          <td className="num px-2 py-1.5">{money(p.avg_entry)}</td>
          <td className="num px-2 py-1.5">{money(p.current)}</td>
          <td className={`num px-2 py-1.5 ${pnlClass(p.unrealized_pnl)}`}>
            {money(p.unrealized_pnl, true)} <span className="text-xs">{pct(p.pnl_pct)}</span>
          </td>
          <td className="num px-2 py-1.5">
            {money(p.stop)} {p.trailing_active && <Badge tone="accent">trail</Badge>}
          </td>
          <td className="num px-2 py-1.5">{money(p.target)}</td>
          <td className="num px-2 py-1.5">{p.confidence != null ? `${Math.round(p.confidence * 100)}%` : "—"}</td>
          <td className="px-2 py-1.5">{p.strategy ?? "—"}</td>
          <td className="px-2 py-1.5">{duration(p.holding_seconds)}</td>
          <td className="px-2 py-1.5 text-right">
            {onClose && p.status === "OPEN" && (
              <Button variant="danger" onClick={() => onClose(p)}>Close</Button>
            )}
          </td>
        </tr>
      ))}
    </Table>
  );
}

export function ClosePositionModal({ position, onDone }: { position: PositionRow | null; onDone: () => void }) {
  const close = useClosePosition();
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string>();
  if (!position) return null;
  return (
    <Modal
      title={`Close ${position.symbol}`}
      open
      onClose={onDone}
      footer={
        <>
          <Button onClick={onDone}>Cancel</Button>
          <Button
            variant="danger"
            disabled={typed.trim().toUpperCase() !== position.symbol || close.isPending}
            onClick={() =>
              close.mutate(
                { id: position.id, symbol: position.symbol },
                {
                  onSuccess: onDone,
                  onError: (e) => setError(e instanceof ApiError ? (e.detail ?? e.title) : "Request failed"),
                },
              )
            }
          >
            Close position
          </Button>
        </>
      }
    >
      <p className="text-sm">
        The engine cancels the protective orders and sells {position.qty} shares at market (exit reason MANUAL).
      </p>
      <label className="mt-3 block text-sm">
        Type <strong>{position.symbol}</strong> to confirm
        <input className="mt-1 w-full rounded-md border border-border bg-surface-2 px-2 py-1"
          value={typed} onChange={(e) => setTyped(e.target.value)} aria-label="Confirm symbol" />
      </label>
      {error && <Alert tone="danger">{error}</Alert>}
    </Modal>
  );
}

export function PositionsPage() {
  const { data, isLoading } = useGet<PositionRow[]>("/positions", 5000);
  const [closing, setClosing] = useState<PositionRow | null>(null);
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Positions</h1>
      <Card title="Open positions">
        {isLoading ? <p className="text-muted">Loading…</p> : <PositionsTable rows={data ?? []} onClose={setClosing} />}
      </Card>
      <ClosePositionModal position={closing} onDone={() => setClosing(null)} />
    </div>
  );
}
