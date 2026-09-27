import { Fragment, useState } from "react";
import { ApiError } from "../api/client";
import { useRunScan, useScanner } from "../api/hooks";
import type { ScanRow } from "../api/types";
import { Alert, Badge, Button, Card } from "../components/ui";

const fmtInt = (n: number | null) => (n == null ? "—" : n.toLocaleString("en-US"));
const fmtTime = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });

export function ScannerView({ rows, onRun, running, header }: {
  rows: ScanRow[];
  onRun: () => void;
  running: boolean;
  header: React.ReactNode;
}) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">Market Scanner</h1>
        {header}
        <Button className="ml-auto" variant="primary" onClick={onRun} disabled={running}>
          {running ? "Scan requested…" : "Run scan now"}
        </Button>
      </div>
      <p className="text-sm text-muted">
        Universe filters applied to every tradable watchlist symbol. Context symbols (SPY, QQQ, sector ETFs) are
        market inputs only and are never scanned. Filters marked “not evaluated” are re-checked at order time.
      </p>
      <Card>
        {rows.length === 0 ? (
          <p className="text-sm text-muted">No scan yet. The engine scans at startup, at 09:25 ET, and during the session.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs text-muted">
                <tr>
                  <th className="py-1.5 pr-3 font-medium">Symbol</th>
                  <th className="py-1.5 pr-3 font-medium">Result</th>
                  <th className="py-1.5 pr-3 text-right font-medium">Price</th>
                  <th className="py-1.5 pr-3 text-right font-medium">Chg %</th>
                  <th className="py-1.5 pr-3 text-right font-medium">ADV</th>
                  <th className="py-1.5 pr-3 text-right font-medium">Spread %</th>
                  <th className="py-1.5 font-medium">Failed filters</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {rows.map((r) => (
                  <Fragment key={r.symbol}>
                    <tr
                      className="cursor-pointer hover:bg-surface-2"
                      onClick={() => setOpen(open === r.symbol ? null : r.symbol)}
                      aria-expanded={open === r.symbol}
                    >
                      <td className="py-1.5 pr-3 font-semibold">{r.symbol}</td>
                      <td className="py-1.5 pr-3">
                        <Badge tone={r.passed ? "ok" : "danger"}>{r.passed ? "pass" : "fail"}</Badge>
                      </td>
                      <td className="num py-1.5 pr-3 text-right">{r.metrics.price ?? "—"}</td>
                      <td className="num py-1.5 pr-3 text-right">
                        {r.metrics.change_pct == null ? "—" : `${r.metrics.change_pct > 0 ? "+" : ""}${r.metrics.change_pct.toFixed(2)}`}
                      </td>
                      <td className="num py-1.5 pr-3 text-right">{fmtInt(r.metrics.adv)}</td>
                      <td className="num py-1.5 pr-3 text-right">{r.metrics.spread_pct ?? "—"}</td>
                      <td className="py-1.5 text-xs text-muted">
                        {r.failed.length ? r.failed.map((id) => r.reasons[id]?.name ?? id).join(", ") : "—"}
                      </td>
                    </tr>
                    {open === r.symbol && (
                      <tr>
                        <td colSpan={7} className="bg-surface-2 px-3 py-2">
                          <ul className="space-y-1 text-xs">
                            {Object.values(r.reasons).map((f) => (
                              <li key={f.id} className="flex gap-2">
                                <Badge tone={!f.evaluated ? "neutral" : f.passed ? "ok" : "danger"}>
                                  {!f.evaluated ? "n/e" : f.passed ? "✓" : "✗"}
                                </Badge>
                                <span className="w-44 shrink-0 font-medium">
                                  {f.id} {f.name}
                                </span>
                                <span className="text-muted">{f.detail}</span>
                              </li>
                            ))}
                          </ul>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

export function ScannerPage() {
  const scanner = useScanner();
  const run = useRunScan();
  if (scanner.isPending) return <p className="text-muted">Loading scan…</p>;
  if (scanner.error) return <Alert tone="danger">Could not load the scan.</Alert>;
  const scan = scanner.data.scan;
  return (
    <>
      {run.error && (
        <div className="mb-3">
          <Alert tone="danger">{run.error instanceof ApiError ? run.error.title : "Request failed"}</Alert>
        </div>
      )}
      <ScannerView
        rows={scanner.data.results}
        running={run.isPending}
        onRun={() => run.mutate()}
        header={
          scan && (
            <span className="text-xs text-muted">
              {scan.session_date} · {fmtTime(scan.ts)} ET · {scan.feed.toUpperCase()} · {scan.passed} pass /{" "}
              {scan.failed} fail
            </span>
          )
        }
      />
    </>
  );
}
