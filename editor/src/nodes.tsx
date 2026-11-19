import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { useI18n } from "./i18n";
import type { AgentRaw, GateRaw } from "./types";

export type AgentNodeData = { name: string; agent: AgentRaw; start: boolean; error: boolean; run?: string };
export type GateNodeData = { name: string; gate: GateRaw; error: boolean; run?: string };
export type AgentNodeT = Node<AgentNodeData, "agent">;
export type GateNodeT = Node<GateNodeData, "gate">;

const SERVER_ICON: Record<string, string> = { artifacts: "AR", connections: "API", workspace: "WS" };
const icon = (s: string) => SERVER_ICON[s] ?? s.slice(0, 3).toUpperCase();

export function AgentNode({ data, selected }: NodeProps<AgentNodeT>) {
  const { t } = useI18n();
  const a = data.agent;
  const servers = [...new Set((a.tools ?? []).map((t) => t.split(".")[0]))];
  const writes = a.writes ?? [];
  return (
    <div className={`node agent${selected ? " sel" : ""}${data.error ? " err" : ""}${data.run ? ` run-${data.run}` : ""}`}>
      <Handle type="target" position={Position.Top} />
      <div className="node-head">
        <span className="node-kind">{t("node.agent")}</span>
        {data.start && <span className="badge start">{t("node.start")}</span>}
        {a.model && <span className="badge">{a.model}</span>}
      </div>
      <div className="node-name">{data.name}</div>
      <div className="node-desc">{a.description}</div>
      <div className="node-foot">
        {servers.map((s) => (
          <span key={s} className="chip" title={(a.tools ?? []).filter((t) => t.startsWith(s + ".")).join("\n")}>
            {icon(s)}
          </span>
        ))}
        {writes.length > 0 && (
          <span className="chip out" title={writes.map((w) => w.name).join("\n")}>
            ↧ {writes.length}
          </span>
        )}
      </div>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

export function GateNode({ data, selected }: NodeProps<GateNodeT>) {
  const { t } = useI18n();
  const g = data.gate;
  return (
    <div className={`node gate ${g.kind}${selected ? " sel" : ""}${data.error ? " err" : ""}${data.run ? ` run-${data.run}` : ""}`}>
      <Handle type="target" position={Position.Top} />
      <div className="node-head">
        <span className="node-kind">{t(g.kind === "human" ? "node.human" : "node.stop")}</span>
      </div>
      <div className="node-name">{data.name}</div>
      <div className="node-desc">{g.label}</div>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

export const nodeTypes = { agent: AgentNode, gate: GateNode };
