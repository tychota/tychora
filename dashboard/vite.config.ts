import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/dashboard/",
  plugins: [react()],
  build: {
    outDir: "../src/tychora/resources/dashboard",
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    exclude: ["e2e/**", "node_modules/**"],
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
    // Async queries wait up to 5 s (src/test/setup.ts); a whole test gets more.
    testTimeout: 20000,
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/test/**"],
      reporter: ["text-summary", "json-summary"],
      thresholds: {
        statements: 81.5,
        branches: 76.8,
        functions: 73.3,
        lines: 83.4,
      },
    },
  },
});
