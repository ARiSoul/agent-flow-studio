// The run open in the Run panel: its state, its events (streamed from the server), its saved
// values and its files. One stream at a time: following another run stops the one before; a bad
// line is skipped; the outputs and files refresh at most every 400 ms while it runs.
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { RUN_ID, type RunInfo } from "./RunSetup";
import { parseLines, type Ev } from "./runSteps";
import type { RunIO } from "./types";

export function useRunStream(flow: string) {
  const [run, setRun] = useState<RunInfo | null>(null);
  const [events, setEvents] = useState<Ev[]>([]);
  const [saved, setSaved] = useState<RunIO | null>(null);
  const [arts, setArts] = useState<{ name: string; size: number }[]>([]);
  const evRef = useRef<Ev[]>([]);
  const stream = useRef<AbortController | null>(null);
  useEffect(() => () => stream.current?.abort(), []);

  const loadArts = useCallback((inp: string) => {
    fetch(`/api/artifacts/${encodeURIComponent(inp)}`).then((r) => (r.ok ? r.json() : [])).then(setArts).catch(() => setArts([]));
  }, []);
  const loadIO = useCallback((inp: string) => {
    if (!RUN_ID.test(inp)) { setSaved(null); return; }
    api.io(inp, flow).then(setSaved).catch(() => setSaved(null));
  }, [flow]);

  /** Forgets the open run's events (before following another run, or when it was deleted). */
  const clear = useCallback((keepRun = false) => {
    stream.current?.abort();
    evRef.current = []; setEvents([]); setSaved(null); setArts([]);
    if (!keepRun) setRun(null);
  }, []);

  /** Follows the events of a run from where we are. */
  const follow = useCallback(async (id: string, inp: string) => {
    stream.current?.abort();
    const ctl = new AbortController();
    stream.current = ctl;
    let pending: ReturnType<typeof setTimeout> | null = null;
    const refresh = () => {
      if (pending) return;
      pending = setTimeout(() => { pending = null; if (!ctl.signal.aborted) { loadArts(inp); loadIO(inp); } }, 400);
    };
    try {
      const r = await fetch(`/api/runs/${id}/events?after=${evRef.current.length}`, { signal: ctl.signal });
      if (!r.body) return;
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        const got = parseLines(buf + dec.decode(value, { stream: true }));
        buf = got.rest;
        // one update per chunk of the stream (a busy run sends many events at once)
        const add = got.events.filter((ev) => ev.type !== "state");
        const state = [...got.events].reverse().find((ev) => ev.type === "state");
        if (state) setRun(state as unknown as RunInfo);
        if (add.length) { evRef.current = evRef.current.concat(add); setEvents(evRef.current); }
        if (add.some((ev) => ["tool", "agent_done", "outputs", "decision", "end"].includes(ev.type))) refresh();
      }
    } catch { /* aborted (another run was opened) or the connection closed */ }
    if (ctl.signal.aborted) return;
    loadArts(inp); loadIO(inp);
  }, [loadArts, loadIO]);

  return { run, setRun, events, saved, arts, follow, clear };
}
