// In-app dialogs (instead of the browser's confirm/prompt): themed, translated, keyboard-friendly.
// useDialog().confirm(...) → Promise<boolean>; useDialog().form(...) → Promise<values | null>.
import {
  createContext, useCallback, useContext, useEffect, useRef, useState,
  type KeyboardEvent, type ReactNode,
} from "react";
import { useI18n } from "./i18n";

export interface ConfirmOpts { title: string; message?: string; ok?: string; danger?: boolean }
export interface FieldOpts {
  key: string; label: string; value?: string; placeholder?: string; hint?: string;
  /** A select instead of a text input. */
  options?: { value: string; label: string }[];
  /** A multi-line text area (Enter adds a line; Ctrl+Enter accepts the dialog). */
  multiline?: boolean;
  /** A checkbox: the value is "1" when ticked, "" otherwise. */
  checkbox?: boolean;
  /** Returns an error text, or null when the value is valid. */
  validate?: (v: string) => string | null;
}
export interface FormOpts { title: string; message?: string; ok?: string; fields: FieldOpts[] }

interface Dialogs {
  confirm: (o: ConfirmOpts) => Promise<boolean>;
  form: (o: FormOpts) => Promise<Record<string, string> | null>;
}

type Open =
  | { kind: "confirm"; opts: ConfirmOpts; done: (v: boolean) => void }
  | { kind: "form"; opts: FormOpts; done: (v: Record<string, string> | null) => void };

const Ctx = createContext<Dialogs | null>(null);

export function DialogProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState<Open | null>(null);
  const confirm = useCallback((opts: ConfirmOpts) => new Promise<boolean>((resolve) =>
    setOpen({ kind: "confirm", opts, done: (v) => { setOpen(null); resolve(v); } })), []);
  const form = useCallback((opts: FormOpts) => new Promise<Record<string, string> | null>((resolve) =>
    setOpen({ kind: "form", opts, done: (v) => { setOpen(null); resolve(v); } })), []);
  return (
    <Ctx.Provider value={{ confirm, form }}>
      {children}
      {open && <DialogBox open={open} />}
    </Ctx.Provider>
  );
}

export function useDialog(): Dialogs {
  const v = useContext(Ctx);
  if (!v) throw new Error("useDialog outside DialogProvider");
  return v;
}

function DialogBox({ open }: { open: Open }) {
  const { t } = useI18n();
  const fields = open.kind === "form" ? open.opts.fields : [];
  const [values, setValues] = useState<Record<string, string>>(
    () => Object.fromEntries(fields.map((f) => [f.key, f.value ?? ""])));
  const [touched, setTouched] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const restore = useRef<Element | null>(document.activeElement);

  useEffect(() => {
    // destructive confirm: focus Cancel, so Enter does not delete by accident
    const first = box.current?.querySelector<HTMLElement>(
      open.kind === "confirm" && open.opts.danger ? "button.dialog-cancel" : "input, select, button.primary");
    first?.focus();
    if (first instanceof HTMLInputElement) first.select();
    const back = restore.current;
    return () => { if (back instanceof HTMLElement) back.focus(); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- once, when the dialog opens

  const errors = Object.fromEntries(fields.map((f) => [f.key, f.validate?.(values[f.key] ?? "") ?? null]));
  const valid = Object.values(errors).every((e) => !e);
  const cancel = () => (open.kind === "confirm" ? open.done(false) : open.done(null));
  const accept = () => {
    if (open.kind === "confirm") return open.done(true);
    setTouched(true);
    if (valid) open.done(Object.fromEntries(Object.entries(values).map(([k, v]) => [k, v.trim()])));
  };
  const { title, message } = open.opts;
  const danger = open.kind === "confirm" && open.opts.danger;
  const okLabel = open.opts.ok ?? t("dialog.ok");

  return (
    <div className="modal dialog-layer" onMouseDown={(e) => e.target === e.currentTarget && cancel()}>
      <div ref={box} className="modal-box dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title"
        onKeyDown={(e) => {
          // stops here: an Escape must not also close the window behind (e.g. Connectors)
          e.stopPropagation();
          if (e.key === "Escape") { e.preventDefault(); cancel(); }
          const inArea = e.target instanceof HTMLTextAreaElement;
          if (e.key === "Enter" && !(e.target instanceof HTMLButtonElement) && (!inArea || e.ctrlKey || e.metaKey)) {
            e.preventDefault(); accept();
          }
          if (e.key === "Tab") trapFocus(e, box.current);
        }}>
        <h2 id="dialog-title" className="dialog-title">{title}</h2>
        {message && <p className="dialog-message">{message}</p>}
        {fields.map((f) => f.checkbox ? (
          <label key={f.key} className="check">
            <input type="checkbox" checked={values[f.key] === "1"}
              onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.checked ? "1" : "" }))} />
            {f.label}
          </label>
        ) : (
          <label key={f.key} className="field">
            <span className="field-label">{f.label}</span>
            {f.multiline
              ? <textarea rows={f.key === "instructions" ? 7 : 3} value={values[f.key] ?? ""} placeholder={f.placeholder}
                className={touched && errors[f.key] ? "bad" : ""}
                onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))} />
              : f.options
              ? <select value={values[f.key] ?? ""} onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}>
                {f.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
              : <input value={values[f.key] ?? ""} placeholder={f.placeholder}
                className={touched && errors[f.key] ? "bad" : ""}
                onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))} />}
            {touched && errors[f.key]
              ? <span className="field-err">{errors[f.key]}</span>
              : f.hint && <span className="field-hint">{f.hint}</span>}
          </label>
        ))}
        <div className="dialog-actions">
          <button className="dialog-cancel" onClick={cancel}>{t("dialog.cancel")}</button>
          <button className={danger ? "danger strong" : "primary"} onClick={accept}>{okLabel}</button>
        </div>
      </div>
    </div>
  );
}

function trapFocus(e: KeyboardEvent, box: HTMLElement | null) {
  const items = [...(box?.querySelectorAll<HTMLElement>("input, select, textarea, button") ?? [])];
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}
