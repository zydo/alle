// Opt-in evidence for the dashboard's polling cost at scale — never a gate.
//
//   ALLE_BENCH=1 npx playwright test dashboard-scale
//   ALLE_BENCH=1 ALLE_BENCH_CHANNELS=500 npx playwright test dashboard-scale
//
// Browser timings depend on the machine, the display, and what else is
// rendering, so this prints numbers for a human to read instead of asserting
// them. What it *does* assert is the deterministic half: an unchanged status
// poll must replace no nodes at all, at any fleet size.
import { test, expect, STATUS_TICK_MS } from "./support/fixtures.mjs";

const CHANNELS = Number(process.env.ALLE_BENCH_CHANNELS || 200);

test.describe("dashboard render at scale", () => {
  test.skip(!process.env.ALLE_BENCH, "opt-in: set ALLE_BENCH=1");
  test.setTimeout(180_000);

  test(`unchanged polls over ${CHANNELS} channels`, async ({ app }) => {
    const { page } = app;

    // Expand the real status response into a large synthetic fleet. The server
    // stays real — only the channel list is inflated, so the page renders the
    // same row markup it always does, just a lot more of it.
    await page.route("**/api/v1/status*", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const template = body.channels[0];
      body.channels = Array.from({ length: CHANNELS }, (_, i) => ({
        ...template,
        name: `wg_bench_${i}`,
        label: `Bench ${i}`,
        port: `:${20000 + i}`,
        port_number: 20000 + i,
      }));
      body.channel_count = CHANNELS;
      body.enabled_count = CHANNELS;
      await route.fulfill({ response, json: body });
    });
    await page.reload();
    await expect(page.locator(".dashchan.body")).toHaveCount(CHANNELS, {
      timeout: 60_000,
    });

    const measured = await page.evaluate(async (tick) => {
      const grid = document.querySelector(".grid");
      let records = 0;
      let addedNodes = 0;
      const observer = new MutationObserver((batch) => {
        records += batch.length;
        for (const record of batch) addedNodes += record.addedNodes.length;
      });
      observer.observe(grid, { childList: true });
      await new Promise((done) => setTimeout(done, tick * 3 + 500));
      observer.disconnect();

      // Price what the renderer used to spend on every one of those polls: one
      // replaceChildren over the identical children, plus the layout it
      // invalidates. Same nodes, same order — purely the cost of detaching and
      // re-appending them.
      const samples = [];
      for (let i = 0; i < 30; i++) {
        const started = performance.now();
        grid.replaceChildren(...grid.children);
        void grid.offsetHeight; // force the invalidated layout, don't defer it
        samples.push(performance.now() - started);
      }
      samples.sort((a, b) => a - b);
      return {
        rows: grid.children.length,
        polls: 3,
        mutationRecords: records,
        replacedNodes: addedNodes,
        noopReplaceBestMs: samples[0],
        noopReplaceMedianMs: samples[Math.floor(samples.length / 2)],
      };
    }, STATUS_TICK_MS);

    console.log(`\n  dashboard render at scale — ${measured.rows} rows`);
    console.log(`    unchanged polls observed        ${measured.polls}`);
    console.log(`    grid mutation records           ${measured.mutationRecords}`);
    console.log(`    nodes replaced                  ${measured.replacedNodes}`);
    console.log(
      `    one no-op replaceChildren       ${measured.noopReplaceBestMs.toFixed(2)} ms best,` +
        ` ${measured.noopReplaceMedianMs.toFixed(2)} ms median` +
        `  (what each poll used to cost)\n`,
    );

    // The only assertion: quiet polls are free, however many rows there are.
    expect(measured.mutationRecords).toBe(0);
    expect(measured.replacedNodes).toBe(0);
  });
});
