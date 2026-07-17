import assert from "node:assert/strict";
import test from "node:test";

import { parseSpeedStream } from "../src/alle/assets/dashboard.js";

function stream(parts) {
  return new ReadableStream({
    start(controller) {
      for (const part of parts) controller.enqueue(part);
      controller.close();
    },
  });
}

const enc = new TextEncoder();

test("speed NDJSON decodes split UTF-8 and a final non-newline terminal", async () => {
  const text = '{"type":"row","data":{"provider":"nordvpn","name":"東京"}}\n'
    + '{"type":"done","data":{"channel_count":1}}';
  const bytes = enc.encode(text);
  const rows = [];
  const terminal = await parseSpeedStream(
    stream([bytes.slice(0, 49), bytes.slice(49, 54), bytes.slice(54)]),
    (row) => rows.push(row),
  );
  assert.equal(rows[0].name, "東京");
  assert.equal(terminal.type, "done");
});

for (const [name, records, pattern] of [
  ["missing terminal", ['{"type":"row","data":{}}\n'], /without a terminal/],
  ["malformed record", ["{nope}\n"], /malformed/],
  ["unknown record", ['{"type":"wat","data":{}}\n'], /unknown/],
  ["duplicate terminal", ['{"type":"done","data":{}}\n{"type":"done","data":{}}\n'], /after terminal/],
  ["row after terminal", ['{"type":"done","data":{}}\n{"type":"row","data":{}}\n'], /after terminal/],
]) {
  test(`speed NDJSON rejects ${name}`, async () => {
    await assert.rejects(parseSpeedStream(stream(records.map((x) => enc.encode(x)))), pattern);
  });
}
