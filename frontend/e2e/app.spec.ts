import { expect, test, type Page } from "@playwright/test";

let page: Page;

const EMAIL = "admin@example.com";
const PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "correct-horse-battery-staple";

async function login(p: Page) {
  const page = p;
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("trading-mode")).toHaveText("TRADING MODE: PAPER");
}

async function csrf(page: Page): Promise<Record<string, string>> {
  const cookie = (await page.context().cookies()).find((c) => c.name === "csrf");
  return { "X-CSRF-Token": cookie?.value ?? "" };
}

// One engine, shared state: the steps run in order, in one logged-in session
// (logging in per test would trip the 5-per-minute login rate limit).
test.describe.serial("paper trading app", () => {
  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage();
    await login(page);
  });

  test.afterAll(async () => {
    await page.close();
  });

  test("login shows paper mode and the daily-loss lock", async () => {
    await page.goto("/");
    await expect(page.getByText("DAILY LOSS LIMIT").first()).toBeVisible();
    await expect(page.getByRole("button", { name: "START" })).toBeDisabled();
  });

  test("daily-loss lock is reset with the password, then START / PAUSE", async () => {
    await page.goto("/");
    await page.getByRole("button", { name: "Reset…" }).click();
    await page.getByLabel("Reset confirmation").fill("RESET");
    await page.getByRole("dialog").getByLabel("Password").fill(PASSWORD);
    await page.getByRole("dialog").getByRole("button", { name: "Reset", exact: true }).click();
    await expect(page.getByRole("button", { name: "START" })).toBeEnabled({ timeout: 20_000 });
    await page.getByRole("button", { name: "START" }).click();
    await expect(page.getByRole("button", { name: "PAUSE" })).toBeVisible({ timeout: 20_000 });
  });

  test("loosening a risk limit is refused while trading runs", async () => {
    await page.goto("/");
    const settings = await (await page.request.get("/api/v1/settings")).json();
    const config = structuredClone(settings.config);
    config.risk.max_trades_per_symbol_per_day = config.risk.max_trades_per_symbol_per_day + 1;
    const resp = await page.request.put("/api/v1/settings", {
      data: { base_version: settings.version, config, comment: "e2e loosen" },
      headers: await csrf(page),
    });
    expect(resp.status()).toBe(423);
    await page.getByRole("button", { name: "PAUSE" }).click();
    await expect(page.getByRole("button", { name: "START" })).toBeVisible({ timeout: 20_000 });
  });

  test("a replayed trade flows calendar -> day -> trade detail", async () => {
    await page.goto("/");
    await page.goto("/calendar?month=2026-09");
    await page.getByLabel("2026-09-21 PROFIT").click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByText("TAKE_PROFIT")).toBeVisible();
    await dialog.getByRole("link", { name: "AAA" }).click();
    await expect(page.getByRole("heading", { name: /AAA/ })).toBeVisible();
    await expect(page.getByText(/Explanation/)).toBeVisible();
    await expect(page.getByText("ENTRY", { exact: true })).toBeVisible();
    await expect(page.getByText("Risk checks (approved)")).toBeVisible();
  });

  test("trade history exports CSV", async () => {
    await page.goto("/");
    await page.goto("/history");
    await expect(page.getByText("1 trades")).toBeVisible();
    const csv = await page.request.get("/api/v1/trades/export.csv");
    expect(csv.headers()["content-type"]).toContain("text/csv");
    const text = await csv.text();
    expect(text.split("\n")[0]).toContain("symbol");
    expect(text).toContain("AAA");
    expect(text).toContain("TAKE_PROFIT");
  });

  test("emergency stop latches and blocks START", async () => {
    await page.goto("/");
    await page.getByRole("button", { name: "EMERGENCY STOP" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Confirmation phrase").fill("EMERGENCY STOP");
    await dialog.getByRole("button", { name: "EMERGENCY STOP" }).click();
    await expect(page.getByText("EMERGENCY STOP").first()).toBeVisible();
    await expect(page.getByRole("button", { name: "START" })).toBeDisabled({ timeout: 20_000 });
    await expect(page.getByRole("button", { name: "Reset…" })).toBeVisible();
  });
});
