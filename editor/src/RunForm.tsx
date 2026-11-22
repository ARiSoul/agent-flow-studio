// Run form: one field per typed value (flow inputs, or the inputs of the agent under test).
import { useState, type ReactNode } from "react";
import { useI18n } from "./i18n";
import { typeTag } from "./nodes";
import type { IOTypeRaw } from "./types";

export type FileVal = { name: string; content_b64: string; size: number };
/** What a field holds while editing: text, a checkbox, choices or files. */
export type FormVal = string | boolean | string[] | FileVal[];

const MD_SPLIT = /^\s*---\s*$/m;  // markdown lists: items separated by a line with ---

const show = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v));

/** Initial form value from a value (a default, or a saved value). Files cannot be reused. */
export function formValue(t: IOTypeRaw, v: unknown): FormVal {
  const k = t.type ?? "text";
  if (k === "file") return [];
  if (k === "boolean") return v === true || v === "true";
  if (v === undefined || v === null) return k === "choice" && t.multiple ? [] : "";
  if (k === "choice" && t.multiple) return Array.isArray(v) ? v.map(String) : [String(v)];
  if (k === "json") return JSON.stringify(v, null, 2);
  if (t.multiple && Array.isArray(v)) return v.map(show).join(k === "markdown" ? "\n---\n" : "\n");
  if (k === "datetime" && typeof v === "string") return v.slice(0, 16);  // the picker has no seconds or zone
  return show(v);
}

/** The value to send, or an error (only JSON is parsed here; the server checks the rest). */
export function sendValue(t: IOTypeRaw, v: FormVal): { value?: unknown; error?: "json" } {
  const k = t.type ?? "text";
  if (k === "boolean") return { value: v === true };
  if (k === "file") {
    const files = (v as FileVal[]).map(({ name, content_b64 }) => ({ name, content_b64 }));
    return { value: files.length ? (t.multiple ? files : files[0]) : undefined };
  }
  if (Array.isArray(v)) return { value: v.length ? v : undefined };
  const s = String(v);
  if (!s.trim()) return {};
  if (k === "json") {
    try { return { value: JSON.parse(s) }; } catch { return { error: "json" }; }
  }
  const num = (x: string) => (x.trim() !== "" && Number.isFinite(Number(x)) ? Number(x) : x.trim());
  if (t.multiple) {
    const items = (k === "markdown" ? s.split(MD_SPLIT) : s.split("\n")).map((x) => x.trim()).filter(Boolean);
    return { value: k === "number" || k === "integer" ? items.map(num) : items };
  }
  if (k === "number" || k === "integer") return { value: num(s) };
  return { value: k === "markdown" ? s : s.trim() };
}

/** The server accepts letters, digits, '.', '-' and '_' in file names. */
const safeName = (n: string) => n.replace(/[^A-Za-z0-9_.-]/g, "_").replace(/\.{2,}/g, ".").slice(-80) || "file";

function readFiles(list: FileList): Promise<FileVal[]> {
  return Promise.all([...list].map((f) => new Promise<FileVal>((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => {
      const s = String(r.result);
      resolve({ name: safeName(f.name), content_b64: s.slice(s.indexOf(",") + 1), size: f.size });
    };
    r.onerror = () => reject(r.error);
    r.readAsDataURL(f);
  })));
}

const kb = (n: number) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);

/** One field of the form, by type. */
export function ValueField({ name, t, value, onChange, error, disabled, saved, extra }: {
  name: string; t: IOTypeRaw; value: FormVal; onChange: (v: FormVal) => void; error?: string | null;
  disabled?: boolean; saved?: unknown; extra?: ReactNode;
}) {
  const { t: tr } = useI18n();
  const [readErr, setReadErr] = useState<string | null>(null);
  const k = t.type ?? "text";
  const required = t.required !== false && t.default === undefined;
  const label = t.label || name;
  const bad = error ? " bad" : "";
  const range = { min: t.min ?? undefined, max: t.max ?? undefined };
  let control;
  if (k === "boolean") {
    control = (
      <label className="check"><input type="checkbox" checked={value === true} disabled={disabled}
        onChange={(e) => onChange(e.target.checked)} /> {t.description || label}</label>);
  } else if (k === "choice" && t.multiple) {
    const chosen = new Set(value as string[]);
    control = (
      <div className="tools">{(t.options ?? []).map((o) => (
        <label key={o} className="check"><input type="checkbox" checked={chosen.has(o)} disabled={disabled}
          onChange={(e) => { const n = new Set(chosen); if (e.target.checked) n.add(o); else n.delete(o); onChange((t.options ?? []).filter((x) => n.has(x))); }} />
          {o}</label>))}
      </div>);
  } else if (k === "choice") {
    control = (
      <select className={bad} value={String(value)} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
        {(!required || !value) && <option value="">{tr("run.form.choose")}</option>}
        {(t.options ?? []).map((o) => <option key={o}>{o}</option>)}
      </select>);
  } else if (k === "file") {
    const files = value as FileVal[];
    const savedFiles = saved ? (Array.isArray(saved) ? saved : [saved]) as { name?: string }[] : [];
    control = (
      <>
        <input type="file" className={bad} disabled={disabled} multiple={!!t.multiple}
          accept={(t.accept ?? []).join(",") || undefined}
          onChange={(e) => {
            const list = e.target.files;
            setReadErr(null);
            if (!list?.length) return onChange([]);
            readFiles(list).then(onChange).catch((x) => setReadErr(String(x)));
          }} />
        {files.length > 0 && <span className="field-hint">{files.map((f) => `${f.name} (${kb(f.size)})`).join(", ")}</span>}
        {!files.length && savedFiles.length > 0 &&
          <span className="field-hint">{tr("run.form.savedFile", { names: savedFiles.map((f) => f.name ?? "?").join(", ") })}</span>}
        {readErr && <span className="field-err">{readErr}</span>}
      </>);
  } else if (k === "markdown" || k === "json" || t.multiple) {
    control = (
      <textarea className={`${k === "json" ? "mono" : ""}${bad}`} rows={k === "markdown" || k === "json" ? 4 : 3}
        disabled={disabled} value={String(value)} spellCheck={k !== "json"}
        placeholder={t.multiple ? tr(k === "markdown" ? "run.form.mdListHint" : "run.form.listHint") : undefined}
        onChange={(e) => onChange(e.target.value)} />);
  } else {
    const type = k === "number" || k === "integer" ? "number" : k === "date" ? "date"
      : k === "datetime" ? "datetime-local" : k === "url" ? "url" : "text";
    control = (
      <input type={type} className={`${k === "path" ? "mono" : ""}${bad}`} disabled={disabled} value={String(value)}
        {...(type === "number" ? { ...range, step: k === "integer" ? 1 : "any" } : {})}
        placeholder={k === "url" ? "https://" : k === "path" ? tr("run.form.pathHint") : undefined}
        onChange={(e) => onChange(e.target.value)} />);
  }
  return (
    <div className="field run-field">
      <span className="field-label">
        {k === "boolean" ? name : label}{required ? " *" : ""}
        <span className="port-tag">{typeTag(k, t.multiple)}</span>
      </span>
      {control}
      {extra}
      {error ? <span className="field-err">{error}</span>
        : t.description && k !== "boolean" && <span className="field-hint">{t.description}</span>}
    </div>
  );
}
