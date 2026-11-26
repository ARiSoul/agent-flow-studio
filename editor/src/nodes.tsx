import { Handle, NodeResizer, Position, type Node, type NodeProps } from "@xyflow/react";
import { useI18n } from "./i18n";
import { AgentIcon, Icon } from "./icons";
import { agentMinHeight, type NodeStyle } from "./model";
import type { NodeRun } from "./RunPanel";
import type { AgentRaw, GateRaw, IOKind, OriginState } from "./types";

/** One typed port: an input (left, handle `in:<name>`) or an output (right, handle `out:<name>`). */
export type Port = { name: string; type: IOKind; multiple: boolean; from?: string | null; linked?: boolean };
export type AgentNodeData = {
  name: string; agent: AgentRaw; start: boolean; error: boolean; run?: NodeRun;
  ins: Port[]; outs: Port[]; style?: NodeStyle; lib?: OriginState;
  model?: AgentModel | null;  // null: the node runs no model (action, subflow)
};
/** The model an agent runs with, and where it comes from (its own, the flow's, the studio's). */
export type AgentModel = { name: string | null; from: "agent" | "flow" | "studio" };

function ModelBadge({ model }: { model?: AgentModel | null }) {
  const { t } = useI18n();
  if (!model) return null;
  const name = model.name ?? t("node.modelDefault");
  return (
    <span className={`badge model from-${model.from}`} title={t(`node.modelFrom.${model.from}`, { model: name })}>
      {name}
    </span>);
}

/** "lib" badge of an agent copied from the library (↑ when the library has another version). */
function LibBadge({ agent, lib }: { agent: AgentRaw; lib?: OriginState }) {
  const { t } = useI18n();
  if (!agent.origin) return null;
  const state = !lib ? "" : lib.missing ? t("lib.state.missing")
    : lib.changed && lib.update ? t("lib.state.both")
    : t(lib.changed ? "lib.state.changed" : lib.update ? "lib.state.update" : "lib.state.same");
  return (
    <span className={`badge lib${lib?.update || lib?.missing ? " warn" : ""}`}
      title={`${t("lib.from", { lib: lib?.title ?? agent.origin.agent })}${state ? ` · ${state}` : ""}`}>
      {t("node.lib")}{lib?.update ? " ↑" : lib?.changed ? " *" : ""}
    </span>
  );
}
export type GateNodeData = { name: string; gate: GateRaw; error: boolean; run?: NodeRun; style?: NodeStyle };
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

/** Run state of a node: a dot (filled and pulsing while it runs, hollow when it is stopped), a
 * "running" badge and a moving bar. */
function RunDot({ run }: { run?: NodeRun }) {
  const { t } = useI18n();
  const state = run ?? "idle";
  return <span className={`run-dot ${state}`} role="img" aria-label={t(`node.state.${state}`)}
    title={t(`node.state.${state}`)} />;
}

function RunningBadge({ run }: { run?: NodeRun }) {
  const { t } = useI18n();
  return run === "active" ? <span className="badge running">{t("node.state.active")}</span> : null;
}

const KitBar = ({ run }: { run?: NodeRun }) => (run === "active" ? <div className="kit-bar" aria-hidden="true" /> : null);

function portTitle(p: Port, t: ReturnType<typeof useI18n>["t"], input: boolean): string {
  const typeName = `${t(`io.type.${p.type}`)}${p.multiple ? ` · ${t("io.list")}` : ""}`;
  if (!input) return `${p.name} · ${typeName}`;
  return `${p.name} · ${typeName}\n${p.from?.startsWith("input.")
    ? t("node.fromFlowInput", { name: p.from.slice(6) })
    : p.from ? t("node.fromOutput", { ref: p.from }) : t("node.freeInput")}`;
}

/** Avatar style: the port handles sit around the circle (names in the tooltips). */
function RingPorts({ ins, outs }: { ins: Port[]; outs: Port[] }) {
  const { t } = useI18n();
  const at = (i: number, n: number) => ({ top: `${((i + 1) / (n + 1)) * 100}%` });
  const all = [...ins.map((p) => ({ p, add: false })), { p: null as Port | null, add: true }];
  return (
    <>
      {all.map(({ p, add }, i) => add
        ? <Handle key="+" type="target" position={Position.Left} id="in:+" className="h-data h-new ring"
          style={at(i, all.length)} title={t("node.newInputTitle")} />
        : <Handle key={p!.name} type="target" position={Position.Left} id={`in:${p!.name}`}
          className={`h-data ring t-${p!.type}`} style={at(i, all.length)} title={portTitle(p!, t, true)} />)}
      {outs.map((p, i) => (
        <Handle key={p.name} type="source" position={Position.Right} id={`out:${p.name}`}
          className={`h-data ring t-${p.type}`} style={at(i, outs.length)} title={portTitle(p, t, false)} />))}
    </>
  );
}

