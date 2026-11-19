// Small form pieces shared by the Inspector and the IO editors.
import { useEffect, useState, type ReactNode } from "react";
import { useI18n } from "./i18n";

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

/** Field that only commits on leave (Enter or blur): used for names, which touch the links. */
export function CommitInput({ value, onCommit, pattern, placeholder, className }: {
  value: string; onCommit: (v: string) => string | null; pattern?: RegExp; placeholder?: string; className?: string;
}) {
  const { t } = useI18n();
  const [v, setV] = useState(value);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { setV(value); setErr(null); }, [value]);
  const commit = () => {
    if (v === value) return;
    if (pattern && !pattern.test(v)) return setErr(t("insp.slugError"));
    const e = onCommit(v);
    setErr(e);
    if (e) setV(value);
  };
  return (
    <>
      <input className={`${className ?? ""}${err ? " bad" : ""}`} value={v} placeholder={placeholder}
        title={err ?? undefined} onChange={(e) => setV(e.target.value)} onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} />
      {err && <span className="field-err">{err}</span>}
    </>
  );
}

/** A comma-separated list, committed on blur (so typing a comma does not jump). */
export function ListInput({ value, onCommit, placeholder, className }: {
  value: string[] | undefined; onCommit: (v: string[]) => void; placeholder?: string; className?: string;
}) {
  const shown = (value ?? []).join(", ");
  return (
    <input key={shown} className={className} defaultValue={shown} placeholder={placeholder}
      onBlur={(e) => {
        const next = e.target.value.split(",").map((x) => x.trim()).filter(Boolean);
        if (next.join(", ") !== shown) onCommit(next);
      }}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} />
  );
}

/** A number or empty (null). */
export function NumberInput({ value, onChange, min, max, step, placeholder }: {
  value: number | null | undefined; onChange: (v: number | null) => void; min?: number; max?: number;
  step?: number | "any"; placeholder?: string;
}) {
  return (
    <input type="number" value={value ?? ""} min={min} max={max} step={step} placeholder={placeholder ?? "—"}
      onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))} />
  );
}
