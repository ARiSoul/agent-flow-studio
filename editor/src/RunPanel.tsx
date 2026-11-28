// The Run panel: a fixed header and three views.
// - The header shows the open run (state, name and id, time, cost — "≈" while sessions still go —,
//   steps, why it failed) with the run-level action that matters now (Stop · Resume · Re-run; the ⋯
//   menu: Edit and run, Run from step, Compare, copy the id or a link, open the folder,
//   notifications) and "⏸ +n others": the other runs that wait for you and the queue (gates older
//   than a day and the ones you dismissed are listed apart and not counted). The run's name has
//   the first row; the actions and the others go below it. Scrolled, it folds to one line.
// - One count of what waits for you (attention.ts) for the Run tab badge, the page title and the
//   notifications; the Current tab only marks that the open run waits.
// - The open run (and step) is in the page's address (#flow=…&run=…&step=…): a reload opens it again.
// - "New run" (RunSetup), "Current" (RunAttention cards, then the steps or the log, then the
//   results) and "History" (RunHistory). One scroll area per view.
// The canvas follows the run (traceOf / edgeTraceOf) and a click on a node opens its step.
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { api, currentWorkspaceName } from "./api";
import { useDialog } from "./Dialog";
import { GrowingTextarea } from "./fields";
import { useI18n } from "./i18n";
import { Attention } from "./RunAttention";
import { Compare, HistoryView, type PastRun } from "./RunHistory";
import { LogView } from "./RunLog";
import { download, fullTime, MoreMenu, OutputViewer, pretty, StatusChip, useCopy, when } from "./RunParts";
import { Results, useRunFiles, type Shown } from "./RunResults";
import { attention as countNeeds, type Waiting } from "./attention";
import { changedCount, ChangesView, useChanges } from "./RunChanges";
import { PreviewView, usePreview } from "./RunPreview";
import { runApi } from "./runActions";
import { makeRunLink } from "./runLink";
import { defaultRunId, runBody, SetupView, usesExtensionTools, type Prefill, type Queued, type QueueLog, type RunInfo, type RunMode } from "./RunSetup";
import { costOf, count, duration, edgeTraceOf, liveCostOf, stepsOf, traceOf, usd, waitedOf, type EdgeRun, type NodeRun } from "./runSteps";
import { StepsList, type StepInput, type ToolCall } from "./RunStepsView";
import type { Doc } from "./types";
import { useRunStream } from "./useRunStream";

export { defaultRunId, usesExtensionTools, type RunMode } from "./RunSetup";
export { CONTEXT_NODE, edgeKey, edgeTraceOf, traceOf, type EdgeRun, type NodeRun } from "./runSteps";
export type Trace = { nodes: Record<string, NodeRun>; edges: Record<string, EdgeRun> };
/** From outside the panel: prefill New run (`mode`, `target`), or open a saved run (`open`, and a `step`). */
export type RunRequest = { mode: RunMode; target?: string; open?: string; flow?: string; step?: string; nonce: number };
/** What resuming a run that ended with an error would keep and repeat (GET /api/runs/<id>/resume-plan). */
type ResumePlan = { reuse: string[]; redo: string[]; changed: string[]; unreached: string[]; gates: string[] };
/** A run of this workspace that waits for the person (GET /api/runs-waiting). */
const ra = runApi();
type View = "setup" | "run" | "history";

// same test as AUTH_ERROR in editor/runner.py
const AUTH_ERROR = /failed to authenticate|oauth (session|token)|not logged in|please run \/login|invalid (x-)?api[ -]?key|authentication_error|could not be refreshed/i;

/** A value kept in this browser (localStorage). */
function useStored<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try { const raw = localStorage.getItem(key); return raw === null ? initial : (JSON.parse(raw) as T); } catch { return initial; }
  });
  const set = (x: T) => { setV(x); try { localStorage.setItem(key, JSON.stringify(x)); } catch { /* no storage */ } };
  return [v, set];
}

