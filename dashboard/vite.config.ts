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
  },
});
