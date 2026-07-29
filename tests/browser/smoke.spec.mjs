// The focused real-browser smoke layer: real stdlib server, synthetic state.
// Each test drives a real contract end to end (login exchange, status poll,
// stream framing, bundle round-trip) — no route mocking, no component stubs.
import { test, expect, STATUS_TICK_MS } from "./support/fixtures.mjs";

const US = '.row.dashchan.body[data-id="wg_us_new_york_1"]';
const DE = '.row.dashchan.body[data-id="wg_de_1"]';

test("login: single-use token exchanges for a session; reuse is refused", async ({
  page,
  fixture,
  evidence,
}) => {
  // the deliberate bad-token attempt below logs a 401 resource error
  evidence.allow(/console: Failed to load resource: .*401/);
  // no cookie -> the login page, not the app
  await page.goto(fixture.app);
  await expect(page.locator("#form")).toBeVisible();

  // a bad token is refused with a visible message
  await page.locator("#token").fill("not-a-real-token");
  await page.locator("#form button[type=submit]").click();
  await expect(page.locator("#err")).toHaveText(/wasn't accepted/);

  // the real tokenized URL signs in and lands on the dashboard
  const { url } = await (await fetch(`${fixture.control}/login-url`)).json();
  await page.goto(url);
  await expect(page.locator(".dashboard-shell")).toBeVisible();

  // the token was single-use: replaying it in a cookie-less context fails
  const clean = await page.context().browser().newContext();
  const replay = await clean.newPage();
  await replay.goto(url);
  await expect(replay.locator("#form")).toBeVisible();
  await clean.close();
});

test("status poll: masthead + channels render the daemon's truth", async ({ app }) => {
  const { page } = app;
  await expect(page.locator("#pill-text")).toHaveText("stopped");
  await expect(page.locator("#entry-addr")).toContainText("127.0.0.1:");
  await expect(page.locator(US)).toContainText("New York, United States");
  await expect(page.locator(`${US} .port`)).toContainText(":");
  await expect(page.locator(DE)).toContainText("Germany");
  // rulesets arrive from /routes with the built-in LAN row pinned at priority 0
  await expect(page.locator(".rule-row.lan")).toContainText("Priority 0");
  await expect(page.locator(".rule-row[data-id]")).toHaveCount(3);
  await expect(page.locator(".rule-row[data-id]").first()).toContainText("Streaming");
  // IP/latency cells fill from an in-session probe, not from stale state
  await expect(page.locator(`${US} .ip`)).toHaveText("");
  await page.locator("#probe-all").click();
  await expect(page.locator("#toasts")).toContainText("Probe complete.");
  await expect(page.locator(`${DE} .ip`).first()).toHaveText("203.0.113.10");
  await expect(page.locator(`${US} .ip`).first()).toHaveText("203.0.113.11");
  await expect(page.locator(`${US} .lat`)).toHaveText("27 ms");
  // the v6-capable channel shows its IPv6 exit stacked under the IPv4 in
  // the same cell; v4-only channels show no second row. The 160px column
  // fits a realistic compressed v6 exit untruncated (measured, not hoped).
  const v6 = page.locator(`${DE} .ip-v6`);
  await expect(v6).toHaveText("2a02:6ea0:d33b:6276::28");
  expect(await v6.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
  await expect(page.locator(`${US} .ip-v6`)).toHaveCount(0);
});

test("offline recovery: banner appears while down, clears on reconnect", async ({
  app,
  evidence,
}) => {
  const { page } = app;
  // failed status fetches log network errors to the console by design here
  evidence.allow(/console: Failed to load resource/);
  await expect(page.locator("#pill-text")).toHaveText("stopped");
  await page.context().setOffline(true);
  await expect(page.locator("#banner")).toHaveClass(/show/, { timeout: 10_000 });
  await expect(page.locator("#pill-text")).toHaveText("offline");
  await page.context().setOffline(false);
  await expect(page.locator("#banner")).not.toHaveClass(/show/, { timeout: 10_000 });
  await expect(page.locator("#pill-text")).toHaveText("stopped");
});

test("channel disable/enable round-trips through state", async ({ app, evidence }) => {
  const { page } = app;
  // the restrict-only invariant: a channel a ruleset targets refuses to
  // disable, and the refusal names the ruleset (the deliberate 400 logs a
  // resource error in the console)
  evidence.allow(/console: Failed to load resource: .*400/);
  await page.locator(`${US} [data-toggle]`).click();
  await expect(page.locator("#toasts")).toContainText(
    /Cannot disable — the ruleset .Streaming./,
  );
  // the untargeted channel round-trips
  await page.locator(`${DE} [data-toggle]`).click();
  await expect(page.locator("#toasts")).toContainText("Disabled wg_de_1");
  await expect(page.locator(DE)).toHaveClass(/chan-off/, { timeout: 10_000 });
  await expect(page.locator(`${DE} .chan-state`)).toHaveText("Disabled");
  // a disabled row keeps its Enable control; flip it back
  await page.locator(`${DE} [data-toggle]`).click();
  await expect(page.locator("#toasts")).toContainText("Enabled wg_de_1");
  await expect(page.locator(DE)).not.toHaveClass(/chan-off/, { timeout: 10_000 });
});

test("stale channel toggle reports the conflict without changing state", async ({
  app,
  evidence,
}) => {
  const { page } = app;
  await expect(page.locator(DE)).toBeVisible();
  // Freeze status polling at the revision the row rendered with. Another
  // client changes the same channel's label, which must invalidate that
  // captured revision without changing its enabled state.
  const staleStatus = await page.evaluate(async () =>
    (await fetch("/api/v1/status")).json(),
  );
  await page.route("**/api/v1/status", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(staleStatus),
    }),
  );
  const competing = await page.evaluate(async () => {
    const response = await fetch("/api/v1/channels/nordvpn/wg_de_1/label", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: "Changed in another tab" }),
    });
    return response.status;
  });
  expect(competing).toBe(200);

  evidence.allow(/console: Failed to load resource: .*409/);
  await page.locator(`${DE} [data-toggle]`).click();
  await expect(page.locator("#toasts")).toContainText(
    "changed after this client read it",
  );
  const committed = await page.evaluate(async () => {
    const data = await (await fetch("/api/v1/channels")).json();
    return data.channels.find((channel) => channel.name === "wg_de_1");
  });
  expect(committed).toMatchObject({
    enabled: true,
    label: "Changed in another tab",
  });
  await expect(page.locator(DE)).not.toHaveClass(/chan-off/);
});

