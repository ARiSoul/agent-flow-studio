// Unit tests of the Run panel's pure logic (node --test; Node runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";
import { costOf, edgeTraceOf, graphOrder, groups, stepsOf, toolOutcome, traceOf, usd, duration, type Ev } from "../src/runSteps.ts";

// a fan-out (planner → a, b → writer), then a human gate, then a decision with two branches
const spec = {
  version: 1, name: "f", start: "planner", execution: "graph",
  agents: { planner: {}, a: {}, b: {}, writer: {}, yes: {}, no: {} },
  gates: { approve: { kind: "human", label: "Approve" }, choose: { kind: "decision", label: "Choose" } },
  edges: [
    { from: "planner", to: "a" }, { from: "planner", to: "b" }, { from: "a", to: "writer" }, { from: "b", to: "writer" },
    { from: "writer", to: "approve" }, { from: "approve", to: "choose" },
    { from: "choose", to: "yes", if: "x > 1" }, { from: "choose", to: "no" },
  ],
} as never;

let i = 0;
const ev = (type: string, more: Record<string, unknown> = {}, ts = 100 + i): Ev => ({ i: i++, type, ts, ...more });

test("graph order and levels", () => {
  const order = graphOrder(spec);
  assert.deepEqual(order.map((o) => o.node), ["planner", "a", "b", "writer", "approve", "choose", "yes", "no"]);
  assert.equal(order.find((o) => o.node === "a")!.level, order.find((o) => o.node === "b")!.level);
});

test("groups tell a fan-out from a decision's branches", () => {
  const g = groups(stepsOf(spec, []), spec);
  assert.deepEqual(g.filter((x) => x.kind !== "single").map((x) => [x.kind, x.steps.map((s) => s.node)]),
    [["parallel", ["a", "b"]], ["choice", ["yes", "no"]]]);
});

test("steps: states, cost, tokens, attempts, a gate's decision", () => {
  i = 0;
  const events: Ev[] = [
    ev("start", { execution: "graph" }),
    ev("delegate", { agent: "planner" }, 100), ev("result", { agent: "planner", cost: 0.01, turns: 2, model: "m1", session_id: "s1",
      tokens: { in: 1000, out: 200 } }), ev("agent_done", { agent: "planner" }, 110),
    ev("delegate", { agent: "a" }, 111), ev("agent_done", { agent: "a", error: true, message: "boom" }, 115),
    ev("resume", { redo: ["a", "writer"] }),
    ev("delegate", { agent: "a" }, 120), ev("result", { agent: "a", cost: 0.02 }), ev("agent_done", { agent: "a" }, 125),
    ev("gate", { gate: "approve", label: "Approve" }, 130), ev("decision", { gate: "approve", decision: "approve", comment: "ok" }, 140),
  ];
  const by = Object.fromEntries(stepsOf(spec, events).map((s) => [s.node, s]));
  assert.equal(by.planner.state, "done");
  assert.equal(by.planner.model, "m1");
  assert.deepEqual(by.planner.tokens, { in: 1000, out: 200 });
  assert.equal(by.a.state, "done");
  assert.equal(by.a.attempts.length, 2);
  assert.equal(by.a.attempts[0].state, "failed");
  assert.equal(by.a.attempts[0].error, "boom");
  assert.equal(by.a.attempts[1].cost, 0.02);
  assert.equal(by.b.state, "pending");
  assert.equal(by.approve.state, "done");
  assert.deepEqual(by.approve.decision, { decision: "approve", comment: "ok" });
  assert.equal(by.approve.end! - by.approve.start!, 10);  // how long it waited for the person
  assert.equal(Math.round(costOf(events) * 1000), 30);
});

test("a stop interrupts the running steps", () => {
  i = 0;
  const by = Object.fromEntries(stepsOf(spec, [ev("delegate", { agent: "a" }), ev("end", { status: "stopped" })]).map((s) => [s.node, s]));
  assert.equal(by.a.state, "interrupted");
});

test("a tool call is joined with its result or its error", () => {
  i = 0;
  const out = toolOutcome([ev("tool", { id: "t1" }), ev("tool_result", { id: "t1", size: 10, ms: 5 }), ev("tool_error", { id: "t2", error: "x" })]);
  assert.equal(out.t1.size, 10);
  assert.equal(out.t2.type, "tool_error");
});

