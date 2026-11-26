// Round 6 of the Run panel: retries, live cost, plurals, links, the actions sent to the server.
import assert from "node:assert/strict";
import { test } from "node:test";
import { en } from "../src/i18n/en.ts";
import { es } from "../src/i18n/es.ts";
import { pluralKey } from "../src/i18n/plural.ts";
import { pt } from "../src/i18n/pt.ts";
import { ApiError, runApi } from "../src/runActions.ts";
import { foldLinks, line } from "../src/runLines.ts";
import { makeRunLink, parseRunLink } from "../src/runLink.ts";
import { liveCostOf, parseLines, shortId, shortModel, stepsOf, traceOf, TRANSIENT, type Ev } from "../src/runSteps.ts";

const spec = { version: 1, name: "f", start: "a", execution: "graph", agents: { a: {}, b: {} }, gates: { g: { kind: "human", label: "G" } },
  edges: [{ from: "a", to: "b" }, { from: "a", to: "g" }] } as never;
let i = 0;
const ev = (type: string, more: Record<string, unknown> = {}, ts = 100 + i): Ev => ({ i: i++, type, ts, ...more });

test("a temporary error waits, then the step runs again", () => {
  i = 0;
  const events = [ev("delegate", { agent: "a" }), ev("agent_done", { agent: "a", error: true, message: "API Error: overloaded (529)", transient: true }),
    ev("retry", { agent: "a", attempt: 1, max: 2, wait: 15, at: 500, reason: "overloaded" })];
  let a = stepsOf(spec, events).find((s) => s.node === "a")!;
  assert.equal(a.state, "retrying");
  assert.deepEqual(a.retry, { attempt: 1, max: 2, at: 500, reason: "overloaded" });
  assert.equal(traceOf(events).a, "active");
  a = stepsOf(spec, [...events, ev("delegate", { agent: "a", retry: 1 }), ev("agent_done", { agent: "a" })]).find((s) => s.node === "a")!;
  assert.equal(a.state, "done");
  assert.equal(a.retry, undefined);
  assert.equal(a.attempts.length, 2);
  const cancelled = stepsOf(spec, [...events, ev("retry_cancelled", { agent: "a" })]).find((s) => s.node === "a")!;
  assert.equal(cancelled.state, "failed");
  assert.equal(cancelled.transient, true);
});

test("only API errors look temporary", () => {
  for (const x of ["API Error: 529 overloaded", "API Error: overloaded (529)", "rate_limit_error", "API Error (503)", "API Error: Connection error."]) assert.ok(TRANSIENT.test(x), x);
  for (const x of ["Bash timed out after 120 s", "HTTP 500 from connector jira", "error at line 504", "connector: connection error"]) assert.ok(!TRANSIENT.test(x), x);
});

test("a live cost estimate for the sessions still going", () => {
  i = 0;
  const steps = stepsOf(spec, [ev("delegate", { agent: "a" }), ev("usage", { agent: "a", tokens: { out: 10 }, est: 0.002 }),
    ev("usage", { agent: "a", tokens: { out: 5 }, est: 0.001 }), ev("delegate", { agent: "b" }), ev("usage", { agent: "b", tokens: { out: 3 } })]);
  assert.equal(Math.round(liveCostOf(steps) * 1000), 3);
  const done = stepsOf(spec, [ev("delegate", { agent: "a" }), ev("usage", { agent: "a", est: 0.002 }), ev("result", { agent: "a", cost: 0.004 })]);
  assert.equal(liveCostOf(done), 0);  // the real cost replaced the estimate
});

test("a gate's decision keeps how long it waited", () => {
  i = 0;
  const g = stepsOf(spec, [ev("gate", { gate: "g", label: "G" }), ev("decision", { gate: "g", decision: "approve", comment: "", waited: 23.5 })])
    .find((s) => s.node === "g")!;
  assert.deepEqual(g.decision, { decision: "approve", comment: "", waited: 23.5 });
});

const fake = { t: (k: string, v?: Record<string, string>) => `${k}${v ? JSON.stringify(v) : ""}`, tOr: (_k: string, d: string) => d,
  tn: (k: string, n: number) => `${k}#${n}` } as never;

test("log lines: short session id and model, retries, plurals", () => {
  i = 0;
  const r = line(ev("result", { agent: "a", session_id: "320e4915-0f35-5869-84a0-28eb3b5a33ff", turns: 1, cost: 0.004, model: "claude-sonnet-5-5", denials: 1 }), fake)!;
  assert.ok(r.text.includes('"session":"320e4915…"'), r.text);
  assert.ok(r.text.includes("rp.turns#1") && r.text.includes("· sonnet-5-5") && r.text.includes("run.ev.denials#1"), r.text);
  assert.ok(!r.text.includes("28eb3b5a33ff"));
  assert.match(line(ev("retry", { agent: "a", attempt: 1, max: 2, wait: 15 }), fake)!.text, /rp\.ev\.retry.*"n":"2".*"of":"3"/);
  assert.match(line(ev("delegate", { agent: "a", retry: 1 }), fake)!.text, /rp\.ev\.startedAgain/);
  assert.equal(shortId("abc"), "abc");
  assert.equal(shortModel("claude-opus-5-5"), "opus-5-5");
});