test("route reorder stages locally, applies once, survives reload", async ({ app }) => {
  const { page } = app;
  const names = page.locator(".rule-row[data-id] .rule-name");
  await expect(names).toHaveText(["Streaming", "Home lab", "Trackers"]);
  // every ruleset shows its destination — never a blank cell
  await expect(
    page.locator('.rule-row[data-id]', { hasText: "Home lab" }).locator(".rule-via"),
  ).toHaveText(/Direct/);
  await expect(
    page.locator('.rule-row[data-id]', { hasText: "Trackers" }).locator(".rule-via"),
  ).toHaveText(/Block/);
  await expect(
    page.locator('.rule-row[data-id]', { hasText: "Streaming" }).locator(".rule-via"),
  ).toContainText("via");
  // drag Streaming below Home lab (the only reorder control — buttons are gone).
  // HTML5 drag-and-drop is driven with explicit mouse moves: locator.dragTo
  // does not reliably synthesize dragstart/dragover/drop. Both rows must be
  // on-screen first — the dashboard stacks entry/channels/routes, so with the
  // routes panel's test box the lower rows sit below the 720px viewport and a
  // drag to an off-screen target never fires dragover on it.
  const streaming = page.locator('.rule-row[data-id]', { hasText: "Streaming" });
  const homelab = page.locator('.rule-row[data-id]', { hasText: "Home lab" });
  await streaming.scrollIntoViewIfNeeded();
  await homelab.scrollIntoViewIfNeeded();
  const sBox = await streaming.boundingBox();
  const hBox = await homelab.boundingBox();
  await page.mouse.move(sBox.x + sBox.width / 2, sBox.y + sBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(hBox.x + 200, hBox.y + 30, { steps: 12 });
  await page.mouse.up();
  // staged only: the apply bar appears, nothing is saved yet
  await expect(page.locator(".apply-bar")).toBeVisible();
  await expect(names).toHaveText(["Home lab", "Streaming", "Trackers"]);
  await page.locator("#dash-reorder-apply").click();
  await expect(page.locator("#toasts")).toContainText("Order applied.");
  await expect(page.locator(".apply-bar")).toHaveCount(0);
  await page.reload();
  await expect(page.locator(".rule-row[data-id] .rule-name")).toHaveText([
    "Home lab",
    "Streaming",
    "Trackers",
  ]);
});

test("stale staged reorder refreshes instead of merging", async ({
  app,
  evidence,
}) => {
  const { page } = app;
  const names = page.locator(".rule-row[data-id] .rule-name");
  await expect(names).toHaveText(["Streaming", "Home lab", "Trackers"]);
  // Keep the dashboard on its initial order revision while another client
  // appends a ruleset. The local drag is then a stale full permutation.
  const staleStatus = await page.evaluate(async () =>
    (await fetch("/api/v1/status")).json(),
  );
  await page.route("**/api/v1/status", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(staleStatus),
    }),
  );
  const streaming = page.locator('.rule-row[data-id]', { hasText: "Streaming" });
  const homelab = page.locator('.rule-row[data-id]', { hasText: "Home lab" });
  await streaming.scrollIntoViewIfNeeded();
  await homelab.scrollIntoViewIfNeeded();
  const sBox = await streaming.boundingBox();
  const hBox = await homelab.boundingBox();
  await page.mouse.move(sBox.x + sBox.width / 2, sBox.y + sBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(hBox.x + 200, hBox.y + 30, { steps: 12 });
  await page.mouse.up();
  await expect(names).toHaveText(["Home lab", "Streaming", "Trackers"]);

  const competing = await page.evaluate(async () => {
    const response = await fetch("/api/v1/routes/rulesets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: "Added elsewhere",
        target: "direct",
        matchers: [{ value: "elsewhere.example" }],
      }),
    });
    return response.status;
  });
  expect(competing).toBe(200);

  evidence.allow(/console: Failed to load resource: .*409/);
  await page.locator("#dash-reorder-apply").click();
  await expect(page.locator("#toasts")).toContainText(
    "changed after this client read it",
  );
  await expect(names).toHaveText([
    "Streaming",
    "Home lab",
    "Trackers",
    "Added elsewhere",
  ]);
  await expect(page.locator(".apply-bar")).toHaveCount(0);
});

