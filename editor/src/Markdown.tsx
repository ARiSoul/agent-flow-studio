// Small Markdown renderer for the assistant's replies: builds React elements (never raw HTML), so
// text from the model cannot inject markup. Covers what the assistant writes: headings, paragraphs,
// bold/italic, inline code, fenced code, lists (nested by indent), quotes, rules, tables, links.
import type { ReactNode } from "react";

type Block =
  | { t: "p"; lines: string[] }
  | { t: "h"; level: number; text: string }
  | { t: "code"; text: string }
  | { t: "quote"; lines: string[] }
  | { t: "hr" }
  | { t: "list"; ordered: boolean; start: number; items: Item[] }
  | { t: "table"; head: string[]; rows: string[][] };
type Item = { text: string[]; children: Block[] };

const LIST = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const cells = (l: string) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

function parse(lines: string[]): Block[] {
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
    if (l.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
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
    for (; i < lines.length && lines[i].trim() && !LIST.test(lines[i]) && !/^(#{1,6})\s|^\s*```|^\s*>/.test(lines[i]); i++)
      p.push(lines[i]);
    if (!p.length) { p.push(l); i++; }
    out.push({ t: "p", lines: p });
  }
  return out;
}

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+?\*\*|__[^_\n]+?__)|(\*[^*\s][^*\n]*?\*|(?<![A-Za-z0-9])_[^_\s][^_\n]*?_(?![A-Za-z0-9]))|(\[[^\]\n]+\]\([^)\s]+\))/;

function inline(text: string, key = "i"): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text, n = 0;
  while (rest) {
    const m = INLINE.exec(rest);
    if (!m) { out.push(rest); break; }
    if (m.index) out.push(rest.slice(0, m.index));
    const s = m[0], k = `${key}-${n++}`;
    if (m[1]) out.push(<code key={k}>{s.slice(1, -1)}</code>);
    else if (m[2]) out.push(<strong key={k}>{inline(s.slice(2, -2), k)}</strong>);
    else if (m[3]) out.push(<em key={k}>{inline(s.slice(1, -1), k)}</em>);
    else {
      const [, label, url] = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(s)!;
      out.push(/^https?:\/\//i.test(url)
        ? <a key={k} href={url} target="_blank" rel="noopener noreferrer">{inline(label, k)}</a>
        : <span key={k}>{inline(label, k)}</span>);
    }
    rest = rest.slice(m.index + s.length);
  }
  return out;
}

const lines = (ls: string[], key: string) =>
  ls.flatMap((l, j) => (j ? [<br key={`${key}-br${j}`} />, ...inline(l, `${key}-${j}`)] : inline(l, `${key}-${j}`)));

function render(blocks: Block[], key = "b"): ReactNode[] {
  return blocks.map((b, i) => {
    const k = `${key}${i}`;
    switch (b.t) {
      case "p": return <p key={k}>{lines(b.lines, k)}</p>;
      case "h": {
        const H = (`h${Math.min(b.level + 2, 6)}`) as "h3" | "h4" | "h5" | "h6";  // smaller than the page's
        return <H key={k} className="md-h">{inline(b.text, k)}</H>;
      }
      case "code": return <pre key={k} className="mono">{b.text}</pre>;
      case "quote": return <blockquote key={k}>{render(parse(b.lines), k)}</blockquote>;
      case "hr": return <hr key={k} />;
      case "table": return (
        <div key={k} className="md-table"><table>
          <thead><tr>{b.head.map((c, j) => <th key={j}>{inline(c, `${k}h${j}`)}</th>)}</tr></thead>
          <tbody>{b.rows.map((r, ri) => <tr key={ri}>{r.map((c, j) => <td key={j}>{inline(c, `${k}r${ri}${j}`)}</td>)}</tr>)}</tbody>
        </table></div>);
      case "list": {
        const L = b.ordered ? "ol" : "ul";
        return <L key={k} start={b.ordered && b.start !== 1 ? b.start : undefined}>
          {b.items.map((it, j) => <li key={j}>{lines(it.text, `${k}l${j}`)}{render(it.children, `${k}l${j}c`)}</li>)}
        </L>;
      }
    }
  });
}

export function Markdown({ text }: { text: string }) {
  return <div className="md">{render(parse(text.replace(/\r\n/g, "\n").split("\n")))}</div>;
}
