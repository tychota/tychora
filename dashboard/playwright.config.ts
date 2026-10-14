import { defineConfig } from "@playwright/test";

// Optional executable override supports a preinstalled browser without machine-specific paths.
// Playwright sets FORCE_COLOR for workers; remove a conflicting inherited setting.
delete process.env.NO_COLOR;
const port = Number(process.env.TYCHORA_E2E_PORT ?? 8765);
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  outputDir: "../.superpowers/verification/dashboard-e2e/test-results",
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    actionTimeout: 10_000,
    viewport: { width: 1440, height: 1000 },
    serviceWorkers: "block",
    trace: "off", // Keep credentials out of browser/network recording.
    screenshot: "off", // Screenshots are taken only after the token input has cleared.
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH }
      : {},
  },
  webServer: {
    command: `${process.env.TYCHORA_E2E_PYTHON ?? "python"} e2e/server.py --port ${port}`,
    url: `http://127.0.0.1:${port}/dashboard/`,
    env: { PYTHONPATH: "../src" },
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