test("rule tracer: verdict renders and the winning row is highlighted", async ({
  app,
  evidence,
}) => {
  const { page } = app;
  // the deliberate bad-input trace below logs a 400 resource error
  evidence.allow(/console: Failed to load resource: .*400/);
  // an IP destination needs no DNS: 203.0.113.55 hits Home lab's ip_cidr
  await page.locator("#trace-input").fill("203.0.113.55");
  await page.locator("#trace-btn").click();
  await expect(page.locator("#trace-result")).toBeVisible();
  await expect(page.locator(".trace-verdict")).toHaveText("Direct");
  await expect(page.locator(".trace-reason")).toContainText("203.0.113.55");
  // the matched matcher is rendered as an emphasized (bold) segment
  await expect(page.locator(".trace-reason b")).toHaveText("203.0.113.0/24");
  await expect(page.locator(".rule-row.trace-hit")).toContainText("Home lab");
  // a private address is decided by the built-in LAN row instead
  await page.locator("#trace-input").fill("192.168.1.10");
  await page.locator("#trace-btn").click();
  await expect(page.locator(".trace-verdict")).toHaveText("Direct (LAN)");
  await expect(page.locator(".rule-row.lan.trace-hit")).toBeVisible();
  await expect(page.locator(".trace-hit")).toHaveCount(1);
  // unusable input surfaces the API's message, clears any highlight
  await page.locator("#trace-input").fill("not a domain");
  await page.locator("#trace-btn").click();
  await expect(page.locator("#trace-result .trace-err")).toContainText("not a valid");
  await expect(page.locator(".trace-hit")).toHaveCount(0);
  // clear hides the result box
  await page.locator("#trace-input").fill("203.0.113.55");
  await page.locator("#trace-btn").click();
  await expect(page.locator(".rule-row.trace-hit")).toContainText("Home lab");
  await page.locator("#trace-clear").click();
  await expect(page.locator("#trace-result")).toBeHidden();
  await expect(page.locator(".trace-hit")).toHaveCount(0);
});

