// A unified diff (`git diff`) as lines to draw: each with its kind and its line numbers in the old
// and the new file. No React here, so it is tested alone.

export type DiffLine = { kind: "hunk" | "add" | "del" | "same" | "meta"; text: string; old?: number; new?: number };

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/;

export function diffLines(diff: string): DiffLine[] {
  const out: DiffLine[] = [];
  let o = 0, n = 0, inHunk = false;
  for (const raw of diff.replace(/\r\n/g, "\n").split("\n")) {
    const h = HUNK.exec(raw);
    if (h) { o = Number(h[1]); n = Number(h[2]); inHunk = true; out.push({ kind: "hunk", text: raw }); continue; }
    if (!inHunk) { if (raw) out.push({ kind: "meta", text: raw }); continue; }  // diff --git, index, ---, +++
    if (raw.startsWith("\\")) { out.push({ kind: "meta", text: raw }); continue; }  // "\ No newline at end of file"
    if (raw.startsWith("+")) out.push({ kind: "add", text: raw.slice(1), new: n++ });
    else if (raw.startsWith("-")) out.push({ kind: "del", text: raw.slice(1), old: o++ });
    else if (raw.startsWith(" ") || raw === "") out.push({ kind: "same", text: raw.slice(1), old: o++, new: n++ });
    else { inHunk = false; out.push({ kind: "meta", text: raw }); }  // the next file's header
  }
  // a trailing empty line (the diff ends with "\n") is not a line of the file
  while (out.length && out[out.length - 1].kind === "same" && out[out.length - 1].text === "") out.pop();
  return out;
}
