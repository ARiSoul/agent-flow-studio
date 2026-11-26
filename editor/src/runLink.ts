// The address of an open run in the page's URL: #flow=<flow>&run=<run folder>&step=<node>. A reload
// opens the run again; a copied link opens it (and the step) in another tab of the same studio.
export type RunLink = { flow?: string; run?: string; step?: string };

const NAME = /^[A-Za-z0-9_-]{1,64}$/;

export function parseRunLink(hash: string): RunLink {
  const p = new URLSearchParams(hash.replace(/^#/, ""));
  const out: RunLink = {};
  for (const k of ["flow", "run", "step"] as const) {
    const v = p.get(k);
    if (v && NAME.test(v)) out[k] = v;  // only names: a link never carries anything else
  }
  return out;
}

export function makeRunLink(l: RunLink): string {
  const p = new URLSearchParams();
  for (const k of ["flow", "run", "step"] as const) if (l[k]) p.set(k, l[k]!);
  const s = p.toString();
  return s ? `#${s}` : "";
}
