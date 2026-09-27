import { useState } from "react";
import { useAckAlert, useGet, useReconcileNow } from "../api/hooks";
import { Table } from "../components/LineChart";
import { Badge, Button, Card, type Tone } from "../components/ui";
import { etDateTime } from "../lib/format";

interface AlertRow {
  id: string; ts: string; severity: string; type: string; message: string; count: number;
  acknowledged_at: string | null; channels_sent: string[];
}

const SEV: Record<string, Tone> = { INFO: "neutral", WARN: "warn", HIGH: "danger", CRITICAL: "danger", ERROR: "danger", DEBUG: "neutral" };

export function AlertsCard() {
  const [state, setState] = useState<"open" | "all">("open");
  const { data } = useGet<AlertRow[]>(`/alerts?state=${state}`, 15000);
  const ack = useAckAlert();
  return (
    <Card title="Alerts" actions={
      <select className="rounded-md border border-border bg-surface-2 px-2 py-1 text-sm" value={state}
        onChange={(e) => setState(e.target.value as "open" | "all")} aria-label="Alert state">
        <option value="open">open</option><option value="all">all</option>
      </select>}>
      <Table head={["When", "Severity", "Type", "Message", "Count", ""]} empty={!data?.length}>
        {data?.map((a) => (
          <tr key={a.id} className="border-b border-border/60 align-top">
            <td className="px-2 py-1.5">{etDateTime(a.ts)}</td>
            <td className="px-2 py-1.5"><Badge tone={SEV[a.severity] ?? "neutral"}>{a.severity}</Badge></td>
            <td className="px-2 py-1.5 text-xs">{a.type}</td>
            <td className="px-2 py-1.5">{a.message}</td>
            <td className="num px-2 py-1.5">{a.count}</td>
            <td className="px-2 py-1.5">
              {!a.acknowledged_at && <Button onClick={() => ack.mutate(a.id)}>Acknowledge</Button>}
            </td>
          </tr>
        ))}
      </Table>
    </Card>
  );
}

type Health = Record<string, unknown> & {
  engine: { heartbeat_age_s: number | null; status: string | null };
  broker: Record<string, unknown> | null;
  market_data: Record<string, unknown> | null;
  ai: Record<string, unknown>;
  database: { latency_ms: number };
  redis: string;
  orders: Record<string, number | null>;
  disk: { free_gb: number; used_pct: number };
  last_backup: string | null;
};

function KV({ data }: { data: Record<string, unknown> | null | undefined }) {
  if (!data) return <p className="text-sm text-muted">Not available.</p>;
  return (
    <dl className="grid grid-cols-2 gap-y-1 text-sm">
      {Object.entries(data).filter(([, v]) => typeof v !== "object" || v === null).map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted">{k.replaceAll("_", " ")}</dt>
          <dd className="num break-all">{v === null || v === undefined ? "—" : String(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

export function HealthPage() {
  const { data } = useGet<Health>("/system/health", 10000);
  const reconcile = useReconcileNow();
  const last = (data?.broker?.last_reconcile ?? null) as Record<string, unknown> | null;
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">System health</h1>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        <Card title="Engine"><KV data={data?.engine} /></Card>
        <Card title="Broker" actions={<Button onClick={() => reconcile.mutate()} disabled={reconcile.isPending}>Reconcile now</Button>}>
          <KV data={data?.broker} />
          {last && <p className="mt-2 text-xs text-muted">Last reconcile {etDateTime(String(last.ts))}: {last.clean ? "clean" : "differences found"}</p>}
        </Card>
        <Card title="Market data"><KV data={data?.market_data} /></Card>
        <Card title="AI provider"><KV data={data?.ai} /></Card>
        <Card title="Orders (24 h)"><KV data={data?.orders} /></Card>
        <Card title="Infrastructure">
          <KV data={data ? { db_latency_ms: data.database.latency_ms, redis: data.redis, disk_free_gb: data.disk.free_gb,
            disk_used_pct: data.disk.used_pct, last_backup: data.last_backup } : null} />
        </Card>
      </div>
      <AlertsCard />
    </div>
  );
}

interface LogRow { ts: string; kind: string; component: string; type: string; severity: string; message: string }

export function LogsPage() {
  const [severity, setSeverity] = useState("");
  const [component, setComponent] = useState("");
  const [q, setQ] = useState("");
  const qs = new URLSearchParams(Object.entries({ severity, component, q }).filter(([, v]) => v));
  const { data } = useGet<LogRow[]>(`/logs?${qs.toString()}&limit=300`, 15000);
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Logs</h1>
      <div className="flex flex-wrap gap-2 text-sm">
        <select className="rounded-md border border-border bg-surface-2 px-2 py-1" value={severity}
          onChange={(e) => setSeverity(e.target.value)} aria-label="Severity">
          <option value="">all severities</option>
          {["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"].map((s) => <option key={s}>{s}</option>)}
        </select>
        <select className="rounded-md border border-border bg-surface-2 px-2 py-1" value={component}
          onChange={(e) => setComponent(e.target.value)} aria-label="Component">
          <option value="">all components</option>
          {["engine", "trading", "market_data", "risk", "api"].map((s) => <option key={s}>{s}</option>)}
        </select>
        <input className="rounded-md border border-border bg-surface-2 px-2 py-1" placeholder="search" value={q}
          onChange={(e) => setQ(e.target.value)} aria-label="Search" />
      </div>
      <Card>
        <Table head={["When", "Severity", "Component", "Type", "Message"]} empty={!data?.length}>
          {data?.map((l, i) => (
            <tr key={`${l.ts}${i}`} className="border-b border-border/60">
              <td className="px-2 py-1.5 whitespace-nowrap">{etDateTime(l.ts)}</td>
              <td className="px-2 py-1.5"><Badge tone={SEV[l.severity] ?? "neutral"}>{l.severity}</Badge></td>
              <td className="px-2 py-1.5">{l.component}</td>
              <td className="px-2 py-1.5 text-xs">{l.type}</td>
              <td className="px-2 py-1.5">{l.message}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  );
}
