// The block structure of the small Markdown renderer (headings, paragraphs, code, quotes, rules,
// lists, tables): no React here, so it is tested alone. Markdown.tsx draws it.
export type Block =
  | { t: "p"; lines: string[] }
  | { t: "h"; level: number; text: string }
  | { t: "code"; text: string }
  | { t: "quote"; lines: string[] }
  | { t: "hr" }
  | { t: "list"; ordered: boolean; start: number; items: Item[] }
  | { t: "table"; head: string[]; rows: string[][] };
export type Item = { text: string[]; children: Block[] };

const LIST = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/;
// a separator row: |---|:--:|, also single dashes (|-|-|) and without outer pipes (--- | ---)
const TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
/** The cells of a table row: `\|` and a pipe inside `code` stay in the cell. */
export function cells(l: string): string[] {
  const row = l.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "");
  const out: string[] = [];
  let cur = "", code = false;
  for (let i = 0; i < row.length; i++) {
    const ch = row[i];
    if (ch === "\\" && row[i + 1] === "|") { cur += "|"; i++; continue; }
    if (ch === "`") code = !code;
    if (ch === "|" && !code) { out.push(cur.trim()); cur = ""; continue; }
    cur += ch;
  }
  out.push(cur.trim());
  return out;
}
/** A table starts here: a row with a pipe and, under it, a separator row with as many columns
 * (so "text | more" over a plain "---" is not a table). */
const tableAt = (lines: string[], i: number) => lines[i].includes("|") && i + 1 < lines.length
  && TABLE_SEP.test(lines[i + 1]) && cells(lines[i + 1]).length === cells(lines[i]).length;

export function parse(lines: string[]): Block[] {
  const out: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) { i++; continue; }
    if (/^\s*```/.test(l)) {
      const body: string[] = [];
      for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) body.push(lines[i]);
      out.push({ t: "code", text: body.join("\n") });
      i++;
      continue;
    }
    const h = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(l);
    if (h) { out.push({ t: "h", level: h[1].length, text: h[2] }); i++; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) { out.push({ t: "hr" }); i++; continue; }
    if (/^\s*>/.test(l)) {
      const q: string[] = [];
      for (; i < lines.length && /^\s*>/.test(lines[i]); i++) q.push(lines[i].replace(/^\s*>\s?/, ""));
      out.push({ t: "quote", lines: q });
      continue;
    }
    if (tableAt(lines, i)) {
      const head = cells(l), rows: string[][] = [];
      for (i += 2; i < lines.length && lines[i].includes("|") && lines[i].trim(); i++) rows.push(cells(lines[i]));
      out.push({ t: "table", head, rows });
      continue;
    }
    const m = LIST.exec(l);
    if (m) {
      const base = m[1].length, ordered = /\d/.test(m[2]);
      const items: Item[] = [];
      while (i < lines.length) {
        const x = LIST.exec(lines[i]);
        if (x && x[1].length <= base + 1 && /\d/.test(x[2]) === ordered) {
          items.push({ text: [x[3]], children: [] });
          i++;
          continue;
        }
        if (!items.length) break;
        // deeper lines (a nested list or a continuation) belong to the last item
        const sub: string[] = [];
        while (i < lines.length && lines[i].trim() && (lines[i].length - lines[i].trimStart().length) > base) {
          sub.push(lines[i].slice(Math.min(base + 2, lines[i].length - lines[i].trimStart().length)));
          i++;
        }
        if (!sub.length) break;
        const last = items[items.length - 1];
        if (LIST.test(sub[0])) last.children.push(...parse(sub));
        else last.text.push(...sub.map((s) => s.trim()));
      }
      out.push({ t: "list", ordered, start: ordered ? parseInt(m[2], 10) || 1 : 1, items });
      continue;
    }
    const p: string[] = [];
    for (; i < lines.length && lines[i].trim() && !LIST.test(lines[i]) && !/^(#{1,6})\s|^\s*```|^\s*>/.test(lines[i])
      && !tableAt(lines, i); i++)
      p.push(lines[i]);
    if (!p.length) { p.push(l); i++; }
    out.push({ t: "p", lines: p });
  }
  return out;
}
