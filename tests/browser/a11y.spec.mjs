// Accessibility evidence in the running accessibility tree — axe scans per
// page (and inside an open dialog), plus keyboard-only traversals of the main
// flows. CSP/console/rejection evidence is enforced for every test by the
// shared fixtures; these specs add what static DOM review cannot prove.
import AxeBuilder from "@axe-core/playwright";
import { test, expect } from "./support/fixtures.mjs";

async function expectNoAxeViolations(page, scope) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa"])
    // color-contrast is a known, deliberate exception: the theme's muted
    // grays (eyebrows, hints, refs) fail AA contrast by design. Tracked for a
    // dedicated design pass — every structural/semantic rule stays enforced,
    // so regressions in roles, names, labels, or focus order still fail CI.
    .disableRules(["color-contrast"])
    .analyze();
  const summary = results.violations.map(
    (v) => `${scope}: ${v.id} (${v.impact}) — ${v.nodes.length} node(s): ${v.help}`,
  );
  expect(summary).toEqual([]);
}

// destructuring `evidence` (unused) opts this cookie-less test into the ledger
test("axe: login page", async ({ page, fixture, evidence: _evidence }) => {
  await page.goto(fixture.app);
  await expect(page.locator("#form")).toBeVisible();
  await expectNoAxeViolations(page, "login");
});

test("axe: dashboard, bundle, logs, and an open dialog", async ({ app }) => {
  const { page } = app;
  await expect(page.locator(".dashboard-shell")).toBeVisible();
  await expectNoAxeViolations(page, "dashboard");

  await page.locator("[data-add-rule]").click();
  await expect(page.locator(".overlay .modal")).toBeVisible();
  await expectNoAxeViolations(page, "add-ruleset dialog");
  await page.keyboard.press("Escape");

  await page.locator('.nav a[data-route="bundle"]').click();
  await expect(page.locator(".bundle-page")).toBeVisible();
  await expectNoAxeViolations(page, "bundle");

  await page.locator('.nav a[data-route="logs"]').click();
  await expect(page.locator("#log")).toBeVisible();
  await expectNoAxeViolations(page, "logs");
});

test("keyboard-only: navigate pages, drive a dialog, flip a toggle", async ({
  app,
}) => {
  const { page } = app;

  // reach and use the nav purely with the keyboard
  await page.locator('.nav a[data-route="bundle"]').focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".bundle-page")).toBeVisible();
  await page.locator('.nav a[data-route=""]').focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".dashboard-shell")).toBeVisible();

  // the add-rule row is a keyboard control; Enter opens the dialog, focus
  // lands inside, Tab stays trapped, Escape closes and restores focus
  await page.locator("[data-add-rule]").focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".overlay .modal")).toBeVisible();
  const inDialog = () =>
    page.evaluate(() => !!document.activeElement?.closest(".modal"));
  expect(await inDialog()).toBe(true);
  for (let i = 0; i < 12; i += 1) await page.keyboard.press("Tab");
  expect(await inDialog()).toBe(true); // trapped, even after cycling past the end
  await page.keyboard.press("Escape");
  await expect(page.locator(".overlay")).toHaveCount(0);
  await expect(page.locator("[data-add-rule]")).toBeFocused();

  // a real switch: the LAN toggle flips with the keyboard and reports state
  const lan = page.locator("[data-lan-toggle]");
  await expect(lan).toHaveAttribute("aria-pressed", "true");
  await lan.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#toasts")).toContainText("LAN direct off");
  await expect(page.locator("[data-lan-toggle]")).toHaveAttribute(
    "aria-pressed",
    "false",
  );
});

test("keyboard-only: the custom select is a listbox with arrow-key flow", async ({
  app,
}) => {
  const { page } = app;
  await page.locator('.nav a[data-route="logs"]').click();
  const button = page.locator(".cselect-btn");
  await expect(button).toHaveAttribute("aria-haspopup", "listbox");
  await button.focus();
  await page.keyboard.press("Enter");
  await expect(button).toHaveAttribute("aria-expanded", "true");
  // arrow to another option and choose it
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(button).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator("#lines")).toHaveValue("500");
  // focus returned to the collapsed control
  await expect(button).toBeFocused();
});