test("canvas trace: nodes and links", () => {
  i = 0;
  const events: Ev[] = [ev("start", { execution: "graph" }), ev("delegate", { agent: "planner" }), ev("agent_done", { agent: "planner" }),
    ev("edge", { from: "planner", to: "a", fired: true }), ev("edge", { from: "planner", to: "b", fired: false }),
    ev("delegate", { agent: "a" }), ev("end", { status: "stopped" })];
  assert.deepEqual(traceOf(events), { planner: "done", a: "stopped" });
  assert.deepEqual(edgeTraceOf(events, [{ from: "planner", to: "a" }, { from: "planner", to: "b" }]), { "planner>a": "followed", "planner>b": "not" });
});

test("formats", () => {
  assert.equal(usd(0.0234), "$0.023");
  assert.equal(duration(65), "1:05");
  assert.equal(duration(4.25), "4.3 s");
});

import { line } from "../src/runLines.ts";
import { parseLines } from "../src/runSteps.ts";

const i18n = { t: (k: string, v?: Record<string, string>) => `${k}${v ? JSON.stringify(v) : ""}`, tOr: (_k: string, d: string) => d,
  tn: (k: string, n: number) => `${k}#${n}` } as never;

test("errors are counted by cause, not by every event of a failure", () => {
  i = 0;
  const kinds = [ev("agent_done", { agent: "a", error: true, message: "x" }), ev("result", { agent: "a", is_error: true, cost: 0 }),
    ev("end", { status: "error", message: "agent a: x" }), ev("tool_error", { agent: "a", error: "boom" })]
    .map((e) => line(e, i18n)?.kind);
  assert.deepEqual(kinds, ["errors", "tools", "flow", "errors"]);
  assert.equal(line(ev("usage", { agent: "a" }), i18n), null);  // live tokens are not a log line
});

test("groups: a loop back and a join do not make a parallel group", () => {
  const loop = { version: 1, name: "l", start: "a", agents: { a: {}, b: {}, c: {} }, gates: {},
    edges: [{ from: "a", to: "b" }, { from: "b", to: "a", if: "again" }, { from: "b", to: "c" }] } as never;
  assert.deepEqual(groups(stepsOf(loop, []), loop).map((g) => g.kind), ["single", "single", "single"]);
});

test("the stream parser keeps an unfinished line and skips a bad one", () => {
  const got = parseLines('{"i":0,"type":"start"}\nnot json\n{"i":1,"type":"tool"}\n{"i":2,');
  assert.deepEqual(got.events.map((e) => e.type), ["start", "tool"]);
  assert.equal(got.rest, '{"i":2,');
});

test("live tokens while a step runs, replaced by the session totals", () => {
  i = 0;
  const running = stepsOf(spec, [ev("delegate", { agent: "a" }), ev("usage", { agent: "a", tokens: { in: 100, out: 7 } }),
    ev("usage", { agent: "a", tokens: { in: 180, out: 9, cache_read: 20 } })]).find((s) => s.node === "a")!;
  assert.deepEqual(running.live, { out: 16, context: 200, est: 0 });
  assert.ok(running.last);
  const done = stepsOf(spec, [ev("delegate", { agent: "a" }), ev("usage", { agent: "a", tokens: { out: 7 } }),
    ev("result", { agent: "a", tokens: { in: 300, out: 16 } })]).find((s) => s.node === "a")!;
  assert.equal(done.live, undefined);
  assert.deepEqual(done.tokens, { in: 300, out: 16 });
});

import { wordDiff } from "../src/runSteps.ts";

test("word diff keeps, adds and removes words", () => {
  assert.deepEqual(wordDiff("the quick fox", "the slow fox"),
    [{ op: "same", text: "the " }, { op: "del", text: "quick" }, { op: "add", text: "slow" }, { op: "same", text: " fox" }]);
  assert.equal(wordDiff("a ".repeat(700), "b"), null);
});

test("a paused step goes on in the same attempt", () => {
  i = 0;
  const paused = [ev("delegate", { agent: "a" }), ev("paused", { agent: "a", session: "s1" }), ev("end", { status: "paused" })];
  const by = (evs: Ev[]) => Object.fromEntries(stepsOf(spec, evs).map((s) => [s.node, s]));
  assert.equal(by(paused).a.state, "paused");
  assert.equal(line(paused[1], i18n)?.text, "rp.ev.paused");
  assert.equal(line(ev("delegate", { agent: "a", after_pause: true }), i18n)?.text.startsWith("rp.ev.afterPause"), true);
  const goes = by([...paused, ev("delegate", { agent: "a", after_pause: true })]).a;
  assert.equal(goes.state, "running");
  assert.equal(goes.attempts.length, 1);
});
