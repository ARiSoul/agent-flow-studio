/** The module-level parts of the editor (App.tsx): palette, storage keys, data link ids, the
 * location badge and field, and the context type its parts share. */
import { currentWorkspaceName } from "../api";
import { locLabel } from "../LibraryManager";
import { useI18n } from "../i18n";
import type { AgentRaw, GateRaw } from "../types";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { useReactFlow } from "@xyflow/react";
import type { useDialog } from "../Dialog";
import type { T } from "../i18n";
import type { MenuState } from "../ContextMenu";
import type { RunRequest, Trace } from "../RunPanel";
import type { NodeStyle } from "../model";
import type { Catalog, Doc, FlowSummary, FlowTemplate, LibraryAgent, Selection, Validation } from "../types";

/** A copied node (in memory, for paste and duplicate). */
export type Clip = { base: string; agent?: AgentRaw; gate?: GateRaw; prompt?: string };

export const PALETTE = [
  { kind: "agent", label: "palette.agent", hint: "palette.agentHint", base: "agent" },
  { kind: "human", label: "palette.human", hint: "palette.humanHint", base: "approval" },
  { kind: "stop", label: "palette.stop", hint: "palette.stopHint", base: "stop" },
  { kind: "decision", label: "palette.decision", hint: "palette.decisionHint", base: "decision" },
  { kind: "action", label: "palette.action", hint: "palette.actionHint", base: "action" },
  { kind: "subflow", label: "palette.subflow", hint: "palette.subflowHint", base: "subflow" },
] as const;
export type PaletteKind = (typeof PALETTE)[number]["kind"];
export const FLOW_NAME = /^[a-z0-9][a-z0-9_-]{0,39}$/;  // same rule as the server
export const DATA_LINKS_KEY = "afs.dataLinks";
// the open studio workspace (cookie afs_ws): the last open flow is kept per workspace
export const WS_NOW = currentWorkspaceName();
export const LAST_FLOW_KEY = WS_NOW === "studio" ? "afs.flow" : `afs.flow.${WS_NOW}`;
export const VIEW_KEY = "afs.view";
export const HIDDEN_KEY = "afs.hiddenPanels";
export type Panel = "pal" | "insp" | "bottom";

/** Id of a data link on the canvas: d:<agent>:<input> (agent and input names are slugs, no ":"). */
export const dataEdgeId = (agent: string, input: string) => `d:${agent}:${input}`;
export const parseDataEdge = (id: string) => { const [, agent, input] = id.split(":"); return { agent, input }; };

/** Where a flow lives: on this machine only, in an extension, or public (git would publish it). */
export function LocationBadge({ loc, isPublic }: { loc?: string; isPublic?: boolean }) {
  const { t } = useI18n();
  if (isPublic) return <span className="badge warn loc" title={t("loc.publicTitle")}>{t("loc.public")}</span>;
  if (loc === "workspace") return <span className="badge loc" title={t("loc.workspaceTitle")}>{t("loc.workspace")}</span>;
  if (loc && loc !== "local") return <span className="badge loc" title={t("loc.extensionTitle", { name: loc })}>{t("loc.extension", { name: loc })}</span>;
  return <span className="badge loc" title={t("loc.localTitle")}>{t("loc.local")}</span>;
}

/** The "Where" field of the create / save-to-library forms (only when there are extensions). */
export function locationField(locations: string[] | undefined, t: ReturnType<typeof useI18n>["t"], value = "local") {
  const locs = locations ?? ["local"];
  if (locs.length < 2) return [];
  return [{ key: "location", label: t("loc.where"), value, hint: t("loc.whereHint"),
    options: locs.map((l) => ({ value: l, label: locLabel(t, l) })) }];
}

/** What the Editor shares with its parts (appParts/*): its state and some of its actions. */
export interface EditorCtx {
  t: T;
  dialog: ReturnType<typeof useDialog>;
  rf: ReturnType<typeof useReactFlow>;
  nodeStyle: NodeStyle;
  catalog: Catalog | null;
  flows: FlowSummary[];
  templates: FlowTemplate[];
  library: LibraryAgent[];
  setLibrary: Dispatch<SetStateAction<LibraryAgent[]>>;
  current: string | null;
  setCurrent: Dispatch<SetStateAction<string | null>>;
  doc: Doc | null;
  setDoc: Dispatch<SetStateAction<Doc | null>>;
  sel: Selection;
  setSel: Dispatch<SetStateAction<Selection>>;
  val: Validation | null;
  setView: (v: "flow" | "board") => void;
  setRight: (tab: "props" | "chat" | "run") => void;
  trace: Trace;
  dims: Record<string, { width: number; height: number }>;
  showData: boolean;
  setRunReq: Dispatch<SetStateAction<RunRequest | null>>;
  menu: MenuState | null;
  setMenu: Dispatch<SetStateAction<MenuState | null>>;
  clip: MutableRefObject<Clip | null>;
  kbMenu: MutableRefObject<number>;
  dirty: boolean;
  flash: (text: string, bad?: boolean) => void;
  copyAgent: (id: string, how?: "copy" | "move") => void;
  refreshFlows: () => Promise<void>;
  update: (fn: (d: Doc) => void, record?: boolean) => void;
  remove: (s: Selection) => void;
  addNode: (kind: PaletteKind, at?: { x: number; y: number }) => void;
}
