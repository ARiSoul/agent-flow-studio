import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { useI18n } from "./i18n";
import type { NodeRun } from "./RunPanel";
import type { AgentRaw, GateRaw, IOKind } from "./types";

/** One typed port: an input (left, handle `in:<name>`) or an output (right, handle `out:<name>`). */
export type Port = { name: string; type: IOKind; multiple: boolean; from?: string | null; linked?: boolean };
export type AgentNodeData = {
  name: string; agent: AgentRaw; start: boolean; error: boolean; run?: NodeRun;
  ins: Port[]; outs: Port[];
};
export type GateNodeData = { name: string; gate: GateRaw; error: boolean; run?: NodeRun };
export type AgentNodeT = Node<AgentNodeData, "agent">;
export type GateNodeT = Node<GateNodeData, "gate">;

const SERVER_ICON: Record<string, string> = { artifacts: "AR", connections: "API", workspace: "WS", io: "IO" };
const icon = (s: string) => SERVER_ICON[s] ?? s.slice(0, 3).toUpperCase();

/** Short tag of a type, shown next to a port. */
export const TYPE_TAG: Record<IOKind, string> = {
  text: "abc", markdown: "md", number: "1.5", integer: "123", boolean: "y/n", date: "date",
  datetime: "time", url: "url", path: "path", file: "file", choice: "opt", json: "{}",
};
export const typeTag = (k: IOKind, multiple?: boolean) => `${TYPE_TAG[k] ?? k}${multiple ? "[]" : ""}`;

function Ports({ ins, outs }: { ins: Port[]; outs: Port[] }) {
  const { t } = useI18n();
  const typeName = (p: Port) => `${t(`io.type.${p.type}`)}${p.multiple ? ` · ${t("io.list")}` : ""}`;
  return (
    <div className="ports">
      <div className="ports-col in">
        {ins.map((p) => (
          <div key={p.name} className={`port${p.from ? " fed" : ""}`}
            title={`${p.name} · ${typeName(p)}${p.from ? `\n← ${p.from}` : `\n${t("node.freeInput")}`}`}>
            <Handle type="target" position={Position.Left} id={`in:${p.name}`} className={`h-data t-${p.type}`} />
            <span className="port-tag">{typeTag(p.type, p.multiple)}</span>
            <span className="port-name">{p.name}</span>
            {p.from?.startsWith("input.") && <span className="port-src">{p.from.slice(6)}</span>}
          </div>
        ))}
        <div className="port add" title={t("node.newInputTitle")}>
          <Handle type="target" position={Position.Left} id="in:+" className="h-data h-new" />
          <span className="port-name">{t("node.newInput")}</span>
        </div>
      </div>
      <div className="ports-col out">
        {outs.map((p) => (
          <div key={p.name} className="port" title={`${p.name} · ${typeName(p)}`}>
            <span className="port-name">{p.name}</span>
            <span className="port-tag">{typeTag(p.type, p.multiple)}</span>
            <Handle type="source" position={Position.Right} id={`out:${p.name}`} className={`h-data t-${p.type}`} />
          </div>
        ))}
      </div>
    </div>
  );
}

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
        {a.join === "any" && <span className="badge" title={t("node.joinAnyTitle")}>{t("node.joinAny")}</span>}
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
      <Ports ins={data.ins} outs={data.outs} />
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
