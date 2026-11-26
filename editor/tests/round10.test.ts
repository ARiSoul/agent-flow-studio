// Round 10: the run states written once (shared with the server), each with a text in every language.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { en } from "../src/i18n/en.ts";
import { es } from "../src/i18n/es.ts";
import { pt } from "../src/i18n/pt.ts";

const table = JSON.parse(readFileSync(new URL("./fixtures/run_status.json", import.meta.url), "utf-8")) as {
  states: Record<string, { waits: boolean; holds: boolean; next: string[] }> };

test("every run state has a text in every language, and only waiting states wait", () => {
  for (const [lang, m] of Object.entries({ en, pt, es }) as [string, Record<string, string>][]) {
    for (const s of Object.keys(table.states)) assert.ok(m[`run.status.${s}`], `${lang} run.status.${s}`);
  }
  assert.deepEqual(Object.entries(table.states).filter(([, s]) => s.waits).map(([k]) => k).sort(), ["gate", "paused"]);
  assert.equal(table.states.decided.holds, false);  // a decided run never holds the queue: it goes first
});

import { runApi } from "../src/runActions.ts";
test("changing a decision holds the run on the server first", async () => {
  const calls: [string, string | undefined, unknown][] = [];
  const f = (async (url: string, init?: RequestInit) => {
    calls.push([url, init?.method, init?.body ? JSON.parse(String(init.body)) : undefined]);
    return new Response("{}", { status: 200 });
  }) as typeof fetch;
  await runApi(f).holdDecision("r1", true);
  await runApi(f).holdDecision("r1", false);
  assert.deepEqual(calls, [["/api/runs/r1/decision/hold", "POST", { dismissed: true }], ["/api/runs/r1/decision/hold", "POST", { dismissed: false }]]);
});
