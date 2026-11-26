// What waits for the person, counted ONE way everywhere (the Run tab badge, the page title, the
// notifications, the header): the open run (a write to approve, its gate, a pause) plus the other
// runs of the workspace that wait. A gate waiting for more than a day, or one the person dismissed,
// is listed apart and not counted, the open run's own gate included. Pure, so it is tested alone.

/** A run of this workspace that waits for the person (GET /api/runs-waiting). */
export type Waiting = { id: string | null; input: string; flow: string; title: string; kind: "gate" | "paused" | "approval"; label: string;
  since?: number | null; stale?: boolean; dismissed?: boolean };

export function attention(open: string | undefined, here: { pending: number; gate: boolean; paused: boolean }, waiting: Waiting[]) {
  const self = waiting.find((w) => w.input === open && w.kind === "gate");
  const quiet = here.gate && !!self && (!!self.stale || !!self.dismissed);  // its own gate is old or dismissed
  const mine = here.pending + (here.gate && !quiet ? 1 : 0) + (here.paused ? 1 : 0);
  const others = waiting.filter((w) => w.input !== open);
  const counted = others.filter((w) => !w.stale && !w.dismissed);
  const later = others.filter((w) => w.stale || w.dismissed);
  return { mine, quiet, counted, later, total: mine + counted.length };
}
