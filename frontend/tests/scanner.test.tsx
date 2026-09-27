import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ScanRow } from "../src/api/types";
import { ScannerView } from "../src/pages/ScannerPage";

const metrics = {
  price: "229.2", previous_close: "229", change_pct: 0.087, adv: 60000, adv_sessions: 20,
  spread_pct: "0.0175", last_bar_at: null, bars_today: 30, market_cap: null,
};

const rows: ScanRow[] = [
  {
    symbol: "AAPL", passed: true, failed: [], ts: "2026-09-21T15:30:00Z", metrics,
    reasons: {
      U1: { id: "U1", name: "Enabled", passed: true, detail: "enabled on the watchlist", value: null, limit: null, evaluated: true },
      U5: { id: "U5", name: "Market cap", passed: true, detail: "not entered on the watchlist: filter skipped", value: null, limit: "2000000000", evaluated: false },
    },
  },
  {
    symbol: "TQQQ", passed: false, failed: ["U2"], ts: "2026-09-21T15:30:00Z", metrics: { ...metrics, adv: null },
    reasons: {
      U2: { id: "U2", name: "Eligible U.S. common stock", passed: false, detail: "fails E7 (name contains '3x')", value: ["E7"], limit: null, evaluated: true },
    },
  },
];

describe("ScannerView", () => {
  it("lists results with failed filter names", () => {
    render(<ScannerView rows={rows} onRun={() => {}} running={false} header={null} />);
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("pass")).toBeInTheDocument();
    expect(screen.getByText("fail")).toBeInTheDocument();
    expect(screen.getByText("Eligible U.S. common stock")).toBeInTheDocument();
    expect(screen.getByText("60,000")).toBeInTheDocument();
  });

  it("expands a row to show every filter's reason", async () => {
    render(<ScannerView rows={rows} onRun={() => {}} running={false} header={null} />);
    await userEvent.click(screen.getByText("TQQQ"));
    expect(screen.getByText("fails E7 (name contains '3x')")).toBeInTheDocument();
    await userEvent.click(screen.getByText("AAPL"));
    expect(screen.getByText("n/e")).toBeInTheDocument(); // not-evaluated filter
  });

  it("requests a scan", async () => {
    const onRun = vi.fn();
    render(<ScannerView rows={[]} onRun={onRun} running={false} header={null} />);
    expect(screen.getByText(/No scan yet/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run scan now" }));
    expect(onRun).toHaveBeenCalled();
  });
});
