// In-app context menu (right click, Shift+F10 or the context-menu key): themed, translated and
// keyboard accessible (arrows, Home/End, Enter/Space, Escape; focus returns to where it was).
import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

export type MenuItem =
  | { kind?: "item"; label: string; onSelect: () => void; disabled?: boolean; danger?: boolean;
      hint?: string; title?: string;
      /** A short reason shown under the label (a disabled item says why; it has no tooltip on hover). */
      note?: string }
  | { kind: "sep" }
  | { kind: "label"; label: string }
  /** A row of small buttons (e.g. icons); each one is a menu item for the keyboard. */
  | { kind: "grid"; label: string; wide?: boolean; cells: { key: string; title: string; content: ReactNode;
      on?: boolean; onSelect: () => void }[] };

export interface MenuState { x: number; y: number; title?: string; items: MenuItem[] }

export function ContextMenu({ menu, onClose }: { menu: MenuState; onClose: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  const restore = useRef<Element | null>(document.activeElement);
  const [pos, setPos] = useState({ x: menu.x, y: menu.y });

  // keep the menu inside the window (open to the left / upwards when needed)
  useLayoutEffect(() => {
    const r = box.current?.getBoundingClientRect();
    if (!r) return;
    const x = Math.max(4, Math.min(menu.x, window.innerWidth - r.width - 4));
    const y = Math.max(4, Math.min(menu.y, window.innerHeight - r.height - 4));
    setPos({ x, y });
  }, [menu]);

  useEffect(() => {
    focusables(box.current)[0]?.focus();
    const back = restore.current;
    const away = (e: Event) => { if (!box.current?.contains(e.target as Node)) onClose(); };
    const close = () => onClose();
    window.addEventListener("mousedown", away, true);
    window.addEventListener("wheel", close, { passive: true });
    window.addEventListener("resize", close);
    window.addEventListener("blur", close);
    return () => {
      window.removeEventListener("mousedown", away, true);
      window.removeEventListener("wheel", close);
      window.removeEventListener("resize", close);
      window.removeEventListener("blur", close);
      if (back instanceof HTMLElement && document.contains(back)) back.focus();
    };
  }, [menu, onClose]);

  const run = (fn: () => void) => { onClose(); setTimeout(fn, 0); };
  const onKey = (e: KeyboardEvent) => {
    e.stopPropagation();
    const items = focusables(box.current);
    const i = items.indexOf(document.activeElement as HTMLElement);
    const go = (n: number) => { e.preventDefault(); items[(n + items.length) % items.length]?.focus(); };
    if (e.key === "Escape" || (e.key === "F10" && e.shiftKey)) { e.preventDefault(); onClose(); }
    else if (e.key === "Tab") { e.preventDefault(); onClose(); }
    else if (e.key === "ArrowDown") go(i + 1);
    else if (e.key === "ArrowUp") go(i - 1);
    else if (e.key === "ArrowRight" && items[i]?.dataset.cell) go(i + 1);
    else if (e.key === "ArrowLeft" && items[i]?.dataset.cell) go(i - 1);
    else if (e.key === "Home") go(0);
    else if (e.key === "End") go(items.length - 1);
  };

  return (
    <div ref={box} className="ctx-menu" role="menu" aria-label={menu.title} style={{ left: pos.x, top: pos.y }}
      onKeyDown={onKey} onContextMenu={(e) => e.preventDefault()}>
      {menu.title && <div className="ctx-title">{menu.title}</div>}
      {menu.items.map((it, k) => {
        if (it.kind === "sep") return <div key={k} className="ctx-sep" role="separator" />;
        if (it.kind === "label") return <div key={k} className="ctx-label">{it.label}</div>;
        if (it.kind === "grid") return (<div key={k}>
          <div className="ctx-label" aria-hidden="true">{it.label}</div>
          <div className={`ctx-grid${it.wide ? " wide" : ""}`} role="group" aria-label={it.label}>
            {it.cells.map((c) => (
              <button key={c.key} type="button" role="menuitemradio" aria-checked={!!c.on} title={c.title}
                aria-label={c.title} data-cell="1" tabIndex={-1} className={`ctx-cell${c.on ? " on" : ""}`}
                onClick={() => run(c.onSelect)}>{c.content}</button>))}
          </div>
        </div>);
        return (
          <button key={k} type="button" role="menuitem" tabIndex={-1} disabled={it.disabled} title={it.title}
            aria-disabled={it.disabled} className={`ctx-item${it.danger ? " danger" : ""}`}
            onClick={() => !it.disabled && run(it.onSelect)}>
            <span>{it.label}</span>{it.hint && <kbd>{it.hint}</kbd>}
            {it.note && <small className="ctx-note">{it.note}</small>}
          </button>);
      })}
    </div>
  );
}

const focusables = (box: HTMLElement | null) =>
  [...(box?.querySelectorAll<HTMLElement>("button:not(:disabled)") ?? [])];
