// Cards for what the workspace assistant proposes (workspace settings, a new flow, a connector).
// The assistant never saves: the person applies each card here, through the editor's own API.
import { useState } from "react";
import { api, errorText } from "./api";
import { useDialog } from "./Dialog";
import { useI18n } from "./i18n";
import type { FlowRaw } from "./types";

export type WsProposal =
  | { id: string; kind: "workspace"; name: string; changes: Record<string, unknown>; before: Record<string, unknown> }
  | { id: string; kind: "flow"; name: string; title: string; description: string; agents: string[];
      template?: string | null; spec: FlowRaw; prompts: Record<string, string> }
  | { id: string; kind: "connector"; name: string; exists: boolean; connector: Record<string, unknown>;
      secret_field: string | null; sign_in: boolean };

const json = (x: unknown) => JSON.stringify(x, null, 2);

export function ProposalCard({ p, onApplied, onOpenFlow, onConnectors }: {
  p: WsProposal; onApplied: (p: WsProposal) => void; onOpenFlow: (name: string) => void;
  onConnectors: () => void;
}) {
  const { t } = useI18n();
  const dialog = useDialog();
  const [state, setState] = useState<{ done?: boolean; error?: string; busy?: boolean }>({});
  const apply = async () => {
    if (p.kind === "connector" && p.exists && !(await dialog.confirm({
      title: t("wsa.replaceTitle", { name: p.name }), message: t("wsa.replaceConnector"),
      ok: t("wsa.replaceOk"), danger: true }))) return;
    setState({ busy: true });
    try {
      if (p.kind === "workspace") await api.updateWorkspace(p.name, p.changes);
      else if (p.kind === "flow") {
        await api.create(p.name, p.title, undefined, "workspace");
        const r = await api.save(p.name, { spec: p.spec, prompts: p.prompts });
        if (r.saved === false) throw new Error(t("wsa.flowNotSaved"));
      } else {
        await api.saveConnection(p.name, p.exists ? p.connector : { ...p.connector, location: "workspace" });
      }
      setState({ done: true });
      onApplied(p);
    } catch (e) { setState({ error: errorText(e, t) }); }
  };
  const c = p.kind === "connector" ? p.connector : null;
  const actions = c && typeof c.actions === "object" && c.actions ? Object.keys(c.actions) : [];
  return (
    <div className="chat-lib">
      <div className="chat-lib-head">
        <b>{t(`wsa.kind.${p.kind}`)}</b> <code>{p.name}</code>
        {p.kind === "connector" && <span className={`badge${p.exists ? " warn" : ""}`}>{t(p.exists ? "wsa.replaces" : "wsa.new")}</span>}
      </div>
      {p.kind === "workspace" && <>
        <p>{t("wsa.changes", { list: Object.keys(p.changes).join(", ") })}</p>
        <details><summary>{t("wsa.details")}</summary>
          <pre className="mono">{json(p.changes)}</pre>
          <p className="muted small">{t("wsa.before")}</p>
          <pre className="mono">{json(p.before)}</pre>
        </details>
      </>}
      {p.kind === "flow" && <>
        <p><b>{p.title}</b>{p.description ? ` — ${p.description}` : ""}</p>
        <dl>
          <dt>{t("wsa.agents")}</dt><dd>{p.agents.join(", ") || "—"}</dd>
          {p.template && <><dt>{t("wsa.template")}</dt><dd>{p.template}</dd></>}
        </dl>
        <details><summary>{t("wsa.details")}</summary>
          <pre className="mono">{json(p.spec)}</pre>
          {Object.entries(p.prompts).map(([f, text]) => (
            <details key={f}><summary><code>{f}</code></summary><pre className="mono">{text}</pre></details>))}
        </details>
      </>}
      {p.kind === "connector" && c && <>
        <p>{String(c.description ?? "")}</p>
        <dl>
          <dt>{t("wsa.type")}</dt><dd>{String(c.type)}</dd>
          <dt>{t("wsa.actions")}</dt><dd>{actions.join(", ") || "—"}</dd>
        </dl>
        {(p.secret_field || p.sign_in) && <p className="muted small">{t(p.sign_in ? "wsa.needsSignIn" : "wsa.needsSecret")}</p>}
        <details><summary>{t("wsa.details")}</summary><pre className="mono">{json(c)}</pre></details>
      </>}
      <div className="row">
        {state.done
          ? <>
            <span className="ok-text">{t("wsa.applied")}</span>
            {p.kind === "flow" && <button className="small" onClick={() => onOpenFlow(p.name)}>{t("wsa.openFlow")}</button>}
            {p.kind === "connector" && <button className="small" onClick={onConnectors}>{t("app.connectors")}</button>}
          </>
          : <button className="small primary" disabled={state.busy} onClick={apply}>{t(`wsa.apply.${p.kind}`)}</button>}
        {state.error && <span className="field-err">{state.error}</span>}
      </div>
    </div>
  );
}