test("rule tracer: in-flight state shows a spinner (button + result), then resolves", async ({
  app,
}) => {
  const { page } = app;
  // stall the trace POST so the pending state is observable
  await page.route("**/api/v1/routes/trace**", async (route) => {
    await new Promise((r) => setTimeout(r, 500));
    return route.fallback();
  });
  await page.locator("#trace-input").fill("203.0.113.55");
  await page.locator("#trace-btn").click();
  // both the button and the result area show a spinner while the request is up
  await expect(page.locator("#trace-btn .spinner")).toBeVisible();
  await expect(page.locator("#trace-btn")).toBeDisabled();
  await expect(page.locator("#trace-result .spinner")).toBeVisible();
  await expect(page.locator("#trace-result")).toContainText("Tracing 203.0.113.55");
  // …and it resolves to the verdict once the response lands
  await expect(page.locator(".trace-verdict")).toHaveText("Direct");
  await expect(page.locator("#trace-btn")).toHaveText("Trace");
  await expect(page.locator("#trace-btn .spinner")).toHaveCount(0);
});

test("modal cancellation: Escape closes, background un-inerts, focus returns", async ({
  app,
}) => {
  const { page } = app;
  await page.locator("[data-add-rule]").click();
  await expect(page.locator(".overlay .modal")).toBeVisible();
  // the page behind the dialog is inert while it is open
  expect(await page.locator("main#view").evaluate((el) => el.inert)).toBe(true);
  await page.keyboard.press("Escape");
  await expect(page.locator(".overlay")).toHaveCount(0);
  expect(await page.locator("main#view").evaluate((el) => el.inert)).toBe(false);
  // focus restored to the opener, so keyboard flow continues where it left off
  await expect(page.locator("[data-add-rule]")).toBeFocused();
});

test("logs page polls while mounted and stops cold after unmount", async ({ app }) => {
  const { page } = app;
  let logRequests = 0;
  page.on("request", (request) => {
    if (request.url().includes("/api/v1/logs")) logRequests += 1;
  });
  await page.locator('.nav a[data-route="logs"]').click();
  await expect(page.locator("#log")).toBeVisible();
  await expect(page.locator("#log")).toContainText(/No log lines yet|./);
  await expect
    .poll(() => logRequests, { timeout: 10_000 })
    .toBeGreaterThanOrEqual(1);
  // back to the dashboard: the logs poll must stop with the page
  await page.locator('.nav a[data-route=""]').click();
  await expect(page.locator(".dashboard-shell")).toBeVisible();
  const after = logRequests;
  // the status poll shares the logs poll's 3s cadence and keeps running, so
  // two observed status responses prove a full logs interval elapsed — with
  // no logs request in it, the logs timer is provably stopped
  for (let i = 0; i < 2; i += 1) {
    await page.waitForResponse((response) =>
      response.url().includes("/api/v1/status"),
    );
  }
  expect(logRequests).toBe(after);
});

test("bundle: export round-trips through validate and merge-import", async ({
  app,
  evidence,
}) => {
  const { page } = app;
  // the deliberate junk-file validation at the end 400s, by design
  evidence.allow(/console: Failed to load resource: .*400/);
  await page.locator('.nav a[data-route="bundle"]').click();
  await expect(page.locator(".bundle-page")).toBeVisible();
  // fetch the real export with the session cookie, then feed it back in
  const yaml = await page.evaluate(async () => {
    const res = await fetch("/api/v1/export");
    return res.text();
  });
  await page
    .locator("#bundle-file")
    .setInputFiles({ name: "backup.yaml", mimeType: "text/yaml", buffer: Buffer.from(yaml) });
  await page.locator("#bundle-validate").click();
  await expect(page.locator("#bundle-valid-msg")).toContainText(
    "Valid — 1 provider(s), 2 channel(s), 3 ruleset(s).",
  );
  await page.locator("#bundle-merge").click();
  // merge semantics: channels dedupe by identity, rulesets append
  await expect(page.locator("#toasts")).toContainText(
    "Imported — 3 ruleset(s) appended.",
  );
  // an invalid file reports its blockers inline, not via a silent failure
  await page
    .locator("#bundle-file")
    .setInputFiles({ name: "junk.yaml", mimeType: "text/yaml", buffer: Buffer.from("kind: nope\n") });
  await page.locator("#bundle-validate").click();
  await expect(page.locator("#bundle-err")).not.toBeEmpty();
  // the appended rulesets are visible on the dashboard
  await page.locator('.nav a[data-route=""]').click();
  await expect(page.locator(".rule-row[data-id]")).toHaveCount(6);
});

