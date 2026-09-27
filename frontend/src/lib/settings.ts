import type { Config, FieldSpec, Policy } from "../api/types";

export const GROUP_LABELS: Record<string, string> = {
  risk: "Capital & risk",
  exits: "Exits",
  hours: "Trading hours (ET)",
  universe: "Universe",
  strategies: "Strategies",
  scoring: "Scoring",
  regime: "Market regime",
  ai: "AI",
  data: "Market data",
  news: "News",
  execution: "Execution",
  alerts: "Alerts",
};

export const POLICY_TEXT: Record<Policy, string> = {
  HOT: "Applies on the next cycle",
  PAUSED: "Loosening requires trading to be paused",
  PAUSED_ANY: "Any change requires trading to be paused",
  RESTART: "Applies on engine restart",
  LOCKED: "Locked — cannot be changed",
};

export function humanize(key: string): string {
  const last = key.split(".").pop() ?? key;
  const words = last.replace(/_/g, " ");
  return (words.charAt(0).toUpperCase() + words.slice(1))
    .replace(/\bai\b/gi, "AI")
    .replace(/\bvwap\b/gi, "VWAP")
    .replace(/\brvol\b/gi, "RVOL")
    .replace(/\brsi\b/gi, "RSI")
    .replace(/\batr\b/gi, "ATR")
    .replace(/\bema(\d*)\b/gi, "EMA$1")
    .replace(/\biex\b/gi, "IEX")
    .replace(/\bsip\b/gi, "SIP")
    .replace(/\bbps\b/gi, "bps");
}

export function getPath(config: Config, key: string): unknown {
  let node: unknown = config;
  for (const part of key.split(".")) {
    if (node === null || typeof node !== "object") return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return node;
}

export function setPath(config: Config, key: string, value: unknown): Config {
  const parts = key.split(".");
  const clone = structuredClone(config) as Record<string, unknown>;
  let node = clone;
  for (const part of parts.slice(0, -1)) {
    node[part] = { ...((node[part] as Record<string, unknown>) ?? {}) };
    node = node[part] as Record<string, unknown>;
  }
  node[parts[parts.length - 1]!] = value;
  return clone as Config;
}

/** Normalise API values for form inputs ("09:35:00" → "09:35", lists → "a, b"). */
export function toInput(spec: FieldSpec, value: unknown): string | boolean {
  if (spec.type === "bool") return Boolean(value);
  if (spec.type === "time" && typeof value === "string") return value.slice(0, 5);
  if (spec.type === "list[str]" && Array.isArray(value)) return value.join(", ");
  return value === null || value === undefined ? "" : String(value);
}

/** Convert form input back to the API's JSON type. Unparseable numbers are sent
 * as-is so the server returns a precise validation error. */
export function fromInput(spec: FieldSpec, raw: string | boolean): unknown {
  switch (spec.type) {
    case "bool":
      return Boolean(raw);
    case "int": {
      const s = String(raw).trim();
      return /^-?\d+$/.test(s) ? Number.parseInt(s, 10) : s;
    }
    case "float": {
      const s = String(raw).trim();
      const n = Number(s);
      return s !== "" && Number.isFinite(n) ? n : s;
    }
    case "list[str]":
      return String(raw)
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean);
    default:
      return String(raw); // decimal, time, str, enum
  }
}

export function formatValue(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ") || "(none)";
  if (typeof value === "boolean") return value ? "on" : "off";
  if (value === null || value === undefined || value === "") return "(empty)";
  return String(value);
}

export function boundsText(spec: FieldSpec): string | null {
  const lo = spec.ge ?? spec.gt;
  const hi = spec.le ?? spec.lt;
  if (lo === undefined && hi === undefined) return null;
  const fmt = (n: number) => (Number.isInteger(n) ? n.toLocaleString("en-US") : String(n));
  if (lo !== undefined && hi !== undefined) return `${fmt(lo)} – ${fmt(hi)}`;
  return lo !== undefined ? `≥ ${fmt(lo)}` : `≤ ${fmt(hi!)}`;
}

/** Sub-section heading for nested groups (strategies.momentum_breakout.x → "Momentum breakout"). */
export function subgroup(spec: FieldSpec): string | null {
  const parts = spec.key.split(".");
  return parts.length > 2 ? humanize(parts[1]!) : null;
}
