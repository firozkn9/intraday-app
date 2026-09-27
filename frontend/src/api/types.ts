// Response shapes of the Phase 1 API (docs/design/05).

export interface User {
  id: string;
  email: string;
  role: string;
}

export interface Block {
  reason: string;
  since: string | null;
  latched: boolean;
  detail: string;
}

export type DisplayStatus =
  | "RUNNING"
  | "PAUSED"
  | "DAILY LOSS LIMIT"
  | "MARKET CLOSED"
  | "DATA ERROR"
  | "BROKER ERROR"
  | "EMERGENCY STOP";

export interface MarketStatus {
  is_open: boolean;
  session_date: string | null;
  open_at: string | null;
  close_at: string | null;
  early_close: boolean | null;
  calendar_until: string | null;
  window?: { trading_start: string; no_new_trades_after: string; force_close: string; trading_end: string };
  data: {
    enabled: boolean;
    reason: string | null;
    feed: string | null;
    stream: string | null;
    subscribed: number | null;
    stale_symbols: string[];
  };
}

export interface Status {
  trading_mode: string;
  status: DisplayStatus;
  engine_online: boolean;
  engine_heartbeat_age_s: number | null;
  blocks: Block[];
  latched: string[];
  trading_enabled: boolean;
  running_config_version: number | null;
  active_config_version: number | null;
  pending_restart: string[];
  paper_guard: { ok: boolean; problems: string[] } | null;
  ai_mode: string | null;
  data_feed: string | null;
  engine_version: string | null;
  market: MarketStatus;
}

export type Policy = "HOT" | "PAUSED" | "PAUSED_ANY" | "RESTART" | "LOCKED";
export type FieldType = "bool" | "int" | "float" | "decimal" | "time" | "str" | "enum" | "list[str]";

export interface FieldSpec {
  key: string;
  group: string;
  type: FieldType;
  default: unknown;
  policy: Policy;
  description: string;
  loosen?: string;
  order?: string[];
  options?: (string | boolean)[];
  ge?: number;
  le?: number;
  gt?: number;
  lt?: number;
  unit?: string;
}

export type Config = Record<string, Record<string, unknown>>;

export interface SettingsResponse {
  version: number;
  created_at: string;
  created_by: string | null;
  comment: string;
  is_active: boolean;
  config: Config;
  fields: FieldSpec[];
  engine_paused: boolean;
  running_version: number | null;
  pending_restart: string[];
}

export interface Change {
  key: string;
  old: unknown;
  new: unknown;
  policy: Policy;
  loosening: boolean;
  requires_pause: boolean;
}

export interface Evaluation {
  valid: boolean;
  errors: { key: string; message: string }[];
  changes: Change[];
  requires_pause: boolean;
  blocked_by_running: boolean;
  pending_restart: string[];
}

export interface HistoryEntry {
  version: number;
  created_at: string;
  created_by: string | null;
  comment: string;
  is_active: boolean;
  changes: Change[];
}

export interface CommandAccepted {
  command_id: string;
  status: string;
}

export interface FilterReason {
  id: string;
  name: string;
  passed: boolean;
  detail: string;
  value: unknown;
  limit: unknown;
  evaluated: boolean;
}

export interface ScanRow {
  symbol: string;
  passed: boolean;
  failed: string[];
  reasons: Record<string, FilterReason>;
  metrics: {
    price: string | null;
    previous_close: string | null;
    change_pct: number | null;
    adv: number | null;
    adv_sessions: number;
    spread_pct: string | null;
    last_bar_at: string | null;
    bars_today: number;
    market_cap: string | null;
  };
  ts: string;
}

export interface ScanResponse {
  scan: {
    scan_id: string;
    ts: string;
    session_date: string;
    feed: string;
    config_version: number;
    passed: number;
    failed: number;
  } | null;
  results: ScanRow[];
}

export interface RegimeInfo {
  label: string;
  trend: string;
  high_volatility: boolean;
  vol_percentile: number | null;
  breadth: number | null;
  reasons: string[];
}

export interface RegimeResponse {
  bar_ts: string | null;
  regime: RegimeInfo | null;
  evaluated: string[];
  proposals: { symbol: string; strategy: string; score: number }[];
}

export interface Decision {
  id: string;
  ts: string;
  symbol: string;
  strategy: string;
  confidence: number;
  entry_ref_price: string;
  stop_loss: string;
  take_profit: string;
  risk_reward: number;
  signals: string[];
  reasoning: string;
  decision_result: string;
  block_reasons: string[];
}

export type Split = "TRAIN" | "VALIDATION" | "TEST";

export interface BacktestMetrics {
  sessions: number;
  starting_capital: number;
  final_equity: number;
  total_return_pct: number;
  annualized_return_pct: number | null;
  max_drawdown: number;
  max_drawdown_pct: number;
  sharpe: number | null;
  sortino: number | null;
  trades: number;
  win_rate_pct: number | null;
  profit_factor: number | null;
  net_pnl: number | null;
  fees: number | null;
  average_trade: number | null;
  average_win: number | null;
  average_loss: number | null;
  largest_win: number | null;
  largest_loss: number | null;
  largest_winning_streak: number;
  largest_losing_streak: number;
  average_holding_minutes: number | null;
  exit_reasons: Record<string, number>;
}

export interface BacktestRun {
  id: string;
  created_at: string;
  status: "QUEUED" | "RUNNING" | "DONE" | "FAILED" | "CANCELED";
  feed: string;
  params: Record<string, unknown>;
  summary: Partial<Record<Split, Partial<BacktestMetrics>>>;
  metrics?: Partial<Record<Split, BacktestMetrics>>;
  error: string | null;
}

export interface BacktestTrade {
  split: Split;
  session_date: string;
  symbol: string;
  strategy: string;
  qty: number;
  entry_price: string;
  entry_time: string;
  exit_price: string;
  exit_time: string;
  exit_reason: string;
  net_pnl: string;
}