test("relabel editor closes even when the name is unchanged", async ({ app }) => {
  const { page } = app;
  const row = page.locator(US);
  // open the inline editor, "change" the label to the same value (the
  // reported bug: space added then deleted -> identical name -> the row's
  // render signature matched and the stuck input was reused forever)
  await row.locator(".name.edit").click();
  const input = page.locator("input.relabel");
  await expect(input).toBeVisible();
  const current = await input.inputValue();
  await input.fill(current + " ");
  await input.fill(current);
  await page.keyboard.press("Enter");
  await expect(page.locator("input.relabel")).toHaveCount(0, { timeout: 10_000 });
  await expect(row.locator(".name.edit")).toBeVisible();

  // Escape cancels and also restores the plain row
  await row.locator(".name.edit").click();
  await expect(page.locator("input.relabel")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator("input.relabel")).toHaveCount(0, { timeout: 10_000 });
  await expect(row.locator(".name.edit")).toBeVisible();

  // clicking elsewhere (blur) with an unchanged name closes it too
  await row.locator(".name.edit").click();
  await expect(page.locator("input.relabel")).toBeVisible();
  await page.locator(".table-title").first().click();
  await expect(page.locator("input.relabel")).toHaveCount(0, { timeout: 10_000 });
});

test("ruleset via-chips resolve and track channel labels", async ({ app }) => {
  const { page } = app;
  const viaChip = page
    .locator('.rule-row[data-id]', { hasText: "Streaming" })
    .locator(".rule-via .vch");
  // resolved to the channel's display name (not the raw provider/id ref),
  // including on a fresh load where routes may render before the first status
  await expect(viaChip).toHaveText("wg_us_new_york_1");
  // rename the channel: the via chip must follow without a manual refresh
  await page
    .locator('.row.dashchan.body[data-id="wg_us_new_york_1"] .name.edit')
    .click();
  await page.locator("input.relabel").fill("NYC");
  await page.keyboard.press("Enter");
  await expect(page.locator("#toasts")).toContainText("Label updated.");
  await expect(viaChip).toHaveText("NYC", { timeout: 10_000 });
  // and it survives a reload (no raw-ref flash sticking around)
  await page.reload();
  await expect(
    page.locator('.rule-row[data-id]', { hasText: "Streaming" }).locator(".rule-via .vch"),
  ).toHaveText("NYC");
});

test("add-channel wizard: country and city search actually filter", async ({
  app,
}) => {
  const { page } = app;
  await page.locator("[data-add-channel]").click();
  await page.locator('.prov-tile[data-provider="nordvpn"]').click();
  await expect(page.locator("#loc-grid")).toBeVisible();
  await expect(page.locator("#loc-grid [data-country]")).toHaveCount(3);
  // typing filters the grid down to matches (the reported bug: nothing hid)
  await page.locator("#loc-search").fill("jap");
  await expect(page.locator("#loc-grid [data-country]:visible")).toHaveCount(1);
  await expect(page.locator("#loc-grid [data-country]:visible")).toContainText(
    "Japan",
  );
  await page.locator("#loc-grid [data-country]:visible").click();
  // city step: same search behavior ("Any city" + Tokyo/Osaka)
  await expect(page.locator("#loc-grid [data-city]")).toHaveCount(3);
  await page.locator("#loc-search").fill("tok");
  await expect(page.locator("#loc-grid [data-city]:visible")).toHaveCount(1);
  await expect(page.locator("#loc-grid [data-city]:visible")).toContainText(
    "Tokyo",
  );
  await page.keyboard.press("Escape");
});

