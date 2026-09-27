// Display helpers. Money arrives as decimal strings from the API.

export const num = (v: string | number | null | undefined): number | null =>
  v === null || v === undefined || v === "" ? null : Number(v);

export function money(v: string | number | null | undefined, signed = false): string {
  const n = num(v);
  if (n === null || Number.isNaN(n)) return "—";
  const s = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const sign = n < 0 ? "-" : signed && n > 0 ? "+" : "";
  return `${sign}$${s}`;
}

export function pct(v: number | string | null | undefined, digits = 2): string {
  const n = num(v);
  return n === null || Number.isNaN(n) ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(digits)}%`;
}

export const pnlClass = (v: string | number | null | undefined) => {
  const n = num(v);
  return n === null ? "" : n > 0 ? "text-ok" : n < 0 ? "text-danger" : "text-muted";
};

export const etTime = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" }) : "—";

export const etDateTime = (iso: string | null | undefined) =>
  iso
    ? new Date(iso).toLocaleString("en-US", {
        timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
      })
    : "—";

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const m = Math.floor(seconds / 60);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
}
