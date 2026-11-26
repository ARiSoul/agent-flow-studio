// Plural forms: the key of a count string for a language and a number ("rp.errors.one",
// "rp.errors.other"), from the language's own rules (Intl.PluralRules). Pure, so it is tested alone.
const rules = new Map<string, Intl.PluralRules>();

export function pluralKey(lang: string, key: string, n: number, has: (k: string) => boolean): string {
  let r = rules.get(lang);
  if (!r) { r = new Intl.PluralRules(lang); rules.set(lang, r); }
  const k = `${key}.${r.select(n)}`;
  return has(k) ? k : `${key}.other`;
}