function Ports({ ins, outs, compact }: { ins: Port[]; outs: Port[]; compact?: boolean }) {
  const { t } = useI18n();
  const typeName = (p: Port) => `${t(`io.type.${p.type}`)}${p.multiple ? ` · ${t("io.list")}` : ""}`;
  return (
    <div className={`ports${compact ? " compact" : ""}`}>
      <div className="ports-col in">
        {ins.map((p) => (
          <div key={p.name} className={`port${p.from ? " fed" : ""}`}
            title={`${p.name} · ${typeName(p)}\n${p.from?.startsWith("input.")
              ? t("node.fromFlowInput", { name: p.from.slice(6) })
              : p.from ? t("node.fromOutput", { ref: p.from }) : t("node.freeInput")}`}>
            <Handle type="target" position={Position.Left} id={`in:${p.name}`} className={`h-data t-${p.type}`} />
            {!compact && <span className="port-tag">{typeTag(p.type, p.multiple)}</span>}
            <span className="port-name">{p.name}</span>
            {!compact && p.from?.startsWith("input.") && <span className="port-src">{p.from}</span>}
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
            {!compact && <span className="port-tag">{typeTag(p.type, p.multiple)}</span>}
            <Handle type="source" position={Position.Right} id={`out:${p.name}`} className={`h-data t-${p.type}`} />
          </div>
        ))}
      </div>
    </div>
  );
}


/** The resize handles of the selected node; the size goes into the flow's layout. */
function Resizer({ selected, minWidth = 160, minHeight = 56 }: { selected: boolean; style: NodeStyle;
  minWidth?: number; minHeight?: number }) {
  return <NodeResizer isVisible={selected} minWidth={minWidth} minHeight={minHeight} lineClassName="resize-line" handleClassName="resize-handle" />;
}

export function AgentNode({ data, selected }: NodeProps<AgentNodeT>) {
  const { t } = useI18n();
  const a = data.agent;
  const servers = [...new Set((a.tools ?? []).map((t) => t.split(".")[0]))];
  const writes = a.writes ?? [];
  const style = data.style ?? "card";
  const rows = Math.max(data.ins.length + 1, data.outs.length);  // port rows: the minimum height
  const cls = `node agent style-${style}${selected ? " sel" : ""}${data.error ? " err" : ""}${data.run ? ` run-${data.run}` : ""}`;
  if (style === "avatar") {
    return (
      <div className={cls} title={a.description}>
        <Resizer selected={!!selected} style={style} minWidth={70} minHeight={agentMinHeight(rows, style)} />
        <div className="avatar">
          <Handle type="target" id="c" position={Position.Top} />
          <AgentIcon icon={a.icon} size={34} />
          <RunDot run={data.run} />
          <RingPorts ins={data.ins} outs={data.outs} />
          <Handle type="source" id="c" position={Position.Bottom} />
        </div>
        <div className="avatar-name">{data.name}</div>
        <div className="avatar-badges">
          <ModelBadge model={data.model} />
          {data.start && <span className="badge start">{t("node.start")}</span>}
          <LibBadge agent={a} lib={data.lib} />
          <RunningBadge run={data.run} />
        </div>
        <KitBar run={data.run} />
      </div>
    );
  }
  if (style === "compact") {
    return (
      <div className={cls} title={a.description}>
        <Resizer selected={!!selected} style={style} minWidth={140} minHeight={agentMinHeight(rows, style)} />
        <Handle type="target" id="c" position={Position.Top} />
        <div className="node-head">
          <span className="node-icon"><AgentIcon icon={a.icon} size={18} /></span>
          <span className="node-name">{data.name}</span>
          <RunDot run={data.run} />
        </div>
        {(data.start || data.run === "active" || a.origin || data.model) && <div className="node-head">
          <ModelBadge model={data.model} />
          {data.start && <span className="badge start">{t("node.start")}</span>}
          <LibBadge agent={a} lib={data.lib} />
          <RunningBadge run={data.run} />
        </div>}
        <Ports ins={data.ins} outs={data.outs} compact />
        <KitBar run={data.run} />
        <Handle type="source" id="c" position={Position.Bottom} />
      </div>
    );
  }
  return (
    <div className={cls}>
      {/* never smaller than its ports need: a hidden port would leave its data links loose */}
      <Resizer selected={!!selected} style={style} minHeight={agentMinHeight(rows, style)} />
      <Handle type="target" id="c" position={Position.Top} />
      <div className="node-head">
        <span className="node-icon"><AgentIcon icon={a.icon} size={16} /></span>
        <span className="node-kind">{t(a.action ? "node.action" : a.subflow ? "node.subflow" : "node.agent")}</span>
        {a.subflow && <span className="badge sub" title={t("node.subflowTitle")}>↳ {a.subflow.flow || "—"}</span>}
        <RunningBadge run={data.run} />
        <RunDot run={data.run} />
        {data.start && <span className="badge start">{t("node.start")}</span>}
        <LibBadge agent={a} lib={data.lib} />
        {a.join === "any" && <span className="badge" title={t("node.joinAnyTitle")}>{t("node.joinAny")}</span>}
      </div>
      <div className="node-name">{data.name}</div>
      {data.model && <div className="node-model"><ModelBadge model={data.model} /></div>}
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
      <KitBar run={data.run} />
      <Handle type="source" id="c" position={Position.Bottom} />
    </div>
  );
}