test("geo matchers round-trip through the ruleset editor", async ({ app }) => {
  const { page } = app;
  // create a ruleset mixing a domain and a geoip matcher (prefixed spelling)
  await page.locator("[data-add-rule]").click();
  await page.locator(".modal #name").fill("Japan");
  await page.locator(".modal #matchers").fill("example.com\ngeoip:jp");
  await page.locator('.modal button[type="submit"]').click();
  await expect(page.locator("#toasts")).toContainText("Created Japan.");

  // reopen the editor: the geo matcher must still read "geoip:jp" — the bare
  // value "jp" would re-save as a broken domain (the reported bug)
  const row = page.locator('.rule-row[data-id]', { hasText: "Japan" });
  await row.locator("[data-id-edit]").click();
  await expect(page.locator(".modal #matchers")).toHaveValue(
    "example.com\ngeoip:jp",
  );

  // and an untouched save round-trips cleanly
  await page.locator('.modal button[type="submit"]').click();
  await expect(page.locator("#toasts")).toContainText("Saved Japan.");
  await row.locator("[data-id-edit]").click();
  await expect(page.locator(".modal #matchers")).toHaveValue(
    "example.com\ngeoip:jp",
  );
  await page.keyboard.press("Escape");
});

test("stale ruleset editor keeps both writers' changes separate", async ({
  app,
  evidence,
}) => {
  const { page } = app;
  // Open first: the editor captures this exact ruleset revision. A separate
  // legacy client then updates the same ruleset without If-Match, leaving the
  // modal intentionally stale even if the dashboard poll refreshes behind it.
  const row = page.locator('.rule-row[data-id]', { hasText: "Home lab" });
  await row.locator("[data-id-edit]").click();
  await expect(page.locator(".modal #name")).toHaveValue("Home lab");
  const competing = await page.evaluate(async () => {
    const routes = await (await fetch("/api/v1/routes")).json();
    const ruleset = routes.rulesets.find((candidate) => candidate.name === "Home lab");
    const response = await fetch(
      `/api/v1/routes/rulesets/${encodeURIComponent(ruleset.id)}/update`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: "Home lab from another tab",
          target: ruleset.target,
          matchers: ruleset.rules.map((rule) => ({
            type: rule.type,
            value: rule.value,
          })),
        }),
      },
    );
    return response.status;
  });
  expect(competing).toBe(200);

  // The deliberate stale save logs the expected 409 resource error. It must
  // leave the editor open, explain how to recover, and preserve the competing
  // writer's committed value rather than silently merging or overwriting it.
  evidence.allow(/console: Failed to load resource: .*409/);
  await page.locator(".modal #name").fill("Home lab from this tab");
  await page.locator('.modal button[type="submit"]').click();
  await expect(page.locator(".modal #err")).toContainText(
    "Close and reopen this editor.",
  );
  await expect(page.locator(".modal")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(row.locator(".rule-name")).toHaveText(
    "Home lab from another tab",
  );
});

test("speed test streams rows incrementally, then completes", async ({ app }) => {
  const { page, control } = app;
  // Deterministic incrementality via the fixture's stream gate: the armed
  // run emits its first row, then BLOCKS until released — "row 1 rendered
  // while row 2 pending" is a guaranteed state, not a timing window.
  await fetch(`${control}/gate/arm`, { method: "POST" });
  await page.locator("#speed-all").click();
  await expect(page.locator("#toasts")).toContainText("Speed Test In Progress.");
  const deDown = page.locator(`${DE} span.mono`).nth(2);
  const usDown = page.locator(`${US} span.mono`).nth(2);
  await expect(deDown).toHaveText("100.0 Mbps");
  await expect(usDown).toHaveText(""); // provably pending: the gate holds row 2
  await expect(page.locator(`${US} [data-speed] .spinner`)).toBeVisible();
  await fetch(`${control}/gate/release`, { method: "POST" });
  await expect(usDown).toHaveText("90.0 Mbps");
  await expect(page.locator("#toasts")).toContainText("Speed Test Complete.");
  // busy state fully cleared: the header buttons are usable again
  await expect(page.locator("#speed-all")).toBeEnabled();
  await expect(page.locator("#probe-all")).toBeEnabled();
});

