// The Markdown blocks of outputs and replies: tables in the forms models write them.
import assert from "node:assert/strict";
import { test } from "node:test";
import { cells, parse } from "../src/markdownParse.ts";

const kinds = (text: string) => parse(text.split("\n")).map((b) => b.t);

test("a table right after a line of text (no blank line) is a table", () => {
  const blocks = parse("Results of the review:\n| File | Issue |\n|---|---|\n| a.ts | unused import |\n\nDone.".split("\n"));
  assert.deepEqual(blocks.map((b) => b.t), ["p", "table", "p"]);
  const t = blocks[1] as { t: "table"; head: string[]; rows: string[][] };
  assert.deepEqual(t.head, ["File", "Issue"]);
  assert.deepEqual(t.rows, [["a.ts", "unused import"]]);
});

test("separators with single dashes, alignment, and without outer pipes", () => {
  assert.deepEqual(kinds("| a | b |\n|-|-|\n| 1 | 2 |"), ["table"]);
  assert.deepEqual(kinds("| a | b |\n|:--|--:|\n| 1 | 2 |"), ["table"]);
  assert.deepEqual(kinds("a | b\n--- | ---\n1 | 2"), ["table"]);
});

test("a line of dashes under text is not a table", () => {
  assert.ok(!kinds("Some text\n---\nMore").includes("table"));
  assert.ok(!kinds("Some text | with a pipe\n---\nMore").includes("table"));
});

test("escaped pipes and pipes in code stay in their cell", () => {
  assert.deepEqual(cells("| `a|b` | x \\| y |"), ["`a|b`", "x | y"]);
  assert.deepEqual(cells("| one |"), ["one"]);
});

test("a table with CRLF line ends", () => {
  assert.deepEqual(kinds("Intro\r\n| a | b |\r\n|---|---|\r\n| 1 | 2 |".split("\r\n").join("\n")), ["p", "table"]);
});
