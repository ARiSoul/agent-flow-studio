import type { Catalog, Doc, FlowSummary, FlowTemplate, RunIO, Validation } from "./types";

// Requests that change files carry X-Flow-Editor (the server rejects them without it).
const MUTATE = { "Content-Type": "application/json", "X-Flow-Editor": "1" };

async function call<T>(url: string, init?: RequestInit): Promise<{ status: number; data: T }> {
  const r = await fetch(url, init);
  const data = await r.json().catch(() => ({ detail: r.statusText }));
  if (!r.ok && r.status !== 422) {
    const d = (data as { detail?: unknown }).detail;
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
  save: (name: string, doc: Doc) =>
    call<Validation & { saved?: boolean }>(`/api/flows/${encodeURIComponent(name)}`, {
      method: "PUT",
      headers: MUTATE,
      body: JSON.stringify(doc),
    }).then((r) => r.data),
  io: (run: string, flow: string) =>
    call<RunIO>(`/api/io/${encodeURIComponent(run)}?flow=${encodeURIComponent(flow)}`).then((r) => r.data),
  templates: () => call<FlowTemplate[]>("/api/templates").then((r) => r.data),
  create: (name: string, title: string, from?: { copy_from?: string; template?: string; params?: Record<string, string> }) =>
    call<{ name: string }>("/api/flows", {
      method: "POST",
      headers: MUTATE,
      body: JSON.stringify({ name, title, ...from }),
    }).then((r) => {
      if (r.status === 422) throw new Error(String((r.data as { detail?: unknown }).detail ?? "invalid"));
      return r.data;
    }),
  remove: (name: string) =>
    call<{ trashed: string }>(`/api/flows/${encodeURIComponent(name)}`, {
      method: "DELETE",
      headers: MUTATE,
    }).then((r) => r.data),
};
