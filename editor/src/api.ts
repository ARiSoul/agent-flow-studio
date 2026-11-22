import type {
  AgentRaw, BoardFlow, Catalog, ChatSummary, CopyItem, CopyPlan, CopyResult, Doc, FlowSummary, FlowTemplate,
  HandoffRun, LibraryAgent, RunIO, SavedChat, SavedCommand, WorkspaceDetail, WorkspaceItem, WorkspaceNow,
  WorkspaceContext, WorkspaceSpecRaw,
  Validation,
} from "./types";
import type { ServerParam } from "./i18n/server";

// Requests that change files carry X-Flow-Editor (the server rejects them without it).
const MUTATE = { "Content-Type": "application/json", "X-Flow-Editor": "1" };

/** Thrown when the API route itself does not exist: the editor page is newer than the server. */
export const SERVER_OUTDATED = "SERVER_OUTDATED";

/** An error as text for the user; `t` translates the "restart the server" case. */
export const PRIVATE_TERMS = "PRIVATE_TERMS:";

/** An error as text for the user; `t` translates the "restart the server" and "private words" cases. */
export const errorText = (e: unknown,
  t: (k: "app.serverOutdated" | "loc.blocked", v?: Record<string, string>) => string) => {
  const m = String((e as Error)?.message ?? e);
  if (m === SERVER_OUTDATED) return t("app.serverOutdated");
  if (m.startsWith(PRIVATE_TERMS)) return t("loc.blocked", { terms: m.slice(PRIVATE_TERMS.length) });
  return m;
};

/** An error from the server with a message code (the editor shows it in the UI language). */
export class ServerError extends Error {
  code?: string;
  params?: Record<string, ServerParam>;
  constructor(text: string, code?: string, params?: Record<string, ServerParam>) {
    super(text);
    this.code = code;
    this.params = params;
  }
}

/** 422 with {msg, code, params} (workspace and copy endpoints) as a ServerError. */
function msgError(data: unknown): ServerError {
  const d = (data as { detail?: unknown }).detail;
  if (d && typeof d === "object" && "msg" in d) {
    const m = d as { msg: string; code?: string; params?: Record<string, ServerParam> };
    return new ServerError(m.msg, m.code, m.params);
  }
  return new ServerError(typeof d === "string" ? d : JSON.stringify(d ?? "invalid"));
}

/** The subfolders of a folder on this machine (the folder browser). */
export interface FolderList {
  path: string; parent: string | null; home: string; roots: string[]; repo: boolean; workspace: boolean;
  folders: { name: string; path: string; repo: boolean; workspace: boolean }[];
}

/** The name of the open workspace (cookie afs_ws; "studio" when none). */
export function currentWorkspaceName(): string {
  return decodeURIComponent(document.cookie.match(/(?:^|; )afs_ws=([^;]*)/)?.[1] ?? "studio");
}

/** The open workspace and environment, kept in cookies: the server reads them on every request. */
export function setWorkspaceCookies(ws: string, env: string | null) {
  const opts = "; path=/; SameSite=Strict; max-age=31536000";
  document.cookie = `afs_ws=${encodeURIComponent(ws)}${opts}`;
  document.cookie = env ? `afs_env=${encodeURIComponent(env)}${opts}` : "afs_env=; path=/; max-age=0";
}

async function call<T>(url: string, init?: RequestInit, keep: number[] = []): Promise<{ status: number; data: T }> {
  const r = await fetch(url, init);
  const data = await r.json().catch(() => ({ detail: r.statusText }));
  if (!r.ok && r.status !== 422 && !keep.includes(r.status)) {
    const d = (data as { detail?: unknown }).detail;
    if (r.status === 404 && d === "Not Found") throw new Error(SERVER_OUTDATED);  // FastAPI: no such route
    const terms = (d as { private_terms?: string[] } | undefined)?.private_terms;
    if (terms) throw new Error(PRIVATE_TERMS + terms.join(", "));  // public: names private words
    throw new Error(typeof d === "string" ? d : JSON.stringify(d ?? r.statusText));
  }
  return { status: r.status, data: data as T };
}

