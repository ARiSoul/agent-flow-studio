// Server messages (validation, run form…) in the UI language. The server sends its English text
// plus a code and parameters (src/agent_flow_studio/messages.py); a parameter can be a nested
// message. English, or a code without a translation, shows the server text as it is.
import { serverEs } from "./server.es";
import { serverPt } from "./server.pt";

export type ServerParam = string | { code: string; params?: Record<string, ServerParam> };
export interface ServerMsg { code?: string; params?: Record<string, ServerParam> }

const TABLES: Record<string, Record<string, string>> = { pt: serverPt, es: serverEs };

/** A nested message in the UI language, or null when there is no translation (use the fallback). */
export function localizeParam(p: ServerParam | undefined, lang: string): string | null {
  if (p === undefined) return null;
  if (typeof p === "string") return p;
  const tpl = TABLES[lang]?.[p.code];
  if (!tpl) return null;
  let ok = true;
  const text = tpl.replace(/\{(\w+)\}/g, (all, k: string) => {
    const v = p.params?.[k];
    if (v === undefined) return all;
    const s = localizeParam(v, lang);
    if (s === null) ok = false;
    return s ?? "";
  });
  return ok ? text : null;
}

/** The message in the UI language; `fallback` (the server's English text) when it has no code or
 * no translation. */
export function localizeServer(m: ServerMsg | null | undefined, fallback: string, lang: string): string {
  if (!m?.code) return fallback;
  return localizeParam({ code: m.code, params: m.params }, lang) ?? fallback;
}
