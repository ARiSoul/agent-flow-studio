/** Shared parts of the Inspector panels: props, small parsers, the prompt editor, the model note. */
import { useRef } from "react";
import { useI18n } from "../i18n";
import type { Catalog, Doc, Selection, Validation } from "../types";

export type Update = (fn: (d: Doc) => void) => void;
export interface Props {
  doc: Doc;
  sel: Selection;
  catalog: Catalog;
  val: Validation | null;
  update: Update;
  select: (s: Selection) => void;
  remove: (s: Selection) => void;
  testAgent?: (id: string) => void;
  /** Opens the Providers window (on one provider): from every model field. */
  openProviders?: (focus?: string) => void;
  flows?: string[];  // the other flows of this workspace (subflow nodes)
  library?: {
    state: (id: string) => string; has: (lib: string) => boolean;
    update: (id: string) => void; detach: (id: string) => void; save: (id: string) => void;
  };
}

export const AUTONOMY = ["approve", "approve_prod", "auto"] as const;

export const lines = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);
export const csv = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
export const kv = (s: string, sep: string) =>
  Object.fromEntries(lines(s).map((l) => {
    const i = l.indexOf(sep);
    return i < 0 ? [l, ""] : [l.slice(0, i).trim(), l.slice(i + 1).trim()];
  }));
export const unkv = (o: Record<string, string> | undefined, sep: string) =>
  Object.entries(o ?? {}).map(([k, v]) => `${k}${sep}${v}`).join("\n");

/** Inline prompt or in a prompts/*.md file (edited here and saved with the flow). */
/** The workspace's variables and roots as buttons that insert {vars.x} / {roots.x} at the cursor. */
export function VarChips({ catalog, onInsert }: { catalog?: Catalog; onInsert: (text: string) => void }) {
  const { t } = useI18n();
  const ws = catalog?.workspace;
  const items = [...(ws?.vars ?? []).map((v) => `{vars.${v}}`), ...(ws?.roots ?? []).map((r) => `{roots.${r}}`)];
  if (!items.length) return null;
  return (
    <div className="var-chips" role="group" aria-label={t("insp.vars")}>
      <span className="muted small" title={t("insp.varsHint")}>{t("insp.vars")}</span>
      {items.map((x) => <button key={x} type="button" className="chip mono" title={t("insp.varsInsert")}
        onMouseDown={(e) => e.preventDefault()} onClick={() => onInsert(x)}>{x}</button>)}
    </div>
  );
}

/** Inserts text at the cursor of a text field (or at the end). */
export function insertAt(el: HTMLTextAreaElement | HTMLInputElement | null, text: string, add: string): string {
  const a = el?.selectionStart ?? text.length, b = el?.selectionEnd ?? text.length;
  return text.slice(0, a) + add + text.slice(b);
}

export function PromptEditor({ owner, defaultPath, doc, update, pick, catalog }: {
  owner: { prompt?: string | null; prompt_file?: string | null };
  defaultPath: string;
  doc: Doc;
  update: Update;
  pick: (d: Doc) => { prompt?: string | null; prompt_file?: string | null };
  catalog?: Catalog;
}) {
  const { t } = useI18n();
  const ref = useRef<HTMLTextAreaElement>(null);
  const file = owner.prompt_file ?? null;
  const text = file ? doc.prompts[file] ?? "" : owner.prompt ?? "";
  const missing = file !== null && !(file in doc.prompts);
  return (
    <div className="prompt">
      <div className="seg">
        <button className={file ? "" : "on"} onClick={() => update((d) => {
          const o = pick(d);
          if (!o.prompt_file) return;
          o.prompt = d.prompts[o.prompt_file] ?? "";
          delete o.prompt_file;
        })}>{t("insp.inline")}</button>
        <button className={file ? "on" : ""} onClick={() => update((d) => {
          const o = pick(d);
          if (o.prompt_file) return;
          const path = defaultPath;
          if (!(path in d.prompts)) d.prompts[path] = o.prompt ?? "";
          o.prompt_file = path;
          delete o.prompt;
        })}>{t("insp.file")}</button>
        {file && <code className="path">{file}</code>}
      </div>
      {missing && <span className="field-err">{t("insp.fileMissing")}</span>}
      <textarea ref={ref} className="mono" rows={14} value={text} spellCheck={false}
        onChange={(e) => {
          const v = e.target.value;
          update((d) => {
            const o = pick(d);
            if (o.prompt_file) d.prompts[o.prompt_file] = v;
            else o.prompt = v;
          });
        }} />
      <VarChips catalog={catalog} onInsert={(x) => {
        const v = insertAt(ref.current, text, x);
        update((d) => { const o = pick(d); if (o.prompt_file) d.prompts[o.prompt_file] = v; else o.prompt = v; });
      }} />
    </div>
  );
}

/** Where a model runs: its provider (from `name/model`), a key that is missing, an unknown cost. */
function ProvidersLink({ open }: { open?: (focus?: string) => void }) {
  const { t } = useI18n();
  if (!open) return null;
  return <button type="button" className="link small" title={t("insp.providersTitle")}
    onClick={() => open()}>{t("insp.providers")}</button>;
}

export function ModelNote({ model, catalog, open }: { model?: string | null; catalog: Catalog; open?: (focus?: string) => void }) {
  const { t } = useI18n();
  const head = model && model.includes("/") ? model.split("/", 1)[0] : null;
  if (!head || !/^[a-z][a-z0-9-]{0,39}$/.test(head)) return <span className="model-note"><ProvidersLink open={open} /></span>;
  const p = (catalog.providers ?? []).find((x) => x.name === head && !x.error);
  if (!p) return <span className="field-err">{t("insp.providerUnknown", { name: head })}
    {open && <> <button type="button" className="link small" onClick={() => open()}>{t("insp.providers")}</button></>}</span>;
  return <span className="small muted">
    {open ? <button type="button" className="link small" onClick={() => open(p.name)}>
      {t("insp.providerRuns", { title: p.title ?? p.name, kind: p.kind ?? "" })}</button>
      : t("insp.providerRuns", { title: p.title ?? p.name, kind: p.kind ?? "" })}
    {p.key === "missing" && <span className="field-err"> {t("insp.providerNoKey")}</span>}
    {p.priced === false && <> · {t("insp.providerNoCost")}</>}
  </span>;
}

/** The tools that write (the same as WRITE_TOOLS in builder/catalog.py): a step that ran one is
 * not retried by itself after a temporary error. */
export const WRITE_TOOL_NAMES = ["workspace.edit", "workspace.write", "workspace.bash", "connections.http_write"];

