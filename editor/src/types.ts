// "Raw" shape of flow.yaml (see src/agent_flow_studio/builder/spec.py). The server validates everything.

export interface ArtifactRaw {
  name: string;
  contract?: string | null;
  draft?: boolean;
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
}

export interface GateRaw {
  kind: "human" | "stop";
  label: string;
}

export interface EdgeRaw {
  from: string;
  to: string;
  when?: string;
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
  layout?: Record<string, { x: number; y: number }>;
}

export interface Doc {
  spec: FlowRaw;
  prompts: Record<string, string>;
}

export interface FlowError {
  loc: string;
  msg: string;
  nodes: string[];
}

export interface Validation {
  ok: boolean;
  errors: FlowError[];
  flow_section?: string;
  allowed_tools?: string[];
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
  connections: Record<string, { type: string; environment: string; description: string; access: string }>;
}

export interface FlowSummary {
  name: string;
  title: string;
  valid: boolean;
  agents?: number;
  gates?: number;
  error?: string;
}

export type Selection = { kind: "node"; id: string } | { kind: "edge"; index: number } | null;
