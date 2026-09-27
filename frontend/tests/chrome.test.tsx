import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Status } from "../src/api/types";
import { Disclaimer, PaperBanner } from "../src/components/Chrome";
import { StatusBarView } from "../src/components/StatusBar";

const base: Status = {
  trading_mode: "PAPER",
  status: "PAUSED",
  engine_online: true,
  engine_heartbeat_age_s: 2,
  blocks: [
    { reason: "ENGINE_INCOMPLETE", since: null, latched: false, detail: "The trading pipeline is not built yet." },
    { reason: "PAUSED_BY_USER", since: null, latched: false, detail: "Trading is paused." },
  ],
  latched: [],
  trading_enabled: false,
  running_config_version: 1,
  active_config_version: 1,
  pending_restart: [],
  paper_guard: { ok: true, problems: [] },
  ai_mode: "OFF",
  data_feed: "iex",
  engine_version: "0.1.0",
  market: {
    is_open: true,
    session_date: "2026-11-27",
    open_at: "2026-11-27T14:30:00Z",
    close_at: "2026-11-27T18:00:00Z",
    early_close: true,
    calendar_until: "2026-12-31",
    data: { enabled: false, reason: "keys not set", feed: null, stream: null, subscribed: null, stale_symbols: [] },
  },
};

describe("app chrome", () => {
  it("shows the paper banner and the risk disclaimer", () => {
    render(
      <>
        <PaperBanner />
        <Disclaimer />
      </>,
    );
    expect(screen.getByText("PAPER TRADING — NO REAL MONEY")).toBeInTheDocument();
    expect(screen.getByText(/does not guarantee profits or prevent losses/)).toBeInTheDocument();
  });

  it("shows trading mode, status, and block reasons", () => {
    render(<StatusBarView status={base} onControl={() => {}} busy={false} />);
    expect(screen.getByTestId("trading-mode")).toHaveTextContent("TRADING MODE: PAPER");
    expect(screen.getByText("● PAUSED")).toBeInTheDocument();
    expect(screen.getByText("ENGINE_INCOMPLETE")).toBeInTheDocument();
  });

  it("starts trading when paused", async () => {
    const onControl = vi.fn();
    render(<StatusBarView status={base} onControl={onControl} busy={false} />);
    await userEvent.click(screen.getByRole("button", { name: "START" }));
    expect(onControl).toHaveBeenCalledWith("start");
  });

  it("offers PAUSE when trading is enabled", async () => {
    const onControl = vi.fn();
    render(<StatusBarView status={{ ...base, trading_enabled: true }} onControl={onControl} busy={false} />);
    await userEvent.click(screen.getByRole("button", { name: "PAUSE" }));
    expect(onControl).toHaveBeenCalledWith("pause");
  });

  it("disables START while a latch is active", () => {
    const latched: Status = { ...base, status: "DAILY LOSS LIMIT", latched: ["DAILY_LOSS_LIMIT"] };
    render(<StatusBarView status={latched} onControl={() => {}} busy={false} />);
    const start = screen.getByRole("button", { name: "START" });
    expect(start).toBeDisabled();
    expect(start).toHaveAttribute("title", "Reset required: DAILY_LOSS_LIMIT");
  });

  it("shows the market session in ET, including early closes", () => {
    render(<StatusBarView status={base} onControl={() => {}} busy={false} />);
    expect(screen.getByTestId("market")).toHaveTextContent("Market open · closes 1:00 PM ET (early close)");
  });

  it("shows the next open when closed", () => {
    const closed: Status = { ...base, market: { ...base.market, is_open: false, early_close: false } };
    render(<StatusBarView status={closed} onControl={() => {}} busy={false} />);
    expect(screen.getByTestId("market")).toHaveTextContent("Market closed · opens Fri, Nov 27 9:30 AM ET");
  });

  it("reports an offline engine", () => {
    render(<StatusBarView status={{ ...base, engine_online: false }} onControl={() => {}} busy={false} />);
    expect(screen.getByText("● ENGINE OFFLINE")).toBeInTheDocument();
  });
});
