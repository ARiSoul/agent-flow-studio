// What the Run panel asks the server to do with runs: start, queue, open, stop, resume, continue,
// decide at a gate, approve a write, ask for more, retry a step now or cancel its retry, dismiss a
// waiting run. No React here, so it is tested with a fake fetch.

/** The headers of every request that changes something (the server checks X-Flow-Editor). */
export const MUTATE = { "Content-Type": "application/json", "X-Flow-Editor": "1" };

/** An error of the server: its text, its HTTP status and its detail (an object for a 422). */
export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(message: string, status: number, detail: unknown) { super(message); this.status = status; this.detail = detail; }
}

type Method = "POST" | "PUT" | "DELETE";
const enc = encodeURIComponent;

export function runApi(f: typeof fetch = (...a) => fetch(...a)) {
  async function call<T>(url: string, method: Method = "POST", body?: unknown): Promise<T> {
    const r = await f(url, { method, headers: MUTATE, body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = (data as { detail?: unknown }).detail;
      throw new ApiError(typeof d === "string" ? d : d ? JSON.stringify(d) : r.statusText, r.status, d);
    }
    return data as T;
  }
  return {
    call,
    start: <T>(body: unknown) => call<T>("/api/runs", "POST", body),
    /** A run that starts by itself when the one in progress stops. */
    queue: <T>(body: unknown) => call<T>("/api/runs-queue", "POST", body),
    unqueue: (item: string) => call(`/api/runs-queue/${enc(item)}`, "DELETE"),
    /** A queued run starts now, even while other runs wait for the person. */
    startQueued: <T>(item: string) => call<T>(`/api/runs-queue/${enc(item)}/start`),
    open: <T>(input: string) => call<T>("/api/runs/open", "POST", { input }),
    stop: <T>(id: string) => call<T>(`/api/runs/${id}/stop`),
    /** Pauses the running agents (all, or one): Continue goes on in their own sessions. */
    pause: <T>(id: string, node?: string) => call<T>(`/api/runs/${id}/pause`, "POST", { node: node ?? null }),
    resume: <T>(id: string, reuseChanged: boolean) => call<T>(`/api/runs/${id}/resume`, "POST", { reuse_changed: reuseChanged }),
    cont: <T>(id: string, how: "step" | "full") => call<T>(`/api/runs/${id}/continue`, "POST", { how }),
    decide: <T>(id: string, decision: "approve" | "reject" | "answer", comment: string) =>
      call<T>(`/api/runs/${id}/decision`, "POST", { decision, comment }),
    approveWrite: (id: string, approval: string, ok: boolean, comment: string) =>
      call(`/api/runs/${id}/approvals/${enc(approval)}`, "POST", { approve: ok, comment }),
    ask: <T>(id: string, message: string, agent: string) => call<T>(`/api/runs/${id}/ask`, "POST", { message, agent }),
    /** A step that waits to run again after a temporary error: now, or cancel the retry. */
    retry: <T>(id: string, node: string | null, action: "now" | "cancel") => call<T>(`/api/runs/${id}/retry`, "POST", { node, action }),
    /** Changing a recorded decision: its run does not go on meanwhile (`on`), or keep it. */
    holdDecision: <T>(id: string, on: boolean) => call<T>(`/api/runs/${id}/decision/hold`, "POST", { dismissed: on }),
    /** A waiting run out of (or back into) the "waits for you" count. */
    dismiss: (input: string, dismissed: boolean) => call(`/api/runs/${enc(input)}/dismissed`, "PUT", { dismissed }),
  };
}

export type RunApi = ReturnType<typeof runApi>;