const typing = () => {
  const el = document.activeElement as HTMLElement | null;
  return !!el && (["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) || el.isContentEditable);
};

export function RunPanel({ flow, doc, dirty, mock, onTrace, request, focus, onFocusNode, onAttention, onOpenRun, onRetrySetting, onRunState }: {
  flow: string; doc: Doc; dirty: boolean; mock: boolean; onTrace: (t: Trace) => void;
  request?: RunRequest | null;
  /** The node selected on the canvas: its step opens in the Current view. */
  focus?: string | null;
  /** Select a node on the canvas (from a step). */
  onFocusNode?: (node: string) => void;
  /** Opens the flow's Properties at its Retry setting (the retry card's link). */
  onRetrySetting?: () => void;
  /** The run shown here: idle, running, or waiting for the person at a gate. */
  onRunState?: (s: "idle" | "running" | "gate") => void;
  /** How many things wait for the person in this workspace (gates, write approvals, paused runs). */
  onAttention?: (n: number) => void;
  /** Open a run of another flow (the editor switches to that flow first). */
  onOpenRun?: (flow: string, input: string) => void;
}) {
  const i18n = useI18n();
  const { t, lang } = i18n;
  const dialog = useDialog();
  const spec = doc.spec;
  const graph = spec.execution === "graph";
  const [view, setView] = useState<View>("setup");
  const [prefill, setPrefill] = useState<Prefill | null>(null);

  // ------------------------------------------------------------------ the open run
  const { run, setRun, events, saved, arts, follow, clear } = useRunStream(flow);
  const [runErr, setRunErr] = useState<string | null>(null);
  const viewIn = run?.input ?? "";
  const wsName = run?.workspace ?? currentWorkspaceName();
  const runDir = `runs/${wsName && wsName !== "studio" ? `@${wsName}/` : ""}${viewIn}`;
  const running = run?.status === "running";
  const [now, setNow] = useState(Date.now());
  const gateWaiting = run?.status === "gate";
  useEffect(() => { onRunState?.(running ? "running" : gateWaiting ? "gate" : "idle"); }, [running, gateWaiting]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!running && !gateWaiting && run?.status !== "decided") return;
    const h = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(h);
  }, [running, gateWaiting, run?.status]);
  const links = spec.edges ?? [];
  useEffect(() => { onTrace({ nodes: traceOf(events), edges: edgeTraceOf(events, links) }); },
    [events, onTrace]);  // eslint-disable-line react-hooks/exhaustive-deps -- the links of this run
  useEffect(() => () => onTrace({ nodes: {}, edges: {} }), [onTrace]);

  const body = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [unseen, setUnseen] = useState(0);
  const [compact, setCompact] = useState(false);
  const [atStep, setAtStep] = useState<string | null>(null);
  const clearNotice = useRef<() => void>(() => undefined);  // set once the queue notice exists  // a step to open once its run is shown
  const show = (r: RunInfo, fresh: boolean) => {
    if (fresh) {
      clear(true); setAtBottom(true); setUnseen(0); setCompact(false); clearNotice.current();
      requestAnimationFrame(() => body.current?.scrollTo({ top: 0 }));
    }
    setRun(r); setRunErr(null); setView("run");
    return follow(r.id, r.input);
  };
  const openPast = async (inp: string, step?: string) => {
    setAtStep(step ?? null);
    try { await show(await ra.open<RunInfo>(inp), true); }
    catch (e) { setRunErr(String((e as Error).message)); setView("run"); }
  };
  useEffect(() => {
    if (!request || (request.flow && request.flow !== flow)) return;
    if (request.open) { openPast(request.open, request.step); return; }
    setPrefill({ nonce: request.nonce, mode: request.mode, target: request.target }); setView("setup");
  }, [request]);  // eslint-disable-line react-hooks/exhaustive-deps -- a new request only

  // ------------------------------------------------------------------ saved runs (for the header and New run)
  const [past, setPast] = useState<PastRun[]>([]);
  const [histTotal, setHistTotal] = useState(0);
  const [histKey, setHistKey] = useState(0);
  const loadPast = useCallback(() => {
    fetch(`/api/flows/${encodeURIComponent(flow)}/runs?limit=200`).then((r) => (r.ok ? r.json() : { runs: [], total: 0 }))
      .then((d) => { setPast(d.runs ?? []); setHistTotal(d.total ?? 0); }).catch(() => setPast([]));
  }, [flow]);
  useEffect(() => { loadPast(); setHistKey((k) => k + 1); }, [loadPast, run?.status]);

  // the runs of this workspace that wait for you (any flow) and the queue, on a fixed timer
  const [waiting, setWaiting] = useState<Waiting[]>([]);
  const [queue, setQueue] = useState<{ queued: Queued[]; log: QueueLog[]; loaded?: boolean }>({ queued: [], log: [] });
  const [queueNotice, setQueueNotice] = useState<QueueLog | null>(null);
  clearNotice.current = () => setQueueNotice(null);
  const seenQueue = useRef<Set<string> | null>(null);  // the outcomes known when the panel opened are not news
  useEffect(() => {
    if (!queue.loaded) return;  // the first answer of the server: what is known already is not news
    if (seenQueue.current === null) { seenQueue.current = new Set(queue.log.map((x) => x.id)); return; }
    const fresh = queue.log.filter((x) => !seenQueue.current!.has(x.id));
    fresh.forEach((x) => seenQueue.current!.add(x.id));
    if (fresh.length) setQueueNotice(fresh[fresh.length - 1]);
  }, [queue.log]);
  useEffect(() => {
    if (!queueNotice) return;
    const h = setTimeout(() => setQueueNotice(null), 120_000);
    return () => clearTimeout(h);
  }, [queueNotice]);
  const loadWaiting = useCallback(() => {
    fetch("/api/runs-waiting").then((r) => (r.ok ? r.json() : { runs: [] })).then((d) => setWaiting(d.runs ?? [])).catch(() => undefined);
    fetch("/api/runs-queue").then((r) => (r.ok ? r.json() : { queued: [], log: [] }))
      .then((d) => setQueue({ queued: d.queued ?? [], log: (d.log ?? []).filter((x: QueueLog) => Date.now() / 1000 - x.ts < 600), loaded: true }))
      .catch(() => undefined);
  }, []);
  useEffect(() => { loadWaiting(); const h = setInterval(loadWaiting, 4000); return () => clearInterval(h); }, [loadWaiting]);
  useEffect(() => { loadWaiting(); }, [run?.status, loadWaiting]);

  // ------------------------------------------------------------------ acting on the open run
  const act = async (fn: () => Promise<RunInfo>) => {
    setRunErr(null);
    try { await show(await fn(), false); } catch (e) { setRunErr(String((e as Error).message)); }
  };
  const cont = (how: "step" | "full") => run && act(() => ra.cont<RunInfo>(run.id, how));
  const [gateNote, setGateNote] = useState("");
  const decide = async (decision: "approve" | "reject" | "answer") => {
    if (!run) return;
    if (decision === "reject" && !(await dialog.confirm({ title: t("rp.rejectConfirmTitle"), message: t("rp.rejectConfirm"),
      ok: t("rp.reject"), danger: true }))) return;
    await act(async () => {
      const r = await ra.decide<RunInfo>(run.id, decision, gateNote);
      setGateNote(""); setChanging(false);
      return r;
    });
  };
  const [stopping, setStopping] = useState(false);
  const [pausing, setPausing] = useState(false);
  const pauseRun = async (node?: string) => {
    if (!run) return;
    setPausing(true); setRunErr(null);
    try { await ra.pause(run.id, node); } catch (e) { setRunErr(String((e as Error).message)); }
    finally { setPausing(false); }
  };
  const stop = async () => {
    if (!run) return;
    setStopping(true); setRunErr(null);
    try {
      const r = await ra.stop<RunInfo>(run.id);
      if (r?.status) setRun((x) => (x ? { ...x, status: r.status } : x));
    } catch (e) { setRunErr(String((e as Error).message)); }
    finally { setStopping(false); }
  };

  // write approvals: one comment per card; a card hides while its answer is on the way
  const decided = new Set(events.filter((e) => e.type === "approval_result").map((e) => e.id as string));
  const [sent, setSent] = useState<Set<string>>(new Set());
  const [notes, setNotes] = useState<Record<string, string>>({});
  const pending = events.filter((e) => e.type === "approval" && !decided.has(e.id as string) && !sent.has(e.id as string));
  const answer = async (id: string, ok: boolean) => {
    if (!run || sent.has(id)) return;
    if (!ok && !(await dialog.confirm({ title: t("rp.rejectWriteTitle"), message: t("rp.rejectWrite"), ok: t("run.reject"), danger: true }))) return;
    setSent((s) => new Set(s).add(id));
    try {
      await ra.approveWrite(run.id, id, ok, notes[id] ?? "");
      setNotes(({ [id]: _, ...rest }) => rest);
    } catch (e) {
      setSent((s) => { const n = new Set(s); n.delete(id); return n; });  // the card comes back
      setRunErr(String((e as Error).message));
    }
  };

  // resume a run that ended with an error (or was stopped): keep the finished work, repeat the rest
  const [plan, setPlan] = useState<ResumePlan | null>(null);
  const [keepEdited, setKeepEdited] = useState(false);
  const [planOpen, setPlanOpen] = useState(false);
  const ended = !!run && (run.status === "error" || run.status === "stopped");
  const canResume = ended && run!.execution === "graph" && run!.mode !== "agent" && run!.mode !== "until";
  const noResume = ended && !canResume ? t(run!.execution !== "graph" ? "rp.noResumeOrch" : "rp.noResumeTest") : null;
  useEffect(() => {
    if (!canResume || !run) { setPlan(null); return; }
    let live = true;
    fetch(`/api/runs/${run.id}/resume-plan?reuse_changed=${keepEdited}`).then((r) => (r.ok ? r.json() : null))
      .then((p) => { if (live) setPlan(p); }).catch(() => { if (live) setPlan(null); });
    return () => { live = false; };
  }, [canResume, run?.id, run?.status, keepEdited]); // eslint-disable-line react-hooks/exhaustive-deps -- by run and state
  const resume = () => run && act(async () => {
    const r = await ra.resume<RunInfo>(run.id, keepEdited);
    setKeepEdited(false); setPlanOpen(false);
    return r;
  });

  /** "Edit and run": the form, filled with the mode, step and inputs of a saved run. */
  const editRun = async (inp: string, mode: RunMode = "full", target?: string) => {
    const io = inp === viewIn && saved ? saved : await api.io(inp, flow).catch(() => null);
    setPrefill({ nonce: Date.now(), mode: mode === "from" ? "full" : mode, target, inputs: io?.inputs ?? {} });
    setView("setup");
  };
  const hasFiles = (inputs?: Record<string, unknown>) => Object.entries(spec.inputs ?? {}).some(([n, x]) => x.type === "file" && inputs?.[n] !== undefined);
  /** Re-run: a new run with the same mode, step and inputs, started at once (a new id), after the
   * same checks as Run (unsaved changes, real data). A run with files or a one-step test opens the
   * form instead; a run that ran "from a step" runs again from the start. */
  const rerun = async (inp: string, mode = "full", target?: string | null) => {
    setRunErr(null);
    const io = inp === viewIn && saved ? saved : await api.io(inp, flow).catch(() => null);
    if (!io || hasFiles(io.inputs) || mode === "agent" || dirty) return editRun(inp, mode as RunMode, target ?? undefined);
    if (usesExtensionTools(spec) && !mock && !(await dialog.confirm({ title: t("run.realTitle"), message: t("run.confirmReal"), ok: t("run.realOk"), danger: true }))) return;
    try {
      const extra = mode === "step" || mode === "until" ? { mode, target: target ?? null } : {};
      const body = runBody(spec, flow, defaultRunId(flow), io.inputs, extra);
      if (running) { await ra.queue(body); loadWaiting(); setView("setup"); return; }  // one run at a time
      await show(await ra.start<RunInfo>(body), true);
    } catch (e) { setRunErr(String((e as Error).message)); setView("run"); }
  };
  const runFromStep = (inp: string, node = "") => { setPrefill({ nonce: Date.now(), mode: "from", target: node, base: inp }); setView("setup"); };

  // after a run (or at a gate): who can be asked for more (the agents that ran, the last first)
  const [askable, setAskable] = useState<string[]>([]);
  const [askTo, setAskTo] = useState<string>("");
  const [askText, setAskText] = useState("");
  const [askOpen, setAskOpen] = useState(false);
  const askRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!run || run.status === "running") { setAskable([]); return; }
    fetch(`/api/runs/${run.id}/askable`).then((r) => (r.ok ? r.json() : { agents: [] }))
      .then((d: { agents: string[] }) => { setAskable(d.agents); setAskTo((x) => (d.agents.includes(x) ? x : d.agents[0] ?? "")); })
      .catch(() => setAskable([]));
  }, [run?.id, run?.status]);  // eslint-disable-line react-hooks/exhaustive-deps
  const ask = () => run && askText.trim() && act(async () => {
    const r = await ra.ask<RunInfo>(run.id, askText, askTo);
    setAskText(""); setAskOpen(false);
    return r;
  });
  const askStep = (node: string) => { setAskTo(node); setAskOpen(true); requestAnimationFrame(() => askRef.current?.scrollIntoView({ block: "nearest" })); };

  // the end of the run: a workspace flow may suggest updating the workspace context
  const lastEnd = useMemo(() => [...events].reverse().find((e) => e.type === "end"), [events]);
  const updateContext = () => run && act(async () => {
    const r = await api.updateContext("update", null, run.input);
    clear(true);
    return r as unknown as RunInfo;
  });

  // ------------------------------------------------------------------ what the open run shows
  const steps = useMemo(() => stepsOf(spec, events), [spec, events]);
  const startEv = events.find((e) => e.type === "start");
  const t0 = startEv?.ts;
  const t1 = running ? now / 1000 : (lastEnd?.ts ?? events[events.length - 1]?.ts ?? t0);
  const cost = events.length ? costOf(events) : run?.cost ?? 0;  // this attempt: its session results
  const est = running ? liveCostOf(steps) : 0;  // the sessions still going: an estimate
  // decided at its gate while another run went: it goes on next
  const turn = useMemo(() => {
    if (run?.status !== "decided") return null;
    const tn = [...events].reverse().find((e) => e.type === "turn");
    const d = [...events].reverse().find((e) => e.type === "decision");
    return { after: String(tn?.after ?? ""), decision: i18n.tOr(`run.decision.${String(d?.decision ?? "")}`, String(d?.decision ?? "")),
      comment: String(d?.comment ?? ""), key: String(d?.decision ?? "") };
  }, [run?.status, events, i18n]);
  const waited = useMemo(() => waitedOf(events, gateWaiting ? now / 1000 : undefined), [events, gateWaiting, now]);
  const pastRun = past.find((p) => p.input === viewIn);
  const allCost = pastRun && pastRun.cost > cost + 0.0005 ? pastRun.cost : null;  // earlier attempts in the folder
  const failedStep = run?.status === "error" ? [...steps].reverse().find((s) => s.state === "failed") : undefined;
  const failMsg = run?.status === "error" ? String(failedStep?.error || lastEnd?.message || "").replace(/^agent [^:]+: /, "") : "";
  const [changing, setChanging] = useState(false);  // decided: the person changes the decision
  useEffect(() => {
    if (run?.status === "decided" || !changing) return;
    setChanging(false);  // it went on while the card was open (the hold ended): say so
    setRunErr(t("rp.holdEnded"));
  }, [run?.status]);  // eslint-disable-line react-hooks/exhaustive-deps -- on a change of state
  const gate = (run?.status === "gate" || (run?.status === "decided" && changing)) && run.gate?.kind === "human" ? run.gate : null;
  const paused = run?.status === "paused";
  const authErr = events.find((e) => e.hint === "auth"
    || (["end", "agent_done", "result", "text"].includes(e.type) && AUTH_ERROR.test(String(e.message ?? e.text ?? ""))));
  const ctxSuggest = !running && !!run && lastEnd?.context === "suggest";
  const retrying = running ? steps.filter((s) => s.state === "retrying" && s.retry) : [];
  // changing a recorded decision is the person's own act: it does not count as waiting for them
  const need = countNeeds(viewIn || undefined, { pending: pending.length, gate: !!gate && run?.status === "gate", paused }, waiting);
  const { counted, later } = need;
  const needHere = need.mine;
  const needAll = need.total;
  const stopMessage = run?.status === "done" && run.gate?.message ? run.gate : null;
  const doneCount = steps.filter((s) => s.state === "done" || s.state === "skipped").length;
  const liveOut = steps.reduce((k, s) => k + (s.live?.out ?? 0), 0);  // tokens of the sessions still going
  const lastOut = useMemo(() => [...events].reverse().find((e) => e.type === "outputs" && !e.sub)?.agent as string | undefined, [events]);
  const outputs = useMemo(() => Object.entries(saved?.outputs ?? {}).filter(([, o]) => Object.keys(o).length)
    .sort(([a], [b]) => (a === lastOut ? -1 : b === lastOut ? 1 : 0)), [saved, lastOut]);
  const typeOf = (agent: string, n: string) => spec.agents[agent]?.outputs?.[n]?.type ?? "text";
  const usedBy = (agent: string, n: string) => Object.entries(spec.agents).flatMap(([a, x]) =>
    Object.entries(x.inputs ?? {}).filter(([, i]) => i.from === `${agent}.${n}`).map(([k]) => `${a}.${k}`));
  const inputsOf = (node: string): StepInput[] => Object.entries(spec.agents[node]?.inputs ?? {}).map(([name, i]) => {
    const from = i.from ?? undefined;
    let value: unknown = saved?.given?.[node]?.[name];
    if (value === undefined && from && saved) {
      const [root, key] = [from.slice(0, from.indexOf(".")), from.slice(from.indexOf(".") + 1)];
      value = root === "input" ? saved.inputs[key] : saved.outputs[root]?.[key];
    }
    return { name, from, value };
  });
  const promptOf = (node: string) => {
    const a = spec.agents[node];
    return a?.prompt || (a?.prompt_file ? doc.prompts[a.prompt_file] ?? doc.prompts[a.prompt_file.replace(/^prompts\//, "")] ?? "" : "");
  };
  const toolCall = async (id: string): Promise<ToolCall> => {
    const r = await fetch(`/api/runs/${run!.id}/tools/${encodeURIComponent(id)}`);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : r.statusText);
    return d as ToolCall;
  };
  const modelOf = (node: string) => {
    const m = spec.agents[node]?.model;
    return m && m !== "inherit" ? m : spec.model || (startEv?.model as string | undefined) || null;
  };
  const title = pastRun?.title || viewIn;
  const gateOutputs = gate ? links.filter((l) => l.to === gate.gate).map((l) => l.from).filter((n) => saved?.outputs?.[n])
    .flatMap((n) => Object.entries(saved!.outputs[n]).map(([name, value]) => ({ node: n, name, value }))) : [];
  const gateStep = gate ? steps.find((s) => s.node === gate.gate) : undefined;
  // what Re-run will do, said on the button
  const fromStep = events.some((e) => e.type === "resume" && e.from);
  const rerunHint = !run ? "" : hasFiles(saved?.inputs) || run.mode === "agent" ? t("rp.rerunForm")
    : fromStep ? t("rp.rerunFromStart") : t("rp.rerunTitle");

  // the badge on the outer Run tab, the page title and (if asked for) a notification
  const [notify, setNotify] = useStored("afs.run.notify", false);
  const seen = useRef(0);
  useEffect(() => {
    onAttention?.(needAll);
    const base = document.title.replace(/^\(\d+\) ⏸ /, "");
    document.title = needAll ? `(${needAll}) ⏸ ${base}` : base;
    if (needAll > seen.current && notify && document.hidden && "Notification" in window && Notification.permission === "granted") {
      new Notification(t("rp.notifyTitle"), { body: gate ? t("rp.waitsFor", { label: gate.label }) : t("rp.waitingN", { n: String(needAll) }) });
    }
    seen.current = needAll;
  }, [needAll]);  // eslint-disable-line react-hooks/exhaustive-deps -- when the count changes
  useEffect(() => () => { onRunState?.("idle"); onAttention?.(0); document.title = document.title.replace(/^\(\d+\) ⏸ /, ""); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  const toggleNotify = async () => {
    if (!notify && "Notification" in window && Notification.permission === "default") await Notification.requestPermission();
    setNotify(!notify);
  };

  // a card that needs you: announced, and focused unless you are typing somewhere
  const attention = useRef<HTMLElement>(null);
  const [announce, setAnnounce] = useState("");
  const needKey = `${gate?.gate ?? ""}|${pending.map((p) => p.id).join(",")}|${paused}`;
  useEffect(() => {
    if (!needHere) return;
    setAnnounce(gate ? t("rp.waitsFor", { label: gate.label }) : t("rp.waitingN", { n: String(needHere) }));
    if (view === "run" && !typing()) attention.current?.focus({ preventScroll: false });
  }, [needKey]);  // eslint-disable-line react-hooks/exhaustive-deps -- when a new one appears
  const review = () => { setView("run"); requestAnimationFrame(() => { body.current?.scrollTo({ top: 0, behavior: "smooth" }); attention.current?.focus(); }); };

  // steps or log; a node chosen on the canvas opens its step
  const [runView, setRunView] = useStored<"steps" | "log" | "changes" | "preview">("afs.run.view", "steps");
  const preview = usePreview();
  // what the run changed in the workspace's git roots: again when a step ends or the state changes
  const [chKey, setChKey] = useState(0);
  const stepsEnded = events.filter((e) => e.type === "agent_done" || e.type === "approval_result").length;
  const changes = useChanges(viewIn || undefined, `${run?.status}|${stepsEnded}|${chKey}`);
  const nChanged = changedCount(changes);
  const runFiles = useRunFiles(viewIn || undefined, `${run?.status}|${stepsEnded}`);
  const showChanges = () => { setView("run"); setRunView("changes"); requestAnimationFrame(() => body.current?.querySelector(".rp-switch")?.scrollIntoView({ block: "start", behavior: "smooth" })); };
  const [reveal, setReveal] = useState<{ node: string; nonce: number } | null>(null);
  const showStep = (node: string) => { setView("run"); setRunView("steps"); setReveal({ node, nonce: Date.now() }); };
  useEffect(() => { if (focus && run && steps.some((s) => s.node === focus)) setReveal({ node: focus, nonce: Date.now() }); },
    [focus]);  // eslint-disable-line react-hooks/exhaustive-deps -- only when the selection changes
  const [highlight, setHighlight] = useState<{ agent: string; name: string; nonce: number } | null>(null);

  // follow the run in the log (only while you are at the bottom); a scrolled view folds the header
  const lastCount = useRef(0);
  const onScroll = () => {
    const el = body.current;
    if (!el) return;
    const bottom = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
    setAtBottom(bottom);
    if (bottom) setUnseen(0);
    setCompact(el.scrollTop > 80);
  };
  useEffect(() => {
    const added = events.length - lastCount.current;
    lastCount.current = events.length;
    if (view !== "run" || runView !== "log" || !running || added <= 0) return;
    if (atBottom) requestAnimationFrame(() => { const el = body.current; if (el) el.scrollTop = el.scrollHeight; });
    else setUnseen((n) => n + added);
  }, [events.length]);  // eslint-disable-line react-hooks/exhaustive-deps -- on new events only
  const toBottom = () => { body.current?.scrollTo({ top: body.current.scrollHeight, behavior: "smooth" }); setUnseen(0); };

  const [shown, setShown] = useState<Shown | null>(null);
  const [copied, copy] = useCopy();
  const [compare, setCompare] = useState<PastRun | null>(null);
  const openStep = (inp: string, node: string) => (inp === viewIn ? showStep(node) : openPast(inp, node));
  // the open run (and the step chosen) in the page's address; a copied link opens them
  const linkTo = (node?: string) => `${location.origin}${location.pathname}${makeRunLink({ flow, run: viewIn || undefined, step: node })}`;
  useEffect(() => {
    if (!viewIn) return;
    const want = makeRunLink({ flow, run: viewIn, step: reveal?.node });
    if (location.hash !== want) history.replaceState(null, "", want);
  }, [viewIn, reveal, flow]);  // eslint-disable-line react-hooks/exhaustive-deps -- reveal is declared below
  useEffect(() => {  // a run opened with a step: open the step once its run is there
    if (atStep && run && steps.some((s) => s.node === atStep) && events.length) { showStep(atStep); setAtStep(null); }
  }, [atStep, run, steps, events.length]);  // eslint-disable-line react-hooks/exhaustive-deps
  const retry = async (node: string, action: "now" | "cancel") => {
    if (!run) return;
    try { await ra.retry(run.id, node, action); } catch (e) { setRunErr(String((e as Error).message)); }
  };
  const dismiss = async (w: Waiting, on: boolean) => {
    try { await ra.dismiss(w.input, on); loadWaiting(); } catch (e) { setRunErr(String((e as Error).message)); }
  };
  const [waitOpen, setWaitOpen] = useState(false);
  // go on with a step's session in Claude Code (its own tools: images, browser, files)
  const claudeCode = async (node: string, how: "terminal" | "copy") => {
    setRunErr(null);
    try {
      const path = `/api/runs/${encodeURIComponent(viewIn)}/steps/${encodeURIComponent(node)}`;
      if (how === "terminal") { await ra.call(`${path}/terminal`); return; }
      const r = await fetch(`${path}/session`);
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : r.statusText);
      copy(String(d.command), `cc-${node}`);
    } catch (e) { setRunErr(String((e as Error).message)); }
  };
  // change a recorded decision: the server holds the run (it does not go on with the old one)
  const startChange = async () => {
    if (!run) return;
    try { setRun(await ra.holdDecision<RunInfo>(run.id, true)); setGateNote(turn?.comment ?? ""); setChanging(true); }
    catch (e) { setRunErr(String((e as Error).message)); }
  };
  const keepDecision = async () => {
    if (!run) return;
    setChanging(false); setGateNote("");
    try { await ra.holdDecision(run.id, false); } catch (e) { setRunErr(String((e as Error).message)); }
  };
  const startQueued = async (q: Queued) => {
    if (!(await dialog.confirm({ title: t("rp.startAnywayTitle", { name: q.name || q.flow }), message: t("rp.startAnywayMessage"), ok: t("rp.startAnyway") }))) return;
    try { setWaitOpen(false); await show(await ra.startQueued<RunInfo>(q.id), true); loadWaiting(); }
    catch (e) { setRunErr(String((e as Error).message)); }
  };
  const waitBox = useRef<HTMLSpanElement>(null);
  useEffect(() => {  // the list closes with Escape or a click outside it
    if (!waitOpen) return;
    const key = (e: globalThis.KeyboardEvent) => { if (e.key === "Escape") setWaitOpen(false); };
    const click = (e: MouseEvent) => { if (!waitBox.current?.contains(e.target as Node)) setWaitOpen(false); };
    window.addEventListener("keydown", key);
    window.addEventListener("mousedown", click);
    return () => { window.removeEventListener("keydown", key); window.removeEventListener("mousedown", click); };
  }, [waitOpen]);
  const openFile = async (name: string) => {
    const r = await fetch(`/api/artifacts/${encodeURIComponent(viewIn)}/${encodeURIComponent(name)}`);
    if (!r.ok) return;
    const f = await r.json() as { name: string; text: string };
    let value: unknown = f.text;
    if (name.endsWith(".json")) try { value = JSON.parse(f.text); } catch { /* as it is */ }
    setShown({ title: name, value, md: name.endsWith(".md") });
  };
  const openFolder = () => { if (viewIn) ra.call(`/api/runs/${encodeURIComponent(viewIn)}/reveal`).catch((e) => setRunErr(String(e))); };
  const openWaiting = (w: Waiting) => { setWaitOpen(false); if (w.flow === flow) openPast(w.input); else onOpenRun?.(w.flow, w.input); };

  // ================================================================== header
  const primary = !run || run.status === "decided" ? null : running ? (<>
    {graph && <button disabled={pausing} onClick={() => pauseRun()} title={t("rp.pauseTitle")}>⏸ {t("rp.pause")}</button>}
    <button className="danger" disabled={stopping} onClick={stop}>{stopping ? t("rp.stopping") : t("run.stop")}</button></>
  ) : gate || paused ? (
    view !== "run" || compact ? <button className="primary" onClick={review}>⏸ {t("rp.review")}</button> : null
  ) : canResume && plan && plan.redo.length > 0 ? (
    <button className="primary" onClick={resume} title={t("rp.resumeTitle", { run: run.input })}>{t("run.resume")}</button>
  ) : (
    <button className="btn-run" onClick={() => rerun(run.input, run.mode, run.target)} title={rerunHint}>▶ {t("rp.rerun")}</button>
  );
  const secondary = !run || running || gate || paused || run.status === "decided" ? null : (<>
    {canResume && plan && plan.redo.length > 0 && <>
      <button className="small rp-plan-btn" aria-expanded={planOpen} aria-controls="rp-plan" onClick={() => setPlanOpen(!planOpen)} title={t("rp.planTitle")}>
        <span aria-hidden="true">{planOpen ? "▾" : "▸"}</span> {t("rp.planBtn", { keep: String(plan.reuse.length), redo: String(plan.redo.length) })}</button>
      <button className="small btn-run" onClick={() => rerun(run.input, run.mode, run.target)} title={rerunHint}>▶ {t("rp.rerun")}</button>
    </>}
  </>);
  const runMenu = run && (
    <MoreMenu label={t("rp.runMenu")} items={[
      { label: t("rp.editRun"), title: t("rp.editRunTitle"), disabled: running, onSelect: () => editRun(run.input, (run.mode ?? "full") as RunMode, run.target ?? undefined) },
      ...(graph ? [{ label: t("rp.runFromStepDots"), title: t("rp.fromStepTitle"), disabled: running, onSelect: () => runFromStep(run.input) }] : []),
      ...(pastRun ? [{ label: t("rp.compareDots"), onSelect: () => setCompare(pastRun) }] : []),
      { kind: "sep" as const },
      { label: t(copied === "id" ? "run.outCopied" : "rp.copyId"), onSelect: () => copy(run.input, "id") },
      { label: t(copied === "link" ? "run.outCopied" : "rp.copyRunLink"), onSelect: () => copy(linkTo(), "link") },
      { label: t("run.openFolder"), onSelect: openFolder },
      { label: t(notify ? "rp.notifyOff" : "rp.notifyOn"), onSelect: toggleNotify },
    ]} />);
  const waitItem = (w: Waiting) => (
    <li key={`${w.input}-${w.kind}`} className={w.stale || w.dismissed ? "muted" : undefined}>
      <button className="link rp-wait-open" onClick={() => openWaiting(w)} title={w.flow !== flow ? t("rp.otherFlow", { flow: w.flow }) : t("rp.review")}>
        <b>{w.title || w.input}</b>{w.label ? ` — ${w.label}` : ""}{w.flow !== flow ? ` (${w.flow})` : ""}</button>
      <span className="small muted">{w.since ? when(w.since, lang) : ""}{w.stale ? ` · ${t("rp.staleTag")}` : ""}</span>
      {w.kind === "gate" && (w.dismissed || !w.stale) && <button className="link small" title={t(w.dismissed ? "rp.undismissTitle" : "rp.dismissTitle")} onClick={() => dismiss(w, !w.dismissed)}>
        {t(w.dismissed ? "rp.undismiss" : "rp.dismiss")}</button>}
    </li>);
  const queued = queue.queued;
  const othersLabel = counted.length === 0 ? i18n.tn("rp.notCounted", later.length)
    : run ? i18n.tn("rp.othersWait", counted.length) : i18n.tn("rp.runsWait", counted.length);
  const waitMenu = (counted.length + later.length + queued.length > 0) && (
    <span className="rp-wait-wrap" ref={waitBox}>
      <button className={`small rp-wait-btn${counted.length ? "" : " quiet"}`} aria-expanded={waitOpen} aria-haspopup="dialog"
        title={t("rp.othersTitle")} onClick={() => setWaitOpen(!waitOpen)}>
        ⏸ {othersLabel}{queued.length > 0 ? ` · ${i18n.tn("rp.queuedN", queued.length)}` : ""}</button>
      {waitOpen && (
        <div className="rp-wait-pop" role="dialog" aria-label={othersLabel}>
          <h4 className="rp-wait-h">{t("rp.secWaiting", { n: String(counted.length) })}</h4>
          {counted.length > 0 ? <ul>{counted.map(waitItem)}</ul> : <p className="small muted">{t("rp.noneWait")}</p>}
          {later.length > 0 && <><h4 className="rp-wait-h rp-wait-sep">{t("rp.waitOlder", { n: String(later.length) })}</h4>
            <ul>{later.map(waitItem)}</ul></>}
          {queued.length > 0 && <><h4 className="rp-wait-h rp-wait-sep">{t("rp.queuedHere", { n: String(queued.length) })}</h4>
            <ul>{queued.map((q) => (
              <li key={q.id}><span className="grow">{q.place}. <b>{q.name || q.flow}</b>{q.flow !== flow ? ` (${q.flow})` : ""}
                <span className="small muted"> · {t("rp.queuedAt", { t: when(q.queued, lang) })}</span></span>
                {q.held_by.length > 0 && <span className="small muted rp-held">{t("rp.heldBy")} {q.held_by.map((h, n) => (
                  <span key={`${h.input}-${h.kind}`}>{n > 0 && ", "}
                    <button className="link small" onClick={() => openWaiting({ ...h, id: null, title: h.title ?? "", label: h.label ?? "" } as Waiting)}>{h.title || h.input}</button>
                    {" "}({t(h.kind === "running" ? "rp.holdRunning" : h.kind === "gate" ? "rp.holdGate" : h.kind === "paused" ? "rp.holdPaused" : "rp.holdWrite")}{h.since ? `, ${when(h.since, lang)}` : ""})</span>))}</span>}
                <span className="row">
                  {q.held_by.length > 0 && q.held_by[0].kind !== "running" && <button className="small warn" onClick={() => startQueued(q)}>{t("rp.startAnyway")}</button>}
                  <button className="small" onClick={() => ra.unqueue(q.id).then(loadWaiting).catch((e) => setRunErr(String((e as Error).message)))}>
                    {t("rp.unqueue")}</button>
                  <span className="muted small" title={t("rp.queueMemory")} aria-label={t("rp.queueMemory")}>ⓘ</span></span></li>))}</ul></>}
          <div className="row"><span className="grow" /><button className="small" onClick={() => setWaitOpen(false)}>{t("conn.close")}</button></div>
        </div>)}
    </span>);
  // a run that left the queue (started, or could not start): a strip in the header until closed
  const notice = queueNotice && (
    <div className={`rp-notice small${queueNotice.error ? " bad" : ""}`} role="status">
      <span className="grow">{queueNotice.error ? t("rp.queueFailedNamed", { name: queueNotice.name || queueNotice.flow, error: queueNotice.error })
        : t("rp.queueStartedNamed", { name: queueNotice.name || queueNotice.flow })}</span>
      {queueNotice.input && !queueNotice.error && (
        <button className="link small" onClick={() => { setQueueNotice(null); if (queueNotice.flow === flow) openPast(queueNotice.input!); else onOpenRun?.(queueNotice.flow, queueNotice.input!); }}>
          {t("rp.open")}</button>)}
      <button className="icon small" aria-label={t("conn.close")} onClick={() => setQueueNotice(null)}>✕</button>
    </div>);
  const header = (run || counted.length + later.length + queued.length > 0 || queueNotice) && (
    <div className={`rp-head${compact ? " compact" : ""}`}>
      {running && <div className="kit-bar global" aria-hidden="true" />}
      <div className="rp-head-row">
        {run ? <><StatusChip status={run.status} big />
          <span className="rp-title" title={title !== viewIn ? `${title} (${viewIn})` : viewIn}>{title}</span></>
          : <><span className="grow" />{waitMenu}</>}
        {compact && primary}
        {runMenu}
      </div>
      {notice}
      {run && !compact && <>
        <div className="rp-meta">
          {title !== viewIn && <span className="mono">{viewIn}</span>}
          <span>{i18n.tOr(`run.mode.${run.mode ?? "full"}`, run.mode ?? "full")}{run.target ? ` · ${run.target}` : ""}</span>
          {t0 && <span title={fullTime(t0, lang)}>{when(t0, lang)}</span>}
          {t0 && t1 && <span title={t("rp.elapsedTitle")}>⏱ {duration(t1 - t0)}{waited >= 1 ? <span className="muted"> ({t("rp.waitedYou", { t: duration(waited) })})</span> : null}</span>}
          <span title={est > 0 ? t("rp.estTitle") : t("rp.costTitle")}>{est > 0 ? `≈ ${usd(cost + est)}` : usd(cost)}
            {allCost !== null && <span className="muted"> {t("rp.allAttempts", { cost: usd(allCost) })}</span>}</span>
          {running && liveOut > 0 && <span title={t("rp.liveTitle")}>{t("rp.liveOut", { n: count(liveOut) })}</span>}
          {steps.length > 0 && <span title={t("rp.stepsTitle")}>{t("rp.stepsDone", { n: String(doneCount), of: String(steps.length) })}</span>}
          {waitMenu && <span className="rp-meta-end">{waitMenu}</span>}
        </div>
        {failMsg && (
          <button className="rp-fail" onClick={() => failedStep && showStep(failedStep.node)} title={`${failMsg}\n${t("rp.showStep")}`}>
            <b>✗ {failedStep ? failedStep.node : t("run.status.error")}</b>{failedStep?.transient ? ` · ${t("rp.transientShort")}` : ""} — {failMsg}</button>)}
        {(gate || paused) && view !== "run" && <div className={`rp-wait${need.quiet ? " quiet" : ""}`}>⏸ {gate
          ? t(need.quiet ? "rp.waitsQuiet" : "rp.waitsFor", { label: gate.label }) : t("rp.pausedShort")}</div>}
        {runErr && <p className="field-err small" role="alert">{runErr}</p>}
        {(primary || secondary) && <div className="rp-actions">{primary}{secondary}</div>}
        {planOpen && plan && (
          <div className="rp-plan small" id="rp-plan">
            <div><span className="rp-ok">✓ {t("run.resumeKeep")}</span> {plan.reuse.join(", ") || "—"}</div>
            <div><span className="rp-redo">↻ {t("run.resumeRedo")}</span> {plan.redo.join(", ")}</div>
            {plan.changed.length > 0 && (
              <label className="row small warn-text">
                <input type="checkbox" checked={keepEdited} onChange={(e) => setKeepEdited(e.target.checked)} />
                {t("run.resumeKeepEdited", { names: plan.changed.join(", ") })}
              </label>)}
            {plan.gates.length > 0 && <div className="muted">{t("run.resumeGates", { names: plan.gates.join(", ") })}</div>}
            <div className="muted">{t("rp.sameFolder", { run: run.input })}</div>
          </div>)}
        {noResume && <p className="muted small">{noResume}</p>}
      </>}
    </div>);

  // ================================================================== Current
  const current = !run ? (
    <div className="rp-body rp-empty">
      {runErr && <p className="field-err" role="alert">{runErr}</p>}
      <p className="muted">{t("rp.noRun")}</p>
      <div className="row"><button className="primary" onClick={() => setView("setup")}>{t("rp.tab.setup")}</button>
        {histTotal > 0 && <button onClick={() => setView("history")}>{t("rp.tab.history")}</button>}</div>
    </div>
  ) : (
    <div className="rp-body" ref={body} onScroll={onScroll}>
      <Attention refEl={attention} authErr={authErr} pending={pending} notes={notes} setNotes={setNotes} onAnswer={answer}
        retrying={retrying} onRetry={retry} onRetrySetting={onRetrySetting} turn={changing ? null : turn} onChange={startChange}
        changingFrom={changing && turn ? { ...turn, until: run.held_until } : null} onKeep={keepDecision} retryPolicy={{ transient: spec.retry?.transient ?? 2, wait: spec.retry?.wait ?? 15 }}
        gate={gate} waitedSince={gateStep?.start} now={now} gateOutputs={gateOutputs} typeOf={typeOf}
        note={gateNote} setNote={setGateNote} onDecide={decide} onStop={stop}
        paused={paused} pausedUntil={run.mode === "until" ? run.target : null} onContinue={cont}
        pausedAgents={steps.filter((s) => s.state === "paused").map((s) => s.node)}
        pausedAfter={[...steps].filter((s) => s.state === "done" && s.end).sort((a, b) => (b.end ?? 0) - (a.end ?? 0))[0]?.node ?? null}
        ctxSuggest={ctxSuggest} onContext={updateContext} onShow={setShown}
        changed={changes?.available ? nChanged : null} onChanges={showChanges} runInput={viewIn} />
      {stopMessage && <div className="ev result-msg">■ {stopMessage.label}: {stopMessage.message}</div>}
      <div className="rp-switch" role="radiogroup" aria-label={t("rp.showAs")}>
        <button role="radio" aria-checked={runView === "steps"} className={runView === "steps" ? "on" : ""} onClick={() => setRunView("steps")}>
          {t("rp.steps")} <span className="muted">{doneCount}/{steps.length}</span></button>
        <button role="radio" aria-checked={runView === "log"} className={runView === "log" ? "on" : ""} onClick={() => setRunView("log")}>
          {t("rp.log")}</button>
        {changes?.available && <button role="radio" aria-checked={runView === "changes"} className={runView === "changes" ? "on" : ""} onClick={() => setRunView("changes")}
          title={t("rp.chTitle")}>{t("rp.changes")} <span className="muted">{nChanged}</span></button>}
        {preview && <button role="radio" aria-checked={runView === "preview"} className={runView === "preview" ? "on" : ""}
          onClick={() => setRunView("preview")} title={preview.url}>{t("rp.preview")}</button>}
      </div>
      {runView === "preview" && preview ? <PreviewView preview={preview} />
        : runView === "changes" && changes?.available ? <ChangesView data={changes} onRefresh={() => setChKey((k) => k + 1)} />
        : runView !== "log" ? (
        <StepsList spec={spec} steps={steps} events={events} span={t0 && t1 ? [t0, t1] : null} now={now} running={running}
          reveal={reveal} focus={focus} graph={graph} askable={askable} model={modelOf} inputsOf={inputsOf} promptOf={promptOf}
          onToolCall={toolCall} onCanvas={onFocusNode} onRunFrom={running ? undefined : (n) => runFromStep(run.input, n)}
          onAsk={running ? undefined : askStep} onLink={linkTo} onClaude={claudeCode}
          onPause={running && graph ? (n) => pauseRun(n) : undefined}
          onOutput={(agent, name) => setHighlight({ agent, name, nonce: Date.now() })} />
      ) : (
        <LogView events={events} t0={t0} steps={steps.map((s) => s.node)} fileName={viewIn} onStep={showStep} />)}
      <Results run={viewIn} files={runFiles} outputs={outputs} lastOut={lastOut} done={run.status === "done"} typeOf={typeOf} usedBy={usedBy} arts={arts} runDir={runDir}
        highlight={highlight} onShow={setShown} onFile={openFile} onFolder={openFolder} onStep={showStep} />
      {!running && askable.length > 0 && (
        <section className="rp-ask" ref={askRef}>
          {!askOpen ? <button className="small" onClick={() => setAskOpen(true)}>{t("rp.askOpen")}</button> : (
            <div className="gate-card ask-card">
              <b>{t("run.askTitle")}</b>
              <p className="small muted">{t("run.askHelp")}</p>
              {(askable.length > 1 || askable[0] !== "") && (
                <label className="row small">
                  {t("run.askTo")}
                  <select value={askTo} onChange={(e) => setAskTo(e.target.value)}>
                    {askable.map((a) => <option key={a} value={a}>{a || t("run.askFlow")}</option>)}
                  </select>
                </label>)}
              <GrowingTextarea minRows={2} placeholder={t("run.askPlaceholder")} value={askText} aria-label={t("run.askPlaceholder")}
                onChange={(e) => setAskText(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); ask(); } }} />
              <div className="row">
                <button className="primary small" disabled={!askText.trim()} onClick={ask} title="Ctrl+Enter">{t("run.askSend")}</button>
                <button className="small" onClick={() => setAskOpen(false)}>{t("dialog.cancel")}</button>
              </div>
            </div>)}
        </section>)}
      {running && runView === "log" && unseen > 0 && !atBottom && <button className="rp-jump" onClick={toBottom}>↓ {i18n.tn("rp.newEvents", unseen)}</button>}
    </div>);

  const tab = (v: View, text: ReactNode) => (
    <button role="tab" id={`rp-tab-${v}`} aria-selected={view === v} aria-controls={`rp-view-${v}`} tabIndex={view === v ? 0 : -1}
      className={view === v ? "on" : ""} onClick={() => setView(v)}>{text}</button>);
  const onTabsKey = (e: KeyboardEvent) => {
    const order: View[] = ["setup", "run", "history"];
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const n = order[(order.indexOf(view) + (e.key === "ArrowRight" ? 1 : order.length - 1)) % order.length];
    setView(n);
    requestAnimationFrame(() => document.getElementById(`rp-tab-${n}`)?.focus());
  };
  return (
    <div className="runp">
      <div className="sr-only" aria-live="assertive">{announce}</div>
      {header}
      <div className="rp-tabs" role="tablist" aria-label={t("rp.views")} onKeyDown={onTabsKey}>
        {tab("setup", t("rp.tab.setup"))}
        {tab("run", <>{t("rp.tab.run")}{run && <span className={`rp-dot st-${run.status}`} aria-hidden="true" />}
          {needHere > 0 && <span className="rp-count" title={t("rp.needsYou")} aria-label={t("rp.needsYou")}>⏸</span>}</>)}
        {tab("history", <>{t("rp.tab.history")} <span className="muted">{histTotal}</span></>)}
      </div>
      <div className="rp-view" role="tabpanel" id="rp-view-setup" aria-labelledby="rp-tab-setup" hidden={view !== "setup"}>
        <SetupView flow={flow} doc={doc} dirty={dirty} mock={mock} running={running} past={past} openRun={run?.input}
          prefill={prefill} onStarted={(r) => { show(r, true); }}
          queue={queue.queued} queueLog={queue.log} onQueued={loadWaiting}
          onUnqueue={(id) => ra.unqueue(id).then(loadWaiting).catch((e) => setRunErr(String((e as Error).message)))}
          onOpenQueued={(inp) => openPast(inp)} />
      </div>
      <div className="rp-view" role="tabpanel" id="rp-view-run" aria-labelledby="rp-tab-run" hidden={view !== "run"}>{current}</div>
      <div className="rp-view" role="tabpanel" id="rp-view-history" aria-labelledby="rp-tab-history" hidden={view !== "history"}>
        <div className="rp-body">
          <HistoryView flow={flow} spec={spec} current={run?.input} refresh={histKey} graph={graph}
            onOpen={(inp) => openPast(inp)} onOpenStep={openStep} onRerun={(p) => rerun(p.input, p.mode, p.target)} onEdit={(inp) => editRun(inp)}
            onFromStep={(inp) => runFromStep(inp)}
            onChanged={(inp, gone) => { loadPast(); if (gone && run?.input === inp) clear(); }} />
        </div>
      </div>
      {compare && <Compare flow={flow} spec={spec} base={compare} onClose={() => setCompare(null)}
        onOpenStep={(inp, node) => { setCompare(null); openStep(inp, node); }} />}
      {shown && <OutputViewer {...shown} copied={copied === shown.title} onClose={() => setShown(null)}
        onCopy={() => copy(pretty(shown.value), shown.title)}
        onDownload={() => {
          if (shown.media) {  // a run file: download it as it is
            const a = document.createElement("a");
            a.href = shown.media.url; a.download = shown.title; a.click();
            return;
          }
          download(`${shown.title}${shown.md ? ".md" : typeof shown.value === "string" ? ".txt" : ".json"}`, pretty(shown.value),
            shown.md ? "text/markdown" : "text/plain");
        }} />}
    </div>
  );
}
