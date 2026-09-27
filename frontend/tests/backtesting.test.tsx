import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { BacktestMetrics } from "../src/api/types";
import { MetricsTable } from "../src/pages/BacktestingPage";

const m = (over: Partial<BacktestMetrics>): BacktestMetrics => ({
  sessions: 20, starting_capital: 100000, final_equity: 101000, total_return_pct: 1, annualized_return_pct: 13.3,
  max_drawdown: 400, max_drawdown_pct: 0.4, sharpe: 1.2, sortino: 1.8, trades: 12, win_rate_pct: 58.3,
  profit_factor: 1.4, net_pnl: 1000, fees: 3.2, average_trade: 83.3, average_win: 210, average_loss: -95,
  largest_win: 400, largest_loss: -101, largest_winning_streak: 3, largest_losing_streak: 2,
  average_holding_minutes: 34.5, exit_reasons: {}, ...over,
});

describe("MetricsTable", () => {
  it("shows splits side by side and labels the out-of-sample column", () => {
    render(<MetricsTable metrics={{ TRAIN: m({}), TEST: m({ total_return_pct: -0.5, sharpe: null }) }} />);
    expect(screen.getByText("TRAIN")).toBeInTheDocument();
    expect(screen.getByText("TEST (out-of-sample)")).toBeInTheDocument();
    expect(screen.queryByText("VALIDATION")).not.toBeInTheDocument();
    expect(screen.getByText("-0.50%")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument(); // missing Sharpe
    expect(screen.getAllByText("58.3%")).toHaveLength(2);
  });
});