export const api = {
  catalog: () => call<Catalog>("/api/catalog").then((r) => r.data),
  flows: () => call<FlowSummary[]>("/api/flows").then((r) => r.data),
  load: (name: string) =>
    call<Doc & Validation>(`/api/flows/${encodeURIComponent(name)}`).then((r) => r.data),
  validate: (name: string, doc: Doc) =>
    call<Validation>(`/api/flows/${encodeURIComponent(name)}/validate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(doc),
    }).then((r) => r.data),
  /** force: save even with problems (after the person confirms); a secret in the flow is never saved. */
  save: (name: string, doc: Doc, force = false, allowPrivate = false) =>
    call<Validation & { saved?: boolean; with_problems?: boolean; blocked_by_secret?: boolean;
      private_terms?: string[] }>(
      `/api/flows/${encodeURIComponent(name)}`, {
        method: "PUT",
        headers: MUTATE,
        body: JSON.stringify({ ...doc, force, allow_private: allowPrivate }),
      }, [409]).then((r) => r.data),
  io: (run: string, flow: string) =>
    call<RunIO>(`/api/io/${encodeURIComponent(run)}?flow=${encodeURIComponent(flow)}`).then((r) => r.data),
  templates: () => call<FlowTemplate[]>("/api/templates").then((r) => r.data),
  create: (name: string, title: string, from?: { copy_from?: string; template?: string; params?: Record<string, string> },
    location = "local") =>
    call<{ name: string }>("/api/flows", {
      method: "POST",
      headers: MUTATE,
      body: JSON.stringify({ name, title, location, ...from }),
    }).then((r) => {
      if (r.status === 422) throw new Error(String((r.data as { detail?: unknown }).detail ?? "invalid"));
      return r.data;
    }),
  chats: (flow: string) => call<ChatSummary[]>(`/api/chats/${encodeURIComponent(flow)}`).then((r) => r.data),
  chat: (flow: string, id: string) =>
    call<SavedChat>(`/api/chats/${encodeURIComponent(flow)}/${id}`).then((r) => r.data),
  saveChat: (flow: string, id: string, c: Omit<SavedChat, "id" | "created" | "updated">) =>
    call<ChatSummary>(`/api/chats/${encodeURIComponent(flow)}/${id}`, {
      method: "PUT", headers: MUTATE, body: JSON.stringify(c),
    }).then((r) => {
      if (r.status === 422) throw new Error(String((r.data as { detail?: unknown }).detail ?? "invalid"));
      return r.data;
    }),
  deleteChat: (flow: string, id: string) =>
    call<{ deleted: string }>(`/api/chats/${encodeURIComponent(flow)}/${id}`, { method: "DELETE", headers: MUTATE })
      .then((r) => r.data),
  commands: () => call<SavedCommand[]>("/api/commands").then((r) => r.data),
  saveCommand: (c: Omit<SavedCommand, "id"> & { id?: string }) =>
    call<SavedCommand>(`/api/commands/${c.id ?? "new"}`, {
      method: "PUT",
      headers: MUTATE,
      body: JSON.stringify(c),
    }).then((r) => {
      if (r.status === 422) throw new Error(String((r.data as { detail?: unknown }).detail ?? "invalid"));
      return r.data;
    }),
  deleteCommand: (id: string) =>
    call<{ deleted: string }>(`/api/commands/${id}`, { method: "DELETE", headers: MUTATE }).then((r) => r.data),
  library: () => call<LibraryAgent[]>("/api/agents").then((r) => r.data),
  saveToLibrary: (body: { id: string; title: string; agent?: AgentRaw; agent_yaml?: string; prompt: string;
    overwrite?: boolean; location?: string }) =>
    call<{ id: string; hash: string; source: string; notes: string[] }>("/api/agents", {
      method: "POST",
      headers: MUTATE,
      body: JSON.stringify(body),
    }).then((r) => {
      if (r.status === 422) throw new Error(String((r.data as { detail?: unknown }).detail ?? "invalid"));
      return r.data;
    }),
  deleteLibraryAgent: (id: string) =>
    call<{ deleted: string }>(`/api/agents/${id}`, { method: "DELETE", headers: MUTATE }).then((r) => r.data),
  moveLibraryAgent: (id: string, location: string) =>
    call<{ id: string; source: string }>(`/api/agents/${id}/move`, {
      method: "POST", headers: MUTATE, body: JSON.stringify({ location }) }).then((r) => {
      if (r.status === 422) throw new Error(String((r.data as { detail?: unknown }).detail ?? "invalid"));
      return r.data;
    }),
  moveFlow: (name: string, location: string) =>
    call<{ location: string }>(`/api/flows/${encodeURIComponent(name)}/move`, {
      method: "POST", headers: MUTATE, body: JSON.stringify({ location }) }).then((r) => r.data),
  remove: (name: string) =>
    call<{ trashed: string }>(`/api/flows/${encodeURIComponent(name)}`, {
      method: "DELETE",
      headers: MUTATE,
    }).then((r) => r.data),
  // ---- studio workspaces
  workspaces: () => call<{ workspaces: WorkspaceItem[]; current: WorkspaceNow }>("/api/workspaces").then((r) => r.data),
  workspace: (name: string) => call<WorkspaceDetail>(`/api/workspaces/${encodeURIComponent(name)}`).then((r) => r.data),
  createWorkspace: (body: { name: string; title?: string; where: "repo" | "local"; repo?: string }) =>
    call<WorkspaceDetail>("/api/workspaces", { method: "POST", headers: MUTATE, body: JSON.stringify(body) })
      .then((r) => { if (r.status === 422) throw msgError(r.data); return r.data; }),
  openWorkspace: (path: string) =>
    call<WorkspaceDetail>("/api/workspaces/open", { method: "POST", headers: MUTATE, body: JSON.stringify({ path }) })
      .then((r) => { if (r.status === 422) throw msgError(r.data); return r.data; }),
  updateWorkspace: (name: string, body: Partial<WorkspaceSpecRaw>) =>
    call<WorkspaceDetail>(`/api/workspaces/${encodeURIComponent(name)}`, {
      method: "PUT", headers: MUTATE, body: JSON.stringify(body) })
      .then((r) => { if (r.status === 422) throw msgError(r.data); return r.data; }),
  forgetWorkspace: (name: string) =>
    call<{ forgotten: string }>(`/api/workspaces/${encodeURIComponent(name)}`, { method: "DELETE", headers: MUTATE })
      .then((r) => r.data),
  duplicateWorkspace: (src: string, body: { name: string; title?: string; where: "repo" | "local"; repo?: string;
    same_secrets?: boolean }) =>
    call<WorkspaceDetail & CopyResult>(`/api/workspaces/${encodeURIComponent(src)}/duplicate`, {
      method: "POST", headers: MUTATE, body: JSON.stringify(body) })
      .then((r) => { if (r.status === 422) throw msgError(r.data); return r.data; }),
  workspaceFlows: (name: string) =>
    call<{ name: string; title: string }[]>(`/api/workspaces/${encodeURIComponent(name)}/flows`).then((r) => r.data),
  copyPlan: (src: string, item: CopyItem & { to: string; new_name?: string | null }) =>
    call<CopyPlan>(`/api/workspaces/${encodeURIComponent(src)}/copy-plan`, {
      method: "POST", headers: MUTATE, body: JSON.stringify(item) })
      .then((r) => { if (r.status === 422) throw msgError(r.data); return r.data; }),
  copy: (src: string, body: CopyItem & { to: string; new_name?: string | null; replace?: boolean;
    deps?: Record<string, string>; same_secrets?: boolean }) =>
    call<CopyResult>(`/api/workspaces/${encodeURIComponent(src)}/copy`, {
      method: "POST", headers: MUTATE, body: JSON.stringify(body) })
      .then((r) => { if (r.status === 422) throw msgError(r.data); return r.data; }),
  folders: (path: string) => call<FolderList>(`/api/folders?path=${encodeURIComponent(path)}`).then((r) => r.data),
  context: () => call<WorkspaceContext>("/api/context").then((r) => r.data),
  setContext: (name: string, text: string) =>
    call<WorkspaceContext>(`/api/context/${encodeURIComponent(name)}`, {
      method: "PUT", headers: MUTATE, body: JSON.stringify({ text }) })
      .then((r) => { if (r.status === 422) throw msgError(r.data); return r.data; }),
  /** A run of the context keeper (explore or update roots of the open workspace). */
  updateContext: (mode: "explore" | "update", roots?: string[] | null, after?: string | null) =>
    call<{ id: string; input: string; status: string }>("/api/context/update", {
      method: "POST", headers: MUTATE, body: JSON.stringify({ mode, roots: roots ?? null, after: after ?? null }) })
      .then((r) => r.data),
  runs: () => call<{ id: string; input: string; status: string; flow: string }[]>("/api/runs").then((r) => r.data),
  move: (src: string, body: CopyItem & { to: string; new_name?: string | null; replace?: boolean;
    deps?: Record<string, string>; same_secrets?: boolean }) =>
    call<CopyResult>(`/api/workspaces/${encodeURIComponent(src)}/move`, {
      method: "POST", headers: MUTATE, body: JSON.stringify(body) })
      .then((r) => { if (r.status === 422) throw msgError(r.data); return r.data; }),
  board: () => call<{ workspace: WorkspaceNow; flows: BoardFlow[] }>("/api/board").then((r) => r.data),
  // ---- hand-off: fill a flow input from a run of another flow
  handoffRuns: (flow: string, type?: string) =>
    call<{ runs: HandoffRun[] }>(`/api/handoff/${encodeURIComponent(flow)}${type ? `?type=${encodeURIComponent(type)}` : ""}`)
      .then((r) => r.data.runs),
  handoffValue: (flow: string, run: string, agent: string, output: string) =>
    call<{ value: unknown; from: { flow: string; run: string; agent: string; output: string } }>(
      `/api/handoff/${encodeURIComponent(flow)}/${encodeURIComponent(run)}/${agent}/${output}`).then((r) => r.data),
};
