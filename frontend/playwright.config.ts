// Phase 15 end-to-end suite: the real API + engine (SimBroker, replayed data) + the SPA.
//   npm run e2e            (starts everything itself; no Alpaca/Anthropic keys are used)
// Set E2E_DATABASE_SERVER=postgresql://postgres:test@127.0.0.1:55432/postgres to use the
// compose.test.yaml Postgres instead of an embedded one.
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: { baseURL: "http://localhost:5173", trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "uv run python scripts/e2e_stack.py",
      cwd: "../backend",
      url: "http://127.0.0.1:8000/api/v1/health/ready",
      timeout: 240_000,
      reuseExistingServer: !process.env.CI,
      stdout: "ignore",
      stderr: "pipe",
    },
    {
      command: "npm run dev",
      url: "http://localhost:5173",
      timeout: 60_000,
      reuseExistingServer: !process.env.CI,
    },
  ],
});
