import type {
  AgentRaw, Catalog, ChatSummary, Doc, FlowSummary, FlowTemplate, LibraryAgent, RunIO, SavedChat, SavedCommand,
  Validation,
} from "./types";

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
};
