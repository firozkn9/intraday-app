import { describe, expect, it } from "vitest";
import type { Config, FieldSpec } from "../src/api/types";
import { boundsText, fromInput, getPath, humanize, setPath, subgroup, toInput } from "../src/lib/settings";

const spec = (over: Partial<FieldSpec>): FieldSpec => ({
  key: "risk.x",
  group: "risk",
  type: "int",
  default: 0,
  policy: "HOT",
  description: "",
  ...over,
});

describe("settings helpers", () => {
  it("gets and sets dotted paths without mutating the original", () => {
    const cfg: Config = { strategies: { pullback: { enabled: true } } as never };
    const next = setPath(cfg, "strategies.pullback.enabled", false);
    expect(getPath(next, "strategies.pullback.enabled")).toBe(false);
    expect(getPath(cfg, "strategies.pullback.enabled")).toBe(true);
    expect(getPath(cfg, "nope.missing")).toBeUndefined();
  });

  it("converts between API values and inputs", () => {
    expect(toInput(spec({ type: "time" }), "09:35:00")).toBe("09:35");
    expect(toInput(spec({ type: "list[str]" }), ["a", "b"])).toBe("a, b");
    expect(fromInput(spec({ type: "list[str]" }), "a, b ,, c")).toEqual(["a", "b", "c"]);
    expect(fromInput(spec({ type: "int" }), "7")).toBe(7);
    expect(fromInput(spec({ type: "int" }), "7.5")).toBe("7.5"); // server reports the error
    expect(fromInput(spec({ type: "float" }), "0.25")).toBe(0.25);
    expect(fromInput(spec({ type: "decimal" }), "500.00")).toBe("500.00"); // money stays a string
    expect(fromInput(spec({ type: "bool" }), true)).toBe(true);
  });

  it("labels fields readably", () => {
    expect(humanize("risk.max_daily_loss")).toBe("Max daily loss");
    expect(humanize("universe.max_spread_percent_iex")).toBe("Max spread percent IEX");
    expect(humanize("strategies.vwap_reclaim.min_rvol")).toBe("Min RVOL");
    expect(subgroup(spec({ key: "strategies.vwap_reclaim.min_rvol" }))).toBe("VWAP reclaim");
    expect(subgroup(spec({ key: "risk.max_daily_loss" }))).toBeNull();
  });

  it("formats bounds", () => {
    expect(boundsText(spec({ ge: 1, le: 20 }))).toBe("1 – 20");
    expect(boundsText(spec({ ge: 100 }))).toBe("≥ 100");
    expect(boundsText(spec({}))).toBeNull();
  });
});
