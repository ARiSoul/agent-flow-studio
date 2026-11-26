// Round 7 of the Run panel: one count of what waits for you, recovered failures, short causes.
import assert from "node:assert/strict";
import { test } from "node:test";
import { attention, type Waiting } from "../src/attention.ts";
import { retriedFailures, shortCause } from "../src/runLines.ts";
import { stepsOf, type Ev } from "../src/runSteps.ts";

const w = (input: string, more: Partial<Waiting> = {}): Waiting => ({ id: null, input, flow: "f", title: "", kind: "gate", label: "G", ...more });

test("one count: the open run plus the other runs that wait, without old or dismissed gates", () => {
  const waiting = [w("open"), w("a"), w("b", { stale: true }), w("c", { dismissed: true }), w("d", { kind: "paused" })];
  const n = attention("open", { pending: 1, gate: true, paused: false }, waiting);
  assert.equal(n.mine, 2);  // a write to approve and its gate
  assert.deepEqual(n.counted.map((x) => x.input), ["a", "d"]);
  assert.deepEqual(n.later.map((x) => x.input), ["b", "c"]);
  assert.equal(n.total, 4);
  // the open run's own gate is old: listed, not counted
  const old = attention("b", { pending: 0, gate: true, paused: false }, waiting);
  assert.equal(old.quiet, true);
  assert.equal(old.mine, 0);
  assert.equal(attention(undefined, { pending: 0, gate: false, paused: false }, waiting).total, 3);
});

let i = 0;
const ev = (type: string, more: Record<string, unknown> = {}): Ev => ({ i: i++, type, ts: 100 + i, ...more });

test("a failure that ran again by itself is not an error; a step that recovered says so", () => {
  i = 0;
  const events = [ev("delegate", { agent: "a" }), ev("result", { agent: "a", is_error: true }), ev("agent_done", { agent: "a", error: true, message: "x" }),
    ev("retry", { agent: "a" }), ev("delegate", { agent: "a", retry: 1 }), ev("agent_done", { agent: "a" }),
    ev("delegate", { agent: "b" }), ev("agent_done", { agent: "b", error: true, message: "y" })];
  assert.deepEqual([...retriedFailures(events)], [1, 2]);  // b's failure stays an error
  const spec = { version: 1, name: "f", start: "a", agents: { a: {}, b: {} }, gates: {}, edges: [{ from: "a", to: "b" }] } as never;
  const a = stepsOf(spec, events).find((s) => s.node === "a")!;
  assert.equal(a.state, "done");
  assert.equal(a.retries, 1);
  const wrote = stepsOf(spec, [ev("delegate", { agent: "b" }), ev("agent_done", { agent: "b", error: true, message: "API Error: overloaded (529)", transient: true }),
    ev("retry_skipped", { agent: "b", why: "wrote" })]).find((s) => s.node === "b")!;
  assert.equal(wrote.wroteNoRetry, true);
  assert.equal(wrote.transient, true);
});

test("the cause of a temporary error in a few words, cut at a word", () => {
  assert.equal(shortCause("the session ended with an error (API Error: overloaded (529))"), "API Error: overloaded (529)");
  const long = shortCause("API Error: overloaded (529). The model is busy right now and could not take the request; try again later please", 60);
  assert.ok(long.endsWith("…") && long.length <= 61 && !/\w…$/.test(long.replace(/ …$/, "")) || long.endsWith("…"));
  assert.ok(!long.includes("plea"));
});