export function GateNode({ data, selected }: NodeProps<GateNodeT>) {
  const { t } = useI18n();
  const g = data.gate;
  const style = data.style ?? "card";
  if (g.kind === "decision") {  // a diamond: the branches leave from the bottom, in order
    const cls = `node gate decision style-${style}${selected ? " sel" : ""}${data.error ? " err" : ""}${data.run ? ` run-${data.run}` : ""}`;
    return (
      <div className={cls} title={g.label}>
        <Resizer selected={!!selected} style={style} minWidth={120} minHeight={70} />
        <Handle type="target" id="c" position={Position.Top} />
        <svg className="diamond" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
          <polygon points="50,2 98,50 50,98 2,50" />
        </svg>
        <div className="diamond-text">
          {style === "card" && <span className="node-kind">{t("node.decision")}</span>}
          <span className="node-name">{data.name}</span>
          {style === "card" && <span className="node-desc">{g.label}</span>}
        </div>
        <RunDot run={data.run} />
        <Handle type="source" id="c" position={Position.Bottom} />
      </div>
    );
  }
  const icon = g.kind === "human" ? "hand" : "stop";
  const cls = `node gate ${g.kind} style-${style}${selected ? " sel" : ""}${data.error ? " err" : ""}${data.run ? ` run-${data.run}` : ""}`;
  if (style === "avatar") {
    return (
      <div className={cls} title={g.label}>
        <Resizer selected={!!selected} style={style} minWidth={70} minHeight={80} />
        <div className="avatar">
          <Handle type="target" id="c" position={Position.Top} />
          <Icon id={icon} size={30} />
          <RunDot run={data.run} />
          <Handle type="source" id="c" position={Position.Bottom} />
        </div>
        <div className="avatar-name">{data.name}</div>
      </div>
    );
  }
  return (
    <div className={cls} title={style === "compact" ? g.label : undefined}>
      <Resizer selected={!!selected} style={style} />
      <Handle type="target" id="c" position={Position.Top} />
      <div className="node-head">
        <span className="node-icon"><Icon id={icon} size={16} /></span>
        {style === "compact"
          ? <span className="node-name">{data.name}</span>
          : <span className="node-kind">{t(g.kind === "human" ? "node.human" : "node.stop")}</span>}
        <RunDot run={data.run} />
      </div>
      {style !== "compact" && <>
        <div className="node-name">{data.name}</div>
        <div className="node-desc">{g.label}</div>
      </>}
      <Handle type="source" id="c" position={Position.Bottom} />
    </div>
  );
}

/** The system step after a run that updates the workspace context (not part of the flow). */
export function ContextNode({ data }: NodeProps<Node<{ run?: string; label: string; hint: string; badge: string }>>) {
  return (
    <div className={`node system${data.run ? ` run-${data.run}` : ""}`} title={data.hint}>
      <Handle type="target" id="c" position={Position.Top} />
      <div className="node-head">
        <span className="node-kind">⟳ {data.label}</span>
        <span className="badge">{data.badge}</span>
      </div>
      <div className="node-desc">{data.hint}</div>
    </div>
  );
}

export const nodeTypes = { agent: AgentNode, gate: GateNode, context: ContextNode };
