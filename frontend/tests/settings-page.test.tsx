import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Evaluation, SettingsResponse } from "../src/api/types";
import { makeQueryClient } from "../src/app/App";
import { SettingsEditor } from "../src/pages/SettingsPage";

const data: SettingsResponse = {
  version: 3,
  created_at: "2026-09-26T14:00:00Z",
  created_by: "admin@example.com",
  comment: "test",
  is_active: true,
  engine_paused: false,
  running_version: 3,
  pending_restart: [],
  config: {
    risk: { max_daily_loss: "500", allow_short_selling: false },
    data: { feed: "iex" },
  },
  fields: [
    { key: "risk.max_daily_loss", group: "risk", type: "decimal", default: "500", policy: "PAUSED", loosen: "higher", description: "Daily loss that locks trading.", ge: 10, unit: "USD" },
    { key: "risk.allow_short_selling", group: "risk", type: "bool", default: false, policy: "LOCKED", description: "Long only.", options: [false] },
    { key: "data.feed", group: "data", type: "enum", default: "iex", policy: "RESTART", description: "Feed.", options: ["iex", "sip"] },
  ],
};

function renderEditor() {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <SettingsEditor data={data} />
    </QueryClientProvider>,
  );
}

describe("SettingsEditor", () => {
  it("renders fields from the schema with policy badges; locked fields are read-only", () => {
    renderEditor();
    expect(screen.getByLabelText(/Max daily loss/)).toHaveValue("500");
    expect(screen.getByText("pause to loosen")).toBeInTheDocument();
    expect(screen.getByLabelText(/Allow short selling/)).toBeDisabled();
    expect(screen.getByText(/Trading is enabled/)).toBeInTheDocument();
  });

  it("tracks unsaved changes and blocks saving a loosening change while running", async () => {
    const evaluation: Evaluation = {
      valid: true,
      errors: [],
      changes: [{ key: "risk.max_daily_loss", old: "500", new: "600", policy: "PAUSED", loosening: true, requires_pause: true }],
      requires_pause: true,
      blocked_by_running: true,
      pending_restart: [],
    };
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(evaluation), { status: 200, headers: { "content-type": "application/json" } }),
    );
    renderEditor();
    const input = screen.getByLabelText(/Max daily loss/);
    await userEvent.clear(input);
    await userEvent.type(input, "600");
    expect(screen.getByText("1 unsaved change(s)")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Review changes" }));
    await waitFor(() => expect(screen.getByText("loosens")).toBeInTheDocument());
    expect(screen.getByText(/Pause trading from the status bar/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save new version" })).toBeDisabled();

    const [url, init] = fetchSpy.mock.calls[0]!;
    expect(url).toBe("/api/v1/settings/validate");
    const sent = JSON.parse(String(init!.body)) as { config: { risk: { max_daily_loss: string } } };
    expect(sent.config.risk.max_daily_loss).toBe("600");
  });

  it("reverting an edit clears the unsaved change", async () => {
    renderEditor();
    const input = screen.getByLabelText(/Max daily loss/);
    await userEvent.type(input, "0");
    expect(screen.getByText("1 unsaved change(s)")).toBeInTheDocument();
    await userEvent.type(input, "{backspace}");
    expect(screen.getByText("No unsaved changes")).toBeInTheDocument();
  });
});
