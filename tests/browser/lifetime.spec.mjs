// Acceptance for generation-scoped async lifetimes and single-flight
// mutations. The shared evidence fixture already fails any uncaught
// exception/rejection, so an ABA navigation that leaves a stale callback
// touching torn-down DOM fails here even without an explicit assertion.
import { test, expect } from "./support/fixtures.mjs";

const DE = '.row.dashchan.body[data-id="germany_1"]';

// Delay matching requests by `ms` without changing their result.
async function delay(page, urlPart, ms, { method } = {}) {
  await page.route(`**${urlPart}**`, async (route) => {
    if (method && route.request().method() !== method) return route.fallback();
    await new Promise((resolve) => setTimeout(resolve, ms));
    return route.fallback();
  });
}

test("ABA: a probe still in flight when the page unmounts has no effects", async ({
  app,
}) => {
  const { page } = app;
  await delay(page, "/api/v1/test", 1500);
  // the observable teardown signal: unmount must abort the in-flight probe,
  // which surfaces as the request failing (a build that leaks the request
  // past unmount never fails it, and this wait times the test out instead)
  const aborted = page.waitForEvent("requestfailed", (request) =>
    request.url().includes("/api/v1/test"),
  );
  await page.locator("#probe-all").click();
  // navigate away while the response is pending, then come back
  await page.locator('.nav a[data-route="logs"]').click();
  await expect(page.locator("#log")).toBeVisible();
  await aborted;
  // no stale toast bleeds onto the logs page…
  await expect(page.locator("#toasts")).not.toContainText("Probe complete");
  // …and the dashboard remounts cleanly afterwards
  await page.locator('.nav a[data-route=""]').click();
  await expect(page.locator(".dashboard-shell")).toBeVisible();
  await expect(page.locator("#probe-all")).toBeEnabled();
});

test("ABA: a speed stream in flight when the page unmounts has no effects", async ({
  app,
}) => {
  const { page, control } = app;
  // arm the stream gate and never release: the run is GUARANTEED to still be
  // mid-flight when we navigate away (the next test's reset unblocks it)
  await fetch(`${control}/gate/arm`, { method: "POST" });
  // the observable teardown signal: unmount aborts the NDJSON stream fetch
  // mid-body, surfacing as a failed request (a build that keeps the stream
  // alive past unmount never fails it, and this wait times the test out)
  const aborted = page.waitForEvent("requestfailed", (request) =>
    request.url().includes("/api/v1/test"),
  );
  await page.locator("#speed-all").click();
  await expect(page.locator("#toasts")).toContainText("Speed Test In Progress.");
  await page.locator('.nav a[data-route="logs"]').click();
  await expect(page.locator("#log")).toBeVisible();
  await aborted;
  await expect(page.locator("#toasts")).not.toContainText("interrupted");
  await expect(page.locator("#toasts")).not.toContainText("Speed Test Complete");
  await page.locator('.nav a[data-route=""]').click();
  await expect(page.locator(".dashboard-shell")).toBeVisible();
  await expect(page.locator("#speed-all")).toBeEnabled({ timeout: 10_000 });
});

test("ABA: a routes fetch delayed across two remounts applies exactly once", async ({
  app,
}) => {
  const { page } = app;
  await delay(page, "/api/v1/routes", 1200, { method: "GET" });
  // A -> B -> A: the first mount's routes response arrives after its page is
  // gone; the second mount must end up rendered from its own request only.
  await page.locator('.nav a[data-route="logs"]').click();
  await page.locator('.nav a[data-route=""]').click();
  await expect(page.locator(".rule-row[data-id]")).toHaveCount(3, {
    timeout: 10_000,
  });
  await expect(page.locator(".rule-row[data-id] .rule-name").first()).toHaveText(
    "Streaming",
  );
});

test("single-flight: double-clicking the LAN toggle sends one request", async ({
  app,
}) => {
  const { page } = app;
  let posts = 0;
  page.on("request", (request) => {
    if (request.url().includes("/api/v1/routes/lan") && request.method() === "POST") {
      posts += 1;
    }
  });
  await delay(page, "/api/v1/routes/lan", 600);
  await page.locator("[data-lan-toggle]").dblclick();
  // the toast is the completion signal of the accepted flight; a regressed
  // build sends its second POST synchronously at click time, so the counter
  // already holds 2 by now — no settling wait needed
  await expect(page.locator("#toasts")).toContainText("LAN direct off");
  expect(posts).toBe(1);
  await expect(page.locator("[data-lan-toggle]")).toHaveAttribute(
    "aria-pressed",
    "false",
  );
});

test("single-flight: double-submitting the add-ruleset form creates one", async ({
  app,
}) => {
  const { page } = app;
  let posts = 0;
  page.on("request", (request) => {
    if (
      request.url().endsWith("/api/v1/routes/rulesets") &&
      request.method() === "POST"
    ) {
      posts += 1;
    }
  });
  await delay(page, "/api/v1/routes/rulesets", 600);
  await page.locator("[data-add-rule]").click();
  await page.locator(".modal #name").fill("Doubled");
  await page.locator(".modal #matchers").fill("example.com");
  // raw synchronous double-activation (Playwright's own dblclick refuses: the
  // first click disables the button, which is exactly the guard under test)
  await page
    .locator('.modal button[type="submit"]')
    .evaluate((btn) => { btn.click(); btn.click(); });
  // completion signal + counter: an unguarded build fires its second POST at
  // the second click, before any response, so it is already counted here
  await expect(page.locator("#toasts")).toContainText("Created Doubled.");
  expect(posts).toBe(1);
  await expect(page.locator(".rule-row[data-id]")).toHaveCount(4);
});

test("single-flight: double-clicking Remove opens exactly one dialog", async ({
  app,
}) => {
  const { page } = app;
  await page.locator(`${DE} [data-remove]`).dblclick();
  await expect(page.locator(".overlay")).toHaveCount(1);
  // one Escape must clear everything: were a second dialog stacked beneath
  // (the unguarded double-click behavior), the count would stay at 1 and this
  // auto-retrying assertion would fail — no settling wait needed
  await page.keyboard.press("Escape");
  await expect(page.locator(".overlay")).toHaveCount(0);
  await expect(page.locator(DE)).toBeVisible();
});
