// Shared harness plumbing: one fixture daemon per worker, a logged-in page per
// test, and the evidence contract — any browser console error, uncaught
// exception/rejection, or CSP violation fails the test unless it explicitly
// allowed that exact kind of noise (e.g. the offline test's failed fetches).
import { test as base, expect } from "@playwright/test";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
// The project venv's interpreter, by exact path — no PATH search, so a
// writable directory earlier on PATH can never substitute the binary.
const python = join(repoRoot, ".venv", "bin", "python");

async function startFixture() {
  if (!existsSync(python)) {
    throw new Error(`no project venv at ${python} — run \`uv sync\` first`);
  }
  // stdin is the lifeline: the fixture exits on its EOF, so even a SIGKILLed
  // worker (no teardown, no signal forwarded) cannot leave an orphan behind.
  const child = spawn(python, [join(repoRoot, "tests", "browser", "fixture_server.py")], {
    cwd: repoRoot,
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const line = await new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`fixture server never announced itself\n${stderr}`)),
      30_000,
    );
    createInterface({ input: child.stdout }).once("line", (text) => {
      clearTimeout(timer);
      resolve(text);
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`fixture server exited with ${code}\n${stderr}`));
    });
  });
  return { child, ...JSON.parse(line) };
}

export const test = base.extend({
  // One fixture daemon per worker: parallel workers never share state.
  fixture: [
    async ({}, use) => {
      const fx = await startFixture();
      await use(fx);
      fx.child.kill("SIGINT");
    },
    { scope: "worker" },
  ],

  // Per-test evidence ledger. `evidence.allow(regex)` whitelists expected
  // noise for THIS test only (offline tests produce failed-fetch console
  // errors by design); everything else fails the test at teardown.
  evidence: async ({ page }, use) => {
    const entries = [];
    const allowed = [];
    page.on("console", (message) => {
      if (message.type() === "error") entries.push(`console: ${message.text()}`);
    });
    page.on("pageerror", (error) => entries.push(`pageerror: ${error.message}`));
    // CSP violations surface as console errors in Chromium, but the event is
    // the contract — capture it directly so a silenced console can't hide one.
    await page.addInitScript(() => {
      globalThis.addEventListener("securitypolicyviolation", (event) => {
        console.error(
          `csp-violation: ${event.violatedDirective} blocked ${event.blockedURI}`,
        );
      });
    });
    await use({
      entries,
      allow: (pattern) => allowed.push(pattern),
    });
    const offending = entries.filter(
      (entry) => !allowed.some((pattern) => pattern.test(entry)),
    );
    expect(offending, "browser evidence must be clean").toEqual([]);
  },

  // A page already signed in via the real single-use-token flow, with the
  // fixture reset to its seeded state. Destructuring `evidence` (even unused)
  // is the dependency declaration: the ledger instantiates before the first
  // navigation, so nothing escapes it.
  app: async ({ page, fixture, evidence: _evidence }, use) => {
    await fetch(`${fixture.control}/reset`, { method: "POST" });
    const res = await fetch(`${fixture.control}/login-url`);
    const { url } = await res.json();
    await page.goto(url);
    await expect(page.locator("#view")).toBeVisible();
    await use({ page, base: fixture.app, control: fixture.control });
  },
});

export { expect };

// The status poll runs every 3s; UI reactions to state changes land within one
// tick. Waits in specs use expect(...).toPass / auto-retrying assertions, so
// this constant only documents the cadence.
export const STATUS_TICK_MS = 3000;
