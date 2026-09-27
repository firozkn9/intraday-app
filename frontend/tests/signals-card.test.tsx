import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SignalsCard } from "../src/pages/DashboardPage";

describe("SignalsCard", () => {
  it("shows the regime and blocked proposals", () => {
    render(
      <SignalsCard
        regime={{ bar_ts: null, evaluated: ["AAPL"], proposals: [],
          regime: { label: "BULLISH", trend: "BULLISH", high_volatility: false, vol_percentile: 40, breadth: 0.62, reasons: [] } }}
        decisions={[{ id: "1", ts: "2026-09-21T14:10:00Z", symbol: "AAPL", strategy: "vwap_reclaim", confidence: 0.81,
          entry_ref_price: "200.12", stop_loss: "198.12", take_profit: "204.12", risk_reward: 2, signals: [],
          reasoning: "", decision_result: "BLOCKED", block_reasons: ["ENGINE_INCOMPLETE"] }]}
      />,
    );
    expect(screen.getByText("BULLISH")).toBeInTheDocument();
    expect(screen.getByText("breadth 62%")).toBeInTheDocument();
    expect(screen.getByText("VWAP Reclaim")).toBeInTheDocument();
    expect(screen.getByText("81%")).toBeInTheDocument();
    expect(screen.getByText("BLOCKED")).toHaveAttribute("title", "ENGINE_INCOMPLETE");
  });

  it("handles no data", () => {
    render(<SignalsCard />);
    expect(screen.getByText("No proposals yet.")).toBeInTheDocument();
  });
});
