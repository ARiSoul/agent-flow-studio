// Small parts shared by the views of the Run panel.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { ContextMenu, type MenuItem, type MenuState } from "./ContextMenu";
import { useI18n, type I18n } from "./i18n";
import { Markdown } from "./Markdown";

export const statusText = (i18n: I18n, s: unknown) => i18n.tOr(`run.status.${String(s)}`, String(s));
export const RUN_ICON: Record<string, string> = { running: "●", gate: "⏸", paused: "⏸", decided: "⧗", error: "✗", stopped: "■", done: "✓", lost: "?" };
export const pretty = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v, null, 2));
/** A file of a run (GET /api/runs/<run>/files) and how to show it. */
export type RunFile = { path: string; size: number; kind: "image" | "video" | "text" | "other" };
const IMAGE = /\.(png|jpe?g|gif|webp)$/i;
const VIDEO = /\.(mp4|m4v|mov|webm)$/i;
/** The URL of a run file's bytes (the server checks it stays in the run's files). */
export const fileUrl = (run: string, path: string) => `/api/runs/${encodeURIComponent(run)}/file?path=${encodeURIComponent(path)}`;
/** A `file` output ({name, path, size}) that is an image or a video: its kind and path. */
export function mediaOf(v: unknown): { kind: "image" | "video"; path: string } | null {
  const p = v && typeof v === "object" && typeof (v as { path?: unknown }).path === "string" ? (v as { path: string }).path : null;
  return !p ? null : IMAGE.test(p) ? { kind: "image", path: p } : VIDEO.test(p) ? { kind: "video", path: p } : null;
}
export const oneLine = (v: unknown, n = 90) => { const s = pretty(v).replace(/\s+/g, " ").trim(); return s.length > n ? `${s.slice(0, n)}…` : s; };

export function clock(ts: number | undefined, lang: string): string {
  return ts ? new Date(ts * 1000).toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" }) : "";
}
export function when(ts: number | undefined, lang: string): string {
  if (!ts) return "";
  const d = new Date(ts * 1000), now = new Date();
  return d.toDateString() === now.toDateString() ? clock(ts, lang)
    : d.toLocaleDateString(lang, { day: "2-digit", month: "short" }) + " " + clock(ts, lang);
}
export const fullTime = (ts: number | undefined, lang: string) => (ts ? new Date(ts * 1000).toLocaleString(lang) : "");

/** The colored state of a run: an icon and a word (never color alone). */
export function StatusChip({ status, big }: { status: string; big?: boolean }) {
  const i18n = useI18n();
  return <span className={`rp-chip st-${status}${big ? " big" : ""}`}>
    <span aria-hidden="true">{RUN_ICON[status] ?? "•"}</span> {statusText(i18n, status)}</span>;
}

/** A long text, cut to a few lines, with "Show more". */
export function Clamp({ children, lines = 4 }: { children: ReactNode; lines?: number }) {
  const { t } = useI18n();
  const ref = useRef<HTMLDivElement>(null);
  const [long, setLong] = useState(false);
  const [open, setOpen] = useState(false);
  useEffect(() => { const el = ref.current; if (el && !open) setLong(el.scrollHeight > el.clientHeight + 4); });
  return <div className="rp-clamp-wrap">
    <div ref={ref} className={`rp-clamp${open ? " open" : ""}`} style={{ maxHeight: open ? undefined : `${lines * 1.35}em` }}>{children}</div>
    {(long || open) && <button className="link small" aria-expanded={open} onClick={() => setOpen(!open)}>
      {t(open ? "rp.less" : "rp.more")}</button>}
  </div>;
}

/** A "⋯" button that opens a small menu of labelled actions. */
export function MoreMenu({ label, items, className, children }: { label: string; items: MenuItem[]; className?: string; children?: ReactNode }) {
  const [menu, setMenu] = useState<MenuState | null>(null);
  return <>
    <button className={`${children ? "" : "icon "}small ${className ?? ""}`} aria-haspopup="menu" aria-label={label} title={label}
      onClick={(e) => { const r = (e.currentTarget as HTMLElement).getBoundingClientRect(); setMenu({ x: r.right - 180, y: r.bottom + 2, title: label, items }); }}>{children ?? "⋯"}</button>
    {menu && <ContextMenu menu={menu} onClose={() => setMenu(null)} />}
  </>;
}

