// Small Markdown renderer for the assistant's replies: builds React elements (never raw HTML), so
// text from the model cannot inject markup. Covers what the assistant writes: headings, paragraphs,
// bold/italic, inline code, fenced code, lists (nested by indent), quotes, rules, tables, links.
import type { ReactNode } from "react";

import { parse, type Block } from "./markdownParse";

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
