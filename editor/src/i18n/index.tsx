// Minimal i18n: strings per language, a React context and t()/tn() helpers.
// To add a language: create <lang>.ts with the same keys as en.ts and add it to LANGS.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { en, type MessageKey, type Messages } from "./en";
import { es } from "./es";
import { pluralKey } from "./plural";
import { pt } from "./pt";

export const LANGS = {
  en: { label: "English", html: "en", messages: en },
  pt: { label: "Português", html: "pt-PT", messages: pt },
  es: { label: "Español", html: "es", messages: es },
} as const satisfies Record<string, { label: string; html: string; messages: Messages }>;
export type Lang = keyof typeof LANGS;

const STORAGE_KEY = "afs.lang";
const isLang = (v: unknown): v is Lang => typeof v === "string" && v in LANGS;

/** Saved choice, else the browser language, else English. */
export function initialLang(): Lang {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (isLang(saved)) return saved;
  } catch { /* storage not available */ }
  for (const l of navigator.languages ?? [navigator.language]) {
    const base = l.toLowerCase().split("-")[0];
    if (isLang(base)) return base;
  }
  return "en";
}

type Vars = Record<string, string | number>;
const fill = (s: string, vars?: Vars) =>
  vars ? s.replace(/\{(\w+)\}/g, (m, k: string) => (k in vars ? String(vars[k]) : m)) : s;

export interface I18n {
  lang: Lang;
  setLang: (l: Lang) => void;
  /** Text for a key, with {placeholders} filled from vars. */
  t: (key: MessageKey, vars?: Vars) => string;
  /** Plural: `<key>.<category>` from the language's plural rules (`one`, `other`…; `other` when the
   * category has no text); {n} is filled. */
  tn: (key: string, n: number, vars?: Vars) => string;
  /** Dynamic key (e.g. a status from the server); returns fallback when the key is unknown. */
  tOr: (key: string, fallback: string, vars?: Vars) => string;
}

const Ctx = createContext<I18n | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(initialLang);
  useEffect(() => { document.documentElement.lang = LANGS[lang].html; }, [lang]);
  const setLang = useCallback((l: Lang) => {
    setLangState(l);
    try { localStorage.setItem(STORAGE_KEY, l); } catch { /* storage not available */ }
  }, []);
  const value = useMemo<I18n>(() => {
    const msgs: Record<string, string> = LANGS[lang].messages;
    const base: Record<string, string> = en;
    const get = (key: string) => msgs[key] ?? base[key];
    return {
      lang, setLang,
      t: (key, vars) => fill(get(key) ?? key, vars),
      tn: (key, n, vars) => fill(get(pluralKey(LANGS[lang].html, key, n, (k) => get(k) !== undefined)) ?? key, { n, ...vars }),
      tOr: (key, fallback, vars) => fill(get(key) ?? fallback, vars),
    };
  }, [lang, setLang]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useI18n(): I18n {
  const v = useContext(Ctx);
  if (!v) throw new Error("useI18n outside I18nProvider");
  return v;
}

export type T = I18n["t"];
