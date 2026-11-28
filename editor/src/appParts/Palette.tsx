/** The left sidebar: Add (a grid of node tiles), the agent library, and the internal tools
 * (reference). Each section collapses; the state is kept in this browser. */
import { useState, type ReactNode } from "react";
import { AgentIcon, Icon } from "../icons";
import { useI18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import type { Catalog, LibraryAgent } from "../types";
import { PALETTE, type PaletteKind } from "./shared";

const OPEN_KEY = "afs.palSections";
const ICON: Record<PaletteKind, string> = { agent: "robot", human: "hand", stop: "stop", decision: "git", action: "wrench", subflow: "doc" };
const SEARCH_FROM = 7;  // the library gets a search field from this many agents

function loadOpen(): Record<string, boolean> {
  try { return JSON.parse(localStorage.getItem(OPEN_KEY) ?? "{}"); } catch { return {}; }
}

function Section({ id, title, open, toggle, extra, children }: {
  id: string; title: string; open: boolean; toggle: () => void; extra?: ReactNode; children: ReactNode;
}) {
  return (
    <section className="pal-section">
      <div className="pal-head">
        <button className="pal-toggle" aria-expanded={open} aria-controls={`pal-${id}`} onClick={toggle}>
          <span className="pal-chev" aria-hidden="true">{open ? "▾" : "▸"}</span>{title}</button>
        {extra}
      </div>
      <div id={`pal-${id}`} hidden={!open}>{children}</div>
    </section>
  );
}

export function Palette({ catalog, library, addNode, insertLibrary, manage }: {
  catalog: Catalog; library: LibraryAgent[];
  addNode: (kind: PaletteKind) => void; insertLibrary: (l: LibraryAgent) => void; manage: () => void;
}) {
  const { t } = useI18n();
  const tk = (k: string) => t(k as MessageKey);
  const [open, setOpen] = useState<Record<string, boolean>>(() => ({ add: true, lib: true, tools: false, ...loadOpen() }));
  const [hint, setHint] = useState<PaletteKind | null>(null);
  const [q, setQ] = useState("");
  const toggle = (k: string) => setOpen((o) => {
    const n = { ...o, [k]: !o[k] };
    try { localStorage.setItem(OPEN_KEY, JSON.stringify(n)); } catch { /* storage not available */ }
    return n;
  });
  const query = q.trim().toLowerCase();
  const shown = query ? library.filter((l) => `${l.title} ${l.description} ${l.id}`.toLowerCase().includes(query)) : library;
  return (
    <aside className="palette" aria-label={t("palette.label")}>
      <Section id="add" title={t("palette.add")} open={open.add} toggle={() => toggle("add")}>
        <div className="pal-grid">
          {PALETTE.map((p) => (
            <div key={p.kind} className={`pal tile ${p.kind}`} draggable role="button" tabIndex={0}
              title={t(p.hint)} aria-label={t(p.label)} aria-description={t(p.hint)}
              onDragStart={(e) => { e.dataTransfer.setData("application/x-flow-node", p.kind); e.dataTransfer.effectAllowed = "move"; }}
              onMouseEnter={() => setHint(p.kind)} onMouseLeave={() => setHint(null)}
              onFocus={() => setHint(p.kind)} onBlur={() => setHint(null)}
              onClick={() => addNode(p.kind)}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); addNode(p.kind); } }}>
              <Icon id={ICON[p.kind]} size={16} className="pal-icon" />
              <b>{tk(`palette.short.${p.kind}`)}</b>
            </div>))}
        </div>
        <p className="pal-hint" aria-hidden="true">{hint ? t(PALETTE.find((p) => p.kind === hint)!.hint) : t("palette.tileHint")}</p>
        <details className="pal-help"><summary>{t("palette.howTo")}</summary><p>{t("palette.help")}</p></details>
      </Section>

      <Section id="lib" title={t("palette.library")} open={open.lib} toggle={() => toggle("lib")}
        extra={<button className="link small" onClick={manage} title={t("libm.manage")}>{t("libm.manage")}</button>}>
        {library.length >= SEARCH_FROM && <input className="pal-search" type="search" value={q} placeholder={t("palette.searchLib")}
          aria-label={t("palette.searchLib")} onChange={(e) => setQ(e.target.value)} />}
        {library.length === 0 && <p className="muted small">{t("palette.libraryEmpty")}</p>}
        <div className="pal-lib-list">
          {shown.map((l) => (
            <div key={l.id} className="pal lib" draggable role="button" tabIndex={0} title={`${l.description}\n${t("palette.libraryHint")}`}
              onDragStart={(e) => { e.dataTransfer.setData("application/x-flow-library", l.id); e.dataTransfer.effectAllowed = "copy"; }}
              onClick={() => insertLibrary(l)}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); insertLibrary(l); } }}>
              <span className="pal-lib-head"><AgentIcon icon={l.icon} size={14} /><b title={l.title}>{l.title}</b>
                {l.source !== "core" && <span className="pal-src">{l.source === "local" ? t("palette.libLocal") : l.source}</span>}</span>
              <span className="pal-desc">{l.description}</span>
            </div>))}
        </div>
      </Section>

      <Section id="tools" title={t("palette.tools")} open={open.tools} toggle={() => toggle("tools")}>
        <ul className="catalog">
          {catalog.servers.map((s) => (
            <li key={s}><b>{s}</b>
              <ul>{catalog.tools.filter((x) => x.server === s).map((x) => <li key={x.id} title={x.description}>{x.name}</li>)}</ul>
            </li>))}
        </ul>
      </Section>
    </aside>
  );
}
