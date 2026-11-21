// "Raw" shape of flow.yaml (see src/agent_flow_studio/builder/spec.py). The server validates everything.

export interface ArtifactRaw {
  name: string;
  contract?: string | null;
  draft?: boolean;
}

/** Kinds of typed values (builder/types.py IOKind). */
export const IO_KINDS = ["text", "markdown", "number", "integer", "boolean", "date", "datetime", "url",
  "path", "file", "choice", "json"] as const;
export type IOKind = (typeof IO_KINDS)[number];

/** A typed value: flow input or agent output (builder/types.py IOType). */
export interface IOTypeRaw {
  type?: IOKind;
  label?: string;
  description?: string;
  required?: boolean;
  default?: unknown;
  multiple?: boolean;
  min?: number | null;
  max?: number | null;
  pattern?: string | null;
  options?: string[];
  accept?: string[];
  contract?: string | null;
  draft?: boolean;
  show_if?: string | null;  // flow inputs: shown only when this expression on earlier inputs holds
}

/** An agent input: from a flow input (input.<x>) or an agent output (<agent>.<out>), or free. */
export interface AgentInputRaw {
  from?: string | null;
  type?: IOKind | null;
  description?: string;
  required?: boolean;
}

export interface AgentRaw {
  description: string;
  prompt?: string | null;
  prompt_file?: string | null;
  tools?: string[];
  model?: string | null;
  skills?: string[];
  max_turns?: number | null;
  reads?: string[];
  writes?: ArtifactRaw[];
  connections?: string[];
  write_connections?: string[];
  autonomy?: Autonomy | null;
  inputs?: Record<string, AgentInputRaw>;
  outputs?: Record<string, IOTypeRaw>;
  join?: "all" | "any";
  max_runs?: number;
  icon?: string | null;  // a built-in icon id (icons.tsx) or an emoji
  origin?: { agent: string; hash: string } | null;  // copied from the agent library
  // an action node (graph mode): the studio runs this connector action, no model
  action?: { connector: string; name: string; params?: Record<string, string>;
    outputs?: Record<string, { from?: string; match?: string }> } | null;
}

/** An agent of the library (GET /api/agents): copied into flows, then tuned there. */
export interface LibraryAgent {
  id: string;
  title: string;
  source: string;  // "core", "local" or an extension name
  location?: string;  // "public" (the built-in library), "local" or an extension
  agent_yaml?: string;  // the definition as the library window edits it
  prompt?: string;
  hash: string;
  description: string;
  icon?: string | null;
  agent: AgentRaw;  // prompt inline
}

/** A saved assistant command (GET /api/commands): a message plus instructions sent with it. */
export interface SavedCommand {
  id: string;
  name: string;
  text: string;
  instructions: string;
  favorite: boolean;
}

/** A saved assistant conversation (runs/chats/<flow>/<id>.json); items as the chat shows them. */
export interface SavedChat {
  id: string;
  title: string;
  session_id: string | null;
  cost: number;
  created: number;
  updated: number;
  items: Record<string, unknown>[];
}

export interface ChatSummary { id: string; title: string; created: number; updated: number; messages: number }

/** State of an agent copied from the library (validation `origins`). */
export interface OriginState {
  agent: string;
  title: string;
  source: string | null;
  missing: boolean;
  changed: boolean | null;  // changed in this flow since it was copied (null = unknown for now)
  update: boolean;  // the library has another version now
}

export interface GateRaw {
  kind: "human" | "stop" | "decision";  // decision: no pause, the first branch whose `if` holds
  label: string;
  message?: string;  // stop: the result shown at the end, with {expression} parts
}

export interface EdgeRaw {
  from: string;
  to: string;
  when?: string;
  if?: string;
  label?: string;  // shown on the canvas (e.g. "yes" / "no" on a decision branch)
}

export interface McpRaw {
  type: "stdio" | "http" | "sse";
  description?: string;
  command?: string | null;
  args?: string[];
  env?: Record<string, string>;
  url?: string | null;
  headers?: Record<string, string>;
  tools: string[];
  approved?: boolean;
  read_only?: boolean;
}

export type Autonomy = "approve" | "approve_prod" | "auto";

export interface WorkspaceRaw {
  path: string;
  environment?: "dev" | "staging" | "prod";
  description?: string;
  protected_branches?: string[];
}

export interface FlowRaw {
  version?: number;
  name: string;
  title: string;
  description?: string;
  model?: string | null;
  max_turns?: number;
  execution?: "orchestrator" | "graph";
  max_parallel?: number;
  inputs?: Record<string, IOTypeRaw>;
  input?: { label?: string; pattern?: string };
  task?: string;
  orchestrator?: { prompt?: string | null; prompt_file?: string | null };
  start: string;
  agents: Record<string, AgentRaw>;
  gates?: Record<string, GateRaw>;
  edges?: EdgeRaw[];
  mcp_servers?: Record<string, McpRaw>;
  autonomy?: Autonomy;
  workspace?: WorkspaceRaw | null;
  // w, h: the size the person gave the node in cards; compact and avatar keep their own
  layout?: Record<string, { x: number; y: number; w?: number; h?: number;
    compact?: { w: number; h: number }; avatar?: { w: number; h: number } }>;
}

export interface Doc {
  spec: FlowRaw;
  prompts: Record<string, string>;
}

export interface FlowError {
  loc: string;
  msg: string;  // the server's English text
  nodes: string[];
  code?: string;  // with params: shown in the UI language (i18n/server.ts)
  params?: Record<string, import("./i18n/server").ServerParam>;
}

export interface Validation {
  ok: boolean;
  errors: FlowError[];
  flow_section?: string;
  allowed_tools?: string[];
  origins?: Record<string, OriginState>;
}

export interface ToolInfo {
  id: string;
  server: string;
  name: string;
  description: string;
  writes_local_only: boolean;
  kind: "read" | "write_local" | "write";
}

export interface Catalog {
  tools: ToolInfo[];
  servers: string[];
  contracts: Record<string, unknown>;
  models: string[];
  mock: boolean;
  connections: Record<string, { type: string; environment: string; description: string; access: string;
    actions_only?: boolean; actions?: Record<string, { access: string; description: string; params?: string[] }> }>;
  locations?: string[];  // where things can be kept: "local" and the loaded extensions
}

export interface FlowSummary {
  name: string;
  title: string;
  valid: boolean;
  agents?: number;
  gates?: number;
  error?: string;
  location?: string;  // "local" (flows/) or an extension
  public?: boolean;  // git would publish it (flows/ entries not ignored)
}

export interface TemplateParam {
  label: string;
  kind: "path" | "branch" | "connector" | "text";
  default: string | null;
  required: boolean;
  hint: string;
  connector_types: string[];
  connector_access: "read" | "write" | null;
}

export interface FlowTemplate {
  id: string;
  source: string;
  title: string;
  description: string;
  params: Record<string, TemplateParam>;
}

/** Saved values of a run (GET /api/io/<run>?flow=). */
export interface RunIO {
  inputs: Record<string, unknown>;
  outputs: Record<string, Record<string, unknown>>;
  gates: Record<string, { decision: string; comment: string }>;
  given: Record<string, Record<string, unknown>>;
}

export type Selection =
  | { kind: "node"; id: string }
  | { kind: "edge"; index: number }
  | { kind: "data"; agent: string; input: string }  // a data link (an input's `from`)
  | null;