test("the links a step follows share one log line", () => {
  i = 0;
  const x = (e: Ev) => ({ e, l: line(e, fake)!, node: e.from as string | undefined });
  const got = foldLinks([x(ev("edge", { from: "a", to: "b", fired: true })), x(ev("edge", { from: "a", to: "g", fired: false })),
    x(ev("edge", { from: "b", to: "g", fired: true }))]);
  assert.equal(got.length, 2);
  assert.ok(got[0].l.text.includes(" · "));
});

test("plural forms: every count string has one and other in every language", () => {
  const langs = { en, pt, es } as Record<string, Record<string, string>>;
  const keys = new Set(Object.keys(en).filter((k) => /\.(one|other)$/.test(k)).map((k) => k.replace(/\.(one|other)$/, "")));
  assert.ok(keys.has("rp.nextError") && keys.has("rp.turns"));
  for (const [lang, m] of Object.entries(langs)) {
    for (const k of keys) { assert.ok(m[`${k}.one`], `${lang} ${k}.one`); assert.ok(m[`${k}.other`], `${lang} ${k}.other`); }
    assert.deepEqual(Object.keys(m).sort(), Object.keys(en).sort(), lang);
  }
  const has = (k: string) => k in en;
  assert.equal(pluralKey("en", "rp.nextError", 1, has), "rp.nextError.one");
  assert.equal(pluralKey("en", "rp.nextError", 2, has), "rp.nextError.other");
  assert.equal(pluralKey("pt-PT", "rp.turns", 0, has), "rp.turns.other");
  assert.equal((en as Record<string, string>)["rp.nextError.one"].includes("errors"), false);
});

test("a run's address in the URL", () => {
  assert.equal(makeRunLink({ flow: "release", run: "release-20261126-101500", step: "writer" }), "#flow=release&run=release-20261126-101500&step=writer");
  assert.deepEqual(parseRunLink("#flow=release&run=r1&step=writer"), { flow: "release", run: "r1", step: "writer" });
  assert.deepEqual(parseRunLink("#flow=../x&run=<script>"), {});  // only names
  assert.equal(makeRunLink({}), "");
});

test("the stream parser joins an event split across chunks", () => {
  const a = parseLines('{"i":0,"type":"start"}\n{"i":1,"ty');
  const b = parseLines(`${a.rest}pe":"tool"}\n`);
  assert.deepEqual([...a.events, ...b.events].map((e) => e.type), ["start", "tool"]);
  assert.equal(b.rest, "");
});

test("the actions send what the server expects", async () => {
  const calls: { url: string; method?: string; body?: unknown; headers?: Record<string, string> }[] = [];
  const reply = (status: number, data: unknown) => new Response(JSON.stringify(data), { status });
  let next: () => Response = () => reply(200, { id: "r1", status: "running" });
  const f = (async (url: string, init?: RequestInit) => {
    calls.push({ url, method: init?.method, body: init?.body ? JSON.parse(String(init.body)) : undefined, headers: init?.headers as Record<string, string> });
    return next();
  }) as typeof fetch;
  const ra = runApi(f);
  await ra.decide("r1", "answer", "use v2");
  await ra.retry("r1", "writer", "now");
  await ra.dismiss("my run", true);
  await ra.queue({ flow: "f", mode: "full" });
  await ra.unqueue("q1");
  await ra.approveWrite("r1", "a/1", false, "no");
  assert.deepEqual(calls.map((c) => [c.method, c.url]), [
    ["POST", "/api/runs/r1/decision"], ["POST", "/api/runs/r1/retry"], ["PUT", "/api/runs/my%20run/dismissed"],
    ["POST", "/api/runs-queue"], ["DELETE", "/api/runs-queue/q1"], ["POST", "/api/runs/r1/approvals/a%2F1"]]);
  assert.deepEqual(calls[0].body, { decision: "answer", comment: "use v2" });
  assert.deepEqual(calls[1].body, { node: "writer", action: "now" });
  assert.ok(calls.every((c) => c.headers?.["X-Flow-Editor"] === "1"));
  next = () => reply(409, { detail: "a run is already in progress" });
  await assert.rejects(ra.resume("r1", false), (e: unknown) => e instanceof ApiError && e.status === 409 && e.message === "a run is already in progress");
  next = () => reply(422, { detail: { inputs: { topic: "required" } } });
  await assert.rejects(ra.start({}), (e: unknown) => e instanceof ApiError && (e.detail as { inputs: unknown }).inputs !== undefined);
});