test("stable polling leaves the channel grid alone", async ({ app }) => {
  const { page } = app;
  await expect(page.locator(US)).toBeVisible();

  // Count childList mutations on the grid from here on. This is the direct
  // evidence: a poll that changed nothing must not touch the DOM at all.
  await page.evaluate(() => {
    window.__gridMutations = 0;
    window.__gridObserver = new MutationObserver((records) => {
      window.__gridMutations += records.length;
    });
    window.__gridObserver.observe(document.querySelector(".grid"), { childList: true });
  });

  // Focus a control inside a row. replaceChildren() detaches and re-appends
  // every row even when each is the same node, and a detached element loses
  // focus — so this is what the user actually felt every three seconds.
  const port = page.locator(`${US} .port`);
  await port.focus();
  await expect(port).toBeFocused();

  // Two full poll cycles with nothing changing.
  await page.waitForTimeout(STATUS_TICK_MS * 2 + 1000);

  expect(await page.evaluate(() => window.__gridMutations)).toBe(0);
  await expect(port).toBeFocused();

  // A real change still reconciles — and replaces only the row that changed.
  await page.evaluate(() => {
    window.__gridMutations = 0;
    window.__rows = [...document.querySelectorAll(".dashchan.body")];
  });
  await page.locator(`${DE} [data-probe]`).click();
  await expect(page.locator(`${DE} .ip`).first()).toHaveText("203.0.113.10");

  const replaced = await page.evaluate(() => {
    const rows = [...document.querySelectorAll(".dashchan.body")];
    return {
      mutations: window.__gridMutations,
      // Which row nodes are no longer the ones we saw before the probe.
      swapped: rows.filter((row, i) => row !== window.__rows[i]).map((r) => r.dataset.id),
      kept: rows.filter((row, i) => row === window.__rows[i]).map((r) => r.dataset.id),
    };
  });
  expect(replaced.mutations).toBeGreaterThan(0);
  expect(replaced.swapped).toEqual(["wg_de_1"]);
  expect(replaced.kept).toEqual(["wg_us_new_york_1"]);
});

test("grid reconciles adds, removes, and reorders after a quiet spell", async ({
  app,
  evidence,
}) => {
  const { page } = app;
  evidence.allow(/console: Failed to load resource: .*400/);
  const ids = () => page.locator(".dashchan.body").evaluateAll((rows) => rows.map((r) => r.dataset.id));

  await expect(page.locator(".dashchan.body")).toHaveCount(2);
  expect(await ids()).toEqual(["wg_de_1", "wg_us_new_york_1"]);

  // Let the no-op path settle first, so the reconcile below starts from a grid
  // the renderer has been deliberately leaving untouched.
  await page.waitForTimeout(STATUS_TICK_MS + 500);

  // Disable one: its row changes in place, the other keeps its node.
  await page.evaluate(() => {
    window.__rows = [...document.querySelectorAll(".dashchan.body")];
  });
  await page.locator(`${DE} [data-toggle]`).click();
  await expect(page.locator(DE)).toHaveClass(/chan-off/, { timeout: 10_000 });
  expect(
    await page.evaluate(() => {
      const rows = [...document.querySelectorAll(".dashchan.body")];
      return rows.filter((row, i) => row === window.__rows[i]).map((r) => r.dataset.id);
    }),
  ).toEqual(["wg_us_new_york_1"]);

  // Remove one: the grid shrinks and the survivor stays.
  await page.locator(`${DE} [data-remove]`).click();
  await page.locator(".confirm-modal [data-confirm]").click();
  await expect(page.locator(".dashchan.body")).toHaveCount(1, { timeout: 10_000 });
  expect(await ids()).toEqual(["wg_us_new_york_1"]);

  // And the quiet path resumes: no further mutations once it settles.
  await page.evaluate(() => {
    window.__gridMutations = 0;
    new MutationObserver((records) => {
      window.__gridMutations += records.length;
    }).observe(document.querySelector(".grid"), { childList: true });
  });
  await page.waitForTimeout(STATUS_TICK_MS + 1000);
  expect(await page.evaluate(() => window.__gridMutations)).toBe(0);
});
