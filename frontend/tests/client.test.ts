import { describe, expect, it, vi } from "vitest";
import { api, ApiError, readCookie } from "../src/api/client";

function mockFetch(status: number, body: unknown, type = "application/json") {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
    new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": type } }),
  );
}

describe("api client", () => {
  it("sends the CSRF cookie as a header on unsafe methods only", async () => {
    document.cookie = "csrf=tok%3D123; path=/";
    const f = mockFetch(200, { ok: true });
    await api("/controls/pause", { method: "POST" });
    await api("/status");
    const [, post] = f.mock.calls[0]!;
    const [, get] = f.mock.calls[1]!;
    expect((post!.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok=123");
    expect((get!.headers as Record<string, string>)["X-CSRF-Token"]).toBeUndefined();
    expect(post!.credentials).toBe("same-origin");
  });

  it("parses problem+json into ApiError with field errors", async () => {
    mockFetch(
      423,
      { title: "Pause trading to apply these changes", status: 423, errors: [{ key: "risk.max_daily_loss", message: "requires trading to be paused" }] },
      "application/problem+json",
    );
    const err = await api("/settings", { method: "PUT", body: {} }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(423);
    expect((err as ApiError).errors[0]!.key).toBe("risk.max_daily_loss");
  });

  it("returns undefined for 204", async () => {
    mockFetch(204, null);
    await expect(api("/auth/logout", { method: "POST" })).resolves.toBeUndefined();
  });

  it("reads cookies", () => {
    document.cookie = "csrf=abc; path=/";
    expect(readCookie("csrf")).toBe("abc");
    expect(readCookie("missing")).toBeUndefined();
  });
});
