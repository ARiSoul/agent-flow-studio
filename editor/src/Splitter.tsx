// Drag handle that resizes a panel (mouse, touch or keyboard arrows; double click = default size).
import { useRef, type KeyboardEvent, type PointerEvent } from "react";

export type PanelSizes = { pal: number; insp: number; bottom: number };
export const DEFAULT_SIZES: PanelSizes = { pal: 210, insp: 380, bottom: 190 };
export const LIMITS: Record<keyof PanelSizes, [number, number]> = {
  pal: [150, 420], insp: [280, 900], bottom: [90, 600],
};
const KEY = "afs.panels";

/** Sizes saved by this browser (only the ones the user changed; the rest follow the CSS). */
export function loadSizes(): Partial<PanelSizes> {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) ?? "{}") as Partial<PanelSizes>;
    return Object.fromEntries(Object.entries(v).filter(([k, n]) =>
      k in LIMITS && typeof n === "number" && n >= LIMITS[k as keyof PanelSizes][0]
        && n <= LIMITS[k as keyof PanelSizes][1])) as Partial<PanelSizes>;
  } catch { return {}; }
}
export function saveSizes(s: Partial<PanelSizes>) {
  try { localStorage.setItem(KEY, JSON.stringify(s)); } catch { /* storage not available */ }
}

export const clampSize = (k: keyof PanelSizes, n: number) =>
  Math.round(Math.min(LIMITS[k][1], Math.max(LIMITS[k][0], n)));

/** `edge`: where the handle sits on its panel ("left" of the right panel, "right" of the palette,
 * "top" of the bottom panel). `size` is the current size in px; `onSize(null)` = back to default. */
export function Splitter({ edge, size, onSize, label }: {
  edge: "left" | "right" | "top"; size: number; label: string; onSize: (px: number | null) => void;
}) {
  const start = useRef<{ pos: number; size: number } | null>(null);
  const vertical = edge !== "top";
  // the panel's real size (the CSS default may differ per screen width)
  const measured = (el: HTMLElement) => {
    const r = el.parentElement?.getBoundingClientRect();
    return r ? (vertical ? r.width : r.height) : size;
  };
  const down = (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    start.current = { pos: vertical ? e.clientX : e.clientY, size: measured(e.currentTarget) };
    document.body.classList.add(vertical ? "resizing-x" : "resizing-y");
  };
  const move = (e: PointerEvent<HTMLDivElement>) => {
    const s = start.current;
    if (!s) return;
    const d = (vertical ? e.clientX : e.clientY) - s.pos;
    // the right panel and the bottom panel grow when the handle moves left / up
    onSize(s.size + (edge === "right" ? d : -d));
  };
  const up = (e: PointerEvent<HTMLDivElement>) => {
    start.current = null;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
    document.body.classList.remove("resizing-x", "resizing-y");
  };
  const key = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 50 : 10;
    const grow = edge === "right" ? ["ArrowRight", "ArrowDown"] : edge === "left" ? ["ArrowLeft"] : ["ArrowUp"];
    const shrink = edge === "right" ? ["ArrowLeft", "ArrowUp"] : edge === "left" ? ["ArrowRight"] : ["ArrowDown"];
    const now = measured(e.currentTarget);
    if (grow.includes(e.key)) { e.preventDefault(); onSize(now + step); }
    else if (shrink.includes(e.key)) { e.preventDefault(); onSize(now - step); }
    else if (e.key === "Home" || e.key === "Enter") { e.preventDefault(); onSize(null); }
  };
  return (
    <div className={`splitter ${edge}`} role="separator" tabIndex={0} aria-label={label} title={label}
      aria-orientation={vertical ? "vertical" : "horizontal"} aria-valuenow={Math.round(size)}
      onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up}
      onDoubleClick={() => onSize(null)} onKeyDown={key} />
  );
}
