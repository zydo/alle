// Real-browser smoke harness. Chromium-only on purpose: this is a smoke layer
// over real contracts (login, CSP, streams, lifetimes), not a cross-browser
// compatibility matrix. The fixture daemon is spawned per worker by
// tests/browser/support/fixtures.mjs — no shared webServer, no shared state.
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "tests/browser",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: [["list"]],
  timeout: 30_000,
  use: {
    trace: "retain-on-failure",
    // The stylesheet honors prefers-reduced-motion with animation: none —
    // emulating it makes every element instantly stable for click actions
    // (the modal's entrance animation otherwise starves actionability).
    reducedMotion: "reduce",
  },
});
