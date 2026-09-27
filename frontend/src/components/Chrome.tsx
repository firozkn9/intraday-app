// App-wide chrome required on every page: paper banner (SPEC §37) and
// risk disclaimer (SPEC §48).

export function PaperBanner() {
  return (
    <div
      role="status"
      className="sticky top-0 z-30 bg-paper-bg text-paper border-b border-paper/40 px-4 py-1.5 text-center text-sm font-bold tracking-wide"
    >
      PAPER TRADING — NO REAL MONEY
    </div>
  );
}

export const DISCLAIMER =
  "Trading involves substantial risk. This system does not guarantee profits or prevent losses. " +
  "Paper-trading results may differ materially from live trading.";

export function Disclaimer() {
  return (
    <footer className="border-t border-border px-4 py-3 text-xs text-muted">{DISCLAIMER}</footer>
  );
}