/** One output (or file) in a large view: Markdown formatted (or the plain text), with copy and download. */
export function OutputViewer({ title, value, md, media, copied, onClose, onCopy, onDownload }: {
  title: string; value: unknown; md: boolean; copied: boolean; onClose: () => void; onCopy: () => void; onDownload: () => void;
  /** A run file to show as it is: an image (fit or real size), a video, or text with line numbers. */
  media?: { kind: "image" | "video" | "text"; url: string };
}) {
  const { t } = useI18n();
  const [raw, setRaw] = useState(typeof value === "string" ? !md : false);
  const [full, setFull] = useState(false);  // an image at its real size
  const [text, setText] = useState<string | null>(null);
  useEffect(() => {
    if (media?.kind !== "text") return;
    fetch(media.url).then((r) => (r.ok ? r.text() : "")).then((x) => setText(x.slice(0, 400_000))).catch(() => setText(""));
  }, [media]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  const text0 = pretty(value);
  return (
    <div className="modal" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal-box out-viewer" role="dialog" aria-label={title}>
        <div className="panel-head">
          <h2><code>{title}</code></h2>
          {!media && (md || typeof value !== "string") && <button className="small" onClick={() => setRaw((x) => !x)}>{t(raw ? "run.viewFormatted" : "run.viewRaw")}</button>}
          <button className="small" onClick={onCopy}>{t(copied ? "run.outCopied" : "run.outCopy")}</button>
          <button className="small" onClick={onDownload}>{t("run.outDownload")}</button>
          <button className="small" onClick={onClose}>{t("conn.close")}</button>
        </div>
        <div className="out-viewer-body">
          {media?.kind === "image" ? (
            <div className={`rp-img-view${full ? " full" : ""}`}>
              <button className="small" onClick={() => setFull(!full)}>{t(full ? "rp.imgFit" : "rp.imgReal")}</button>
              <img src={media.url} alt={title} className="checker" />
            </div>)
            : media?.kind === "video" ? <video src={media.url} controls className="rp-video" />
            : media?.kind === "text" ? (text === null ? <p className="muted small">…</p> : (
              <table className="rp-code mono small"><tbody>{text.replace(/\r?\n$/, "").split(/\r?\n/).map((l, n) => (
                <tr key={n}><td className="ln">{n + 1}</td><td className="tx">{l}</td></tr>))}</tbody></table>))
            : typeof value === "string" ? (raw ? <pre className="mono">{text0}</pre> : <Markdown text={value} />)
            : raw ? <pre className="mono">{text0}</pre> : <JsonTree value={value} />}
        </div>
      </div>
    </div>
  );
}

/** A JSON value as a tree: objects and lists fold, the first two levels start open. */
export function JsonTree({ value, depth = 0, name }: { value: unknown; depth?: number; name?: string }) {
  const label = name !== undefined ? <span className="jt-key">{name}: </span> : null;
  if (value === null || typeof value !== "object") {
    const cls = typeof value === "string" ? "jt-str" : typeof value === "number" ? "jt-num" : "jt-lit";
    return <div className="jt-row">{label}<span className={cls}>{JSON.stringify(value)}</span></div>;
  }
  const list = Array.isArray(value);
  const entries = list ? (value as unknown[]).map((v, i) => [String(i), v] as const) : Object.entries(value as Record<string, unknown>);
  return (
    <details className="jt" open={depth < 2}>
      <summary>{label}<span className="muted">{list ? `[${entries.length}]` : `{${entries.length}}`}</span></summary>
      <div className="jt-kids">{entries.map(([k, v]) => <JsonTree key={k} value={v} depth={depth + 1} name={k} />)}</div>
    </details>);
}

/** Copy to the clipboard with a short "Copied" state per key. */
export function useCopy(): [string | null, (text: string, key: string) => void] {
  const [copied, setCopied] = useState<string | null>(null);
  const copy = (text: string, key: string) => {
    navigator.clipboard?.writeText(text).then(() => {
      setCopied(key);
      setTimeout(() => setCopied((c) => (c === key ? null : c)), 1500);
    }).catch(() => undefined);
  };
  return [copied, copy];
}

export function download(name: string, text: string, type = "text/plain") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
