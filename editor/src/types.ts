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
  // flow inputs: the output of another flow that usually gives this value ("Fill from a run")
  source?: { flow: string; output: string } | null;
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
  // a subflow node (graph mode): another flow of the workspace runs as one step
  subflow?: SubflowRaw | null;
}

export interface SubflowRaw {
  flow: string;
  inputs?: Record<string, string>;  // child flow input -> text with {expression} parts
  outputs?: Record<string, { from: string }>;  // node output -> "<agent>.<output>" or "message"
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
  path?: string;  // a folder (absolute, or relative to where the studio runs), or…
  root?: string;  // …a root of the studio workspace
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
  retry?: { transient?: number; wait?: number };  // graph: temporary API errors run again
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
  // studio workspaces: the agents read the workspace context; after a finished run, update it
  use_context?: boolean;
  context_update?: "suggest" | "always" | "never";
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
  warnings?: FlowError[];  // the flow works, but something is probably wrong (unused input)
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
  default_model?: string | null;  // the studio's model (AFS_MODEL); null: the Claude CLI default
  mock: boolean;
  connections: Record<string, { type: string; environment: string; description: string; access: string;
    actions_only?: boolean; actions?: Record<string, { access: string; description: string; params?: string[] }> }>;
  locations?: string[];  // where things can be kept: "local" and the loaded extensions
  workspace?: WorkspaceNow;  // the open studio workspace and environment
}

/** The open studio workspace (cookie afs_ws) and environment (afs_env). */
export interface WorkspaceNow {
  name: string;
  title?: string;
  where: "studio" | "repo" | "local";
  environment: string | null;
  environments: string[];
  kind: "dev" | "staging" | "prod" | null;
  vars?: string[];
  roots?: string[];
}

export interface WorkspaceItem {
  name: string;
  title: string;
  where: "studio" | "repo" | "local" | "missing";
  folder: string | null;
  ok: boolean;
  environments: string[];
  default_environment: string | null;
}

export interface WorkspaceSpecRaw {
  version?: number;
  name: string;
  title?: string;
  description?: string;
  roots: Record<string, { path: string; description?: string }>;
  vars: Record<string, string>;
  environments: Record<string, { kind: "dev" | "staging" | "prod"; vars?: Record<string, string> }>;
  default_environment?: string | null;
  /** The web app the person runs locally (a game, a site): the Run panel shows it. */
  preview?: { url: string; start?: string } | null;
}

export interface WorkspaceDetail {
  name: string;
  where: "studio" | "repo" | "local";
  folder: string | null;
  home: string | null;
  spec?: WorkspaceSpecRaw;
  roots?: Record<string, string>;  // absolute folders
  renamed_roots?: Record<string, string>;  // after a save: old name → new (same folder)
}

export type CopyKind = "flow" | "agent" | "flow_agent" | "connector" | "vars";
export interface CopyItem {
  kind: CopyKind;
  name?: string;
  agent?: string;
  to_flow?: string | null;
  vars?: string[];
  environments?: string[];
}
export interface CopyDep {
  kind: "var" | "root" | "connector" | "agent" | "flow";
  name: string;
  in_source: boolean;
  in_target: boolean;
  copyable: boolean;
  choice: "copy" | "target" | "leave";
}
export interface CopyPlan {
  kind: CopyKind;
  name?: string;
  clash?: boolean;
  suggestion?: string;
  deps: CopyDep[];
  secrets?: string[];
  clash_vars?: string[];
  clash_environments?: string[];
}
export interface CopyResult {
  copied: { kind: string; name: string; as: string }[];
  secrets: string[];
  missing_secrets: string[];
}

export interface BoardFlow {
  name: string;
  title: string;
  description?: string;
  valid: boolean;
  execution?: string;
  inputs: Record<string, { type: string; label: string; source?: { flow: string; output: string } }>;
  outputs: { agent: string; name: string; type: string }[];
  subflows: string[];
  handoffs: { flow: string; output: string; input: string }[];
  last_run?: { input: string; status: string; started: number };
}

/** The workspace context (GET /api/context). */
export interface WorkspaceContext {
  studio: boolean;
  file?: string | null;
  sections: Record<string, string>;  // root name or "notes" → Markdown
  roots: Record<string, { path: string; exists: boolean; git: boolean; explored: boolean;
    updated: number | null; commit: string | null; commits_since: number | null;
    history?: { exists: boolean; updated: number | null } }>;
  chars?: number;
  limit?: number;
  drafts?: Record<string, HistoryDraft>;  // root → history learned from past sessions, to review
}

export type SourceState = "new" | "changed" | "learned";
/** A past Claude Code session about a root's folder. */
export interface SessionSource {
  id: string; title: string; first_prompt: string; branch: string | null;
  created: number | null; modified: number | null; size: number | null;
  kind: "person" | "studio" | "program"; state: SourceState;
}
/** A Claude Code memory note about a root's folder. */
export interface MemorySource {
  file: string; name: string; description: string; size: number; modified: number; state: SourceState;
}
export interface ContextSources { root: string; folder: string; sessions: SessionSource[]; memory: MemorySource[] }
export interface HistoryDraft {
  root: string; run: string; section: string; summary: string;
  sessions: Record<string, number>; memory: Record<string, number>; created: number;
}

export interface HandoffRun {
  input: string;
  started: number;
  status: string;
  outputs: { agent: string; name: string; type: string; label: string; preview: string }[];
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
