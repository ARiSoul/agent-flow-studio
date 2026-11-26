// Round 8: the server's waiting items read by the editor (a shared fixture), time waiting for you.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { attention, type Waiting } from "../src/attention.ts";
import { retriedFailures } from "../src/runLines.ts";
import { waitedOf, type Ev } from "../src/runSteps.ts";

const fixture = JSON.parse(readFileSync(new URL("./fixtures/waiting.json", import.meta.url), "utf-8")) as { runs: Waiting[] };

test("the editor counts the server's waiting items as the queue does: old and dismissed apart", () => {
  const n = attention("open", { pending: 0, gate: true, paused: false }, fixture.runs);
  assert.equal(n.mine, 1);
  assert.deepEqual(n.counted.map((w) => w.input), ["other"]);
  assert.deepEqual(n.later.map((w) => w.input), ["old", "quiet"]);
  assert.equal(n.total, 2);
  for (const w of fixture.runs) assert.deepEqual(Object.keys(w).sort(), ["dismissed", "flow", "id", "input", "kind", "label", "since", "stale", "title"]);
});

test("time waiting for the person at gates", () => {
  const ev = (type: string, more: Record<string, unknown>, ts: number): Ev => ({ i: ts, type, ts, ...more });
  const events = [ev("gate", { gate: "g", kind: "human" }, 10), ev("decision", { gate: "g" }, 40), ev("gate", { gate: "h", kind: "human" }, 50),
    ev("gate", { gate: "s", kind: "stop" }, 55)];
  assert.equal(waitedOf(events), 30);
  assert.equal(waitedOf(events, 60), 40);  // the open gate counts until now
  assert.equal(retriedFailures([]).size, 0);
});

import { line } from "../src/runLines.ts";
test("a decision kept for its turn is a log line", () => {
  const fake = { t: (k: string, v?: Record<string, string>) => `${k}${v ? JSON.stringify(v) : ""}`, tOr: (_k: string, d: string) => d, tn: (k: string) => k } as never;
  assert.match(line({ i: 1, type: "turn", after: "release-q2" }, fake)!.text, /rp\.ev\.turn.*release-q2/);
});
