import { useState, type ReactNode } from "react";
import { ApiError } from "../api/client";
import { useControl, useStatus } from "../api/hooks";
import type { DisplayStatus, Status } from "../api/types";
import { EmergencyControls, LatchReset } from "./EmergencyStop";
import { Badge, type Tone } from "./ui";

const STATUS_TONE: Record<DisplayStatus, Tone> = {
  RUNNING: "ok",
  PAUSED: "neutral",
  "MARKET CLOSED": "neutral",
  "DATA ERROR": "warn",
  "BROKER ERROR": "warn",
  "DAILY LOSS LIMIT": "danger",
  "EMERGENCY STOP": "danger",
};

export function statusLabel(s: Status): { label: string; tone: Tone } {
  if (!s.engine_online) return { label: "ENGINE OFFLINE", tone: "danger" };
  return { label: s.status, tone: STATUS_TONE[s.status] ?? "neutral" };
}

const etTime = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });
const etDay = (iso: string) =>
  new Date(iso).toLocaleDateString("en-US", { timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric" });

export function marketLabel(m: Status["market"] | undefined): string {
  if (!m || !m.session_date || !m.open_at || !m.close_at) return "Market: calendar unavailable";
  if (m.is_open) return `Market open · closes ${etTime(m.close_at)} ET${m.early_close ? " (early close)" : ""}`;
  return `Market closed · opens ${etDay(m.open_at)} ${etTime(m.open_at)} ET`;
}

export function StatusBarView({
  status,
  onControl,
  busy,
  error,
  extra,
}: {
  status: Status;
  onControl: (action: "start" | "pause") => void;
  busy: boolean;
  error?: string;
  extra?: ReactNode;
}) {
  const { label, tone } = statusLabel(status);
  const configError = status.blocks.some((b) => b.reason === "CONFIG_ERROR");
  const startDisabledReason = configError
    ? "Engine configuration error — see details"
    : status.latched.length
      ? `Reset required: ${status.latched.join(", ")}`
      : undefined;

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border bg-surface px-4 py-2 text-sm">
      <span className="font-semibold" data-testid="trading-mode">
        TRADING MODE: {status.trading_mode || "UNKNOWN"}
      </span>
      <details className="relative">
        <summary className="cursor-pointer list-none" aria-label="Status details">
          <Badge tone={tone}>● {label}</Badge>
        </summary>
        <div className="absolute left-0 z-20 mt-2 w-80 rounded-md border border-border bg-surface p-3 shadow-lg">
          {status.blocks.length === 0 ? (
            <p className="text-muted">No blocks — entries allowed.</p>
          ) : (
            <ul className="space-y-2">
              {status.blocks.map((b) => (
                <li key={b.reason}>
                  <div className="flex items-center gap-2">
                    <code className="text-xs font-semibold">{b.reason}</code>
                    {b.latched && <Badge tone="danger">latched</Badge>}
                  </div>
                  <p className="text-xs text-muted">{b.detail}</p>
                </li>
              ))}
            </ul>
          )}
        </div>
      </details>
      <span className="text-muted" data-testid="market">{marketLabel(status.market)}</span>
      <span className="text-muted">
        AI: {status.ai_mode ?? "—"} · Feed: {status.data_feed?.toUpperCase() ?? "—"} · Settings v
        {status.running_config_version ?? "—"}
        {status.pending_restart.length > 0 && (
          <Badge tone="warn" title={status.pending_restart.join(", ")}>
            restart pending
          </Badge>
        )}
      </span>
      <div className="ml-auto flex items-center gap-2">
        {error && <span className="text-xs text-danger">{error}</span>}
        {extra}
        {status.trading_enabled ? (
          <button
            type="button"
            className="rounded-md border border-border bg-surface-2 px-3 py-1.5 font-medium hover:bg-border/60 disabled:opacity-50"
            onClick={() => onControl("pause")}
            disabled={busy}
          >
            PAUSE
          </button>
        ) : (
          <button
            type="button"
            className="rounded-md bg-ok px-3 py-1.5 font-medium text-white hover:opacity-90 disabled:opacity-50"
            onClick={() => onControl("start")}
            disabled={busy || !!startDisabledReason}
            title={startDisabledReason}
          >
            START
          </button>
        )}
      </div>
    </div>
  );
}

export function StatusBar() {
  const { data, isError } = useStatus();
  const control = useControl();
  const [error, setError] = useState<string>();
  if (!data) {
    return (
      <div className="border-b border-border bg-surface px-4 py-2 text-sm text-muted">
        {isError ? "Status unavailable" : "Loading status…"}
      </div>
    );
  }
  return (
    <StatusBarView
      status={data}
      busy={control.isPending}
      error={error}
      extra={<><LatchReset latched={data.latched} /><EmergencyControls /></>}
      onControl={(action) => {
        setError(undefined);
        control.mutate(action, {
          onError: (e) => setError(e instanceof ApiError ? (e.detail ?? e.title) : "Request failed"),
        });
      }}
    />
  );
}
