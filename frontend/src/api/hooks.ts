import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type {
  BacktestRun,
  BacktestTrade,
  CommandAccepted,
  Config,
  Decision,
  Evaluation,
  HistoryEntry,
  RegimeResponse,
  ScanResponse,
  SettingsResponse,
  Status,
  User,
} from "./types";

export const keys = {
  me: ["me"] as const,
  status: ["status"] as const,
  settings: ["settings"] as const,
  history: ["settings", "history"] as const,
  scanner: ["scanner"] as const,
};

export function useScanner() {
  return useQuery({ queryKey: keys.scanner, queryFn: () => api<ScanResponse>("/scanner"), refetchInterval: 30000 });
}

export function useRunScan() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api<CommandAccepted>("/scanner/run", { method: "POST" }),
    onSuccess: () => {
      setTimeout(() => void qc.invalidateQueries({ queryKey: keys.scanner }), 3000);
    },
  });
}

export function useRegime() {
  return useQuery({ queryKey: ["regime"], queryFn: () => api<RegimeResponse>("/regime"), refetchInterval: 15000 });
}

export function useRecentDecisions(limit = 5) {
  return useQuery({
    queryKey: ["decisions", limit],
    queryFn: () => api<Decision[]>(`/decisions?limit=${limit}`),
    refetchInterval: 15000,
  });
}

export function useBacktests() {
  return useQuery({
    queryKey: ["backtests"],
    queryFn: () => api<BacktestRun[]>("/backtests"),
    refetchInterval: (q) => (q.state.data?.some((r) => r.status === "QUEUED" || r.status === "RUNNING") ? 3000 : false),
  });
}

export function useBacktest(id: string | null) {
  return useQuery({ queryKey: ["backtest", id], queryFn: () => api<BacktestRun>(`/backtests/${id}`), enabled: !!id });
}

export function useBacktestTrades(id: string | null) {
  return useQuery({
    queryKey: ["backtest", id, "trades"],
    queryFn: () => api<BacktestTrade[]>(`/backtests/${id}/trades`),
    enabled: !!id,
  });
}

export function useCreateBacktest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Record<string, unknown>) => api<{ id: string }>("/backtests", { method: "POST", body }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["backtests"] }),
  });
}

export function useCancelBacktest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api<unknown>(`/backtests/${id}/cancel`, { method: "POST" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["backtests"] }),
  });
}

export function useMe() {
  return useQuery({ queryKey: keys.me, queryFn: () => api<User>("/auth/me"), retry: false });
}

export function useLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { email: string; password: string }) =>
      api<User>("/auth/login", { method: "POST", body }),
    onSuccess: (user) => qc.setQueryData(keys.me, user),
  });
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api<void>("/auth/logout", { method: "POST" }),
    onSettled: () => qc.clear(),
  });
}

export function useStatus() {
  // Polled (no WebSocket in V1; docs/design/05 §10 as-built note).
  return useQuery({ queryKey: keys.status, queryFn: () => api<Status>("/status"), refetchInterval: 5000 });
}

export function useControl() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (action: "start" | "pause") =>
      api<CommandAccepted>(`/controls/${action}`, { method: "POST" }),
    onSuccess: () => {
      // The engine processes commands within ~1 s; refresh shortly after.
      setTimeout(() => void qc.invalidateQueries({ queryKey: keys.status }), 1200);
    },
  });
}

export function useSettings() {
  return useQuery({ queryKey: keys.settings, queryFn: () => api<SettingsResponse>("/settings") });
}

export function useSettingsHistory(enabled: boolean) {
  return useQuery({
    queryKey: keys.history,
    queryFn: () => api<HistoryEntry[]>("/settings/history"),
    enabled,
  });
}

export function useValidateSettings() {
  return useMutation({
    mutationFn: (config: Config) => api<Evaluation>("/settings/validate", { method: "POST", body: { config } }),
  });
}

function useInvalidateSettings() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: keys.settings });
    void qc.invalidateQueries({ queryKey: keys.status });
  };
}

export function useSaveSettings() {
  const invalidate = useInvalidateSettings();
  return useMutation({
    mutationFn: (body: { base_version: number; config: Config; comment: string }) =>
      api<Evaluation & { version: number }>("/settings", { method: "PUT", body }),
    onSuccess: invalidate,
  });
}

export function useRollbackSettings() {
  const invalidate = useInvalidateSettings();
  return useMutation({
    mutationFn: (body: { version: number; base_version: number }) =>
      api<{ version: number }>("/settings/rollback", { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function useImportSettings() {
  const invalidate = useInvalidateSettings();
  return useMutation({
    mutationFn: (body: { yaml: string; base_version: number; comment: string }) =>
      api<{ version: number }>("/settings/import", { method: "POST", body }),
    onSuccess: invalidate,
  });
}

// ------------------------------------------------------------------ Phases 8–12

/** GET with optional polling; `path` null disables the query. */
export function useGet<T>(path: string | null, refetchInterval: number | false = false) {
  return useQuery({
    queryKey: ["get", path],
    queryFn: () => api<T>(path as string),
    enabled: path !== null,
    refetchInterval,
  });
}

function useCommand<B>(path: (b: B) => string, body?: (b: B) => unknown) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (b: B) =>
      api<CommandAccepted>(path(b), { method: "POST", body: body ? body(b) : {} }),
    onSuccess: () => {
      setTimeout(() => void qc.invalidateQueries(), 1500);
    },
  });
}

export function useEmergencyStop() {
  return useCommand<{ closePositions: boolean }>(
    () => "/controls/emergency-stop",
    (b) => ({ confirm: "EMERGENCY STOP", close_positions: b.closePositions }),
  );
}

export function useCloseAll() {
  return useCommand<void>(() => "/controls/close-all", () => ({ confirm: "CLOSE ALL" }));
}

export function useReconcileNow() {
  return useCommand<void>(() => "/controls/reconcile-now");
}

export function useClosePosition() {
  return useCommand<{ id: string; symbol: string }>(
    (b) => `/positions/${b.id}/close`,
    (b) => ({ confirm: b.symbol }),
  );
}

export function useAckAlert() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api<unknown>(`/alerts/${id}/ack`, { method: "POST" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["get"] }),
  });
}

export function useResetLatch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (b: { latch: string; password: string }) =>
      b.latch === "DAILY_LOSS_LIMIT"
        ? api<CommandAccepted>("/controls/reset-daily-lock", {
            method: "POST", body: { confirm: "RESET", password: b.password } })
        : api<CommandAccepted>("/controls/reset-latch", {
            method: "POST", body: { latch: b.latch, confirm: "RESET", password: b.password } }),
    onSuccess: () => {
      setTimeout(() => void qc.invalidateQueries(), 1500);
    },
  });
}
