import { type ReactNode } from "react";
import { useI18n } from "./i18n";
import { MoreMenu } from "./RunParts";
import { AgentPanel } from "./inspectorPanels/AgentPanel";
import { FlowPanel } from "./inspectorPanels/FlowPanel";
import { EdgePanel, GatePanel } from "./inspectorPanels/GatePanel";
import { type Props } from "./inspectorPanels/shared";

export function Inspector(p: Props) {
  const { t } = useI18n();
  const { doc, sel } = p;
  let title = t("insp.flow"), body: ReactNode = <FlowPanel doc={doc} catalog={p.catalog} update={p.update} openProviders={p.openProviders} />;
  if (sel?.kind === "node" && sel.id in doc.spec.agents) {
    title = t("insp.agent");
    body = <AgentPanel key={sel.id} id={sel.id} {...p} />;
  } else if (sel?.kind === "data" && sel.agent in doc.spec.agents) {
    const from = doc.spec.agents[sel.agent].inputs?.[sel.input]?.from;
    title = t("insp.dataLink");
    body = <>
      <p className="edge-title"><code>{from ?? "—"}</code> → <code>{sel.agent}.{sel.input}</code></p>
      <AgentPanel key={sel.agent} id={sel.agent} focus={sel.input} {...p} />
    </>;
  } else if (sel?.kind === "node" && sel.id in (doc.spec.gates ?? {})) {
    title = t("insp.gate");
    body = <GatePanel key={sel.id} id={sel.id} {...p} />;
  } else if (sel?.kind === "edge" && doc.spec.edges?.[sel.index]) {
    title = t("insp.edge");
    body = <EdgePanel index={sel.index} doc={doc} val={p.val} update={p.update} />;
  }
  return (
    <aside className="inspector">
      <div className="panel-head">
        <h2>{title}</h2>
        {sel && <>
          <button className="small" onClick={() => p.select(null)} title={t("insp.backToFlow")}>← {t("insp.flow")}</button>
          <MoreMenu label={t("insp.more")} items={[{ label: `${t("insp.delete")}…`, danger: true,
            disabled: sel.kind === "node" && sel.id === doc.spec.start,
            title: sel.kind === "node" && sel.id === doc.spec.start ? t("insp.changeStartFirst")
              : sel.kind === "data" ? t("insp.deleteLinkTitle") : t("insp.deleteTitle"),
            onSelect: () => p.remove(sel) }]} />
        </>}
      </div>
      <div className="panel-body">{body}</div>
      <datalist id="models">{p.catalog.models.map((m) => {
        const pv = m.includes("/") ? (p.catalog.providers ?? []).find((x) => x.name === m.split("/", 1)[0]) : undefined;
        return <option key={m} value={m} label={pv ? `${pv.title ?? pv.name} · ${pv.kind}` : undefined} />;
      })}</datalist>
      <datalist id="afs-flows">{(p.flows ?? []).filter((f) => f !== doc.spec.name).map((f) => <option key={f} value={f} />)}</datalist>
    </aside>
  );
}
