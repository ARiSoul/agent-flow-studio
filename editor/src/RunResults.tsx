// The results of a run: the final result shown in full (the outputs of the step that saved last,
// once the run is done), every output as one dense row (open, copy, download), the files of the
// run and its folder. Available while the run goes on.
import { useEffect, useRef } from "react";
import { useI18n } from "./i18n";
import { Markdown } from "./Markdown";
import { Clamp, download, MoreMenu, oneLine, pretty, useCopy } from "./RunParts";
import { kb } from "./runSteps";

export type Shown = { title: string; value: unknown; md: boolean };

export function Results({ outputs, lastOut, done, typeOf, usedBy, arts, runDir, highlight, onShow, onFile, onFolder, onStep }: {
  outputs: [string, Record<string, unknown>][]; lastOut?: string; done: boolean;
  typeOf: (agent: string, name: string) => string;
  /** The steps that read an output ("writer.code"), from the flow's inputs. */
  usedBy: (agent: string, name: string) => string[];
  arts: { name: string; size: number }[]; runDir: string;
  /** An output to bring into view (a link from a step): its nonce changes each time. */
  highlight: { agent: string; name: string; nonce: number } | null;
  onShow: (s: Shown) => void; onFile: (name: string) => void; onFolder: () => void; onStep: (node: string) => void;
}) {
  const { t } = useI18n();
  const [copied, copy] = useCopy();
  const refs = useRef<Record<string, HTMLLIElement | null>>({});
  useEffect(() => {
    if (!highlight) return;
    const el = refs.current[`${highlight.agent}.${highlight.name}`];
    el?.scrollIntoView({ block: "center", behavior: "smooth" });
    el?.classList.add("flash");
    const h = setTimeout(() => el?.classList.remove("flash"), 1400);
    return () => clearTimeout(h);
  }, [highlight]);
  const md = (agent: string, n: string) => ["markdown", "text"].includes(typeOf(agent, n));
  const final = done && lastOut ? outputs.find(([a]) => a === lastOut) : undefined;
  const finalPick = final ? Object.entries(final[1]).find(([n]) => md(final[0], n)) ?? Object.entries(final[1])[0] : undefined;
  const total = outputs.reduce((k, [, o]) => k + Object.keys(o).length, 0);
  const save = (key: string, v: unknown, isMd: boolean) =>
    download(`${key}${isMd ? ".md" : typeof v === "string" ? ".txt" : ".json"}`, pretty(v), isMd ? "text/markdown" : "application/json");
  if (!total && !arts.length) return null;
  return (
    <section className="rp-results" aria-label={t("rp.results")}>
      <h3>{t("rp.results")}</h3>
      {final && finalPick && (
        <div className="rp-final-box">
          <div className="row small">
            <span className="badge st-done rp-final">{t("rp.final")}</span>
            <button className="link small mono rp-node" onClick={() => onStep(final[0])}>{final[0]}.{finalPick[0]}</button>
            <span className="grow" />
            <button className="small" onClick={() => onShow({ title: `${final[0]}.${finalPick[0]}`, value: finalPick[1], md: md(final[0], finalPick[0]) })}>{t("run.outOpen")}</button>
            <button className="small" onClick={() => copy(pretty(finalPick[1]), "final")}>{t(copied === "final" ? "run.outCopied" : "run.outCopy")}</button>
          </div>
          <Clamp lines={8}>{typeof finalPick[1] === "string" && md(final[0], finalPick[0])
            ? <Markdown text={finalPick[1]} /> : <pre className="mono small">{pretty(finalPick[1])}</pre>}</Clamp>
        </div>)}
      {total > 0 && (
        <details open={!final}>
          <summary>{t("rp.outputs", { n: String(total) })}</summary>
          <ul className="rp-outs">
            {outputs.flatMap(([agent, o]) => Object.entries(o).map(([n, v]) => {
              const key = `${agent}.${n}`, isMd = md(agent, n);
              return (
                <li key={key} ref={(el) => { refs.current[key] = el; }} className="rp-out">
                  <button className="rp-out-main" onClick={() => onShow({ title: key, value: v, md: isMd })} title={t("run.outOpen")}>
                    <span className="mono"><b>{n}</b> <span className="muted">· {agent}</span></span>
                    <span className="port-tag">{typeOf(agent, n)}</span>
                    <span className="rp-preview-line muted small">{oneLine(v, 140)}</span>
                    {usedBy(agent, n).length > 0 && <span className="rp-used small muted">→ {usedBy(agent, n).join(", ")}</span>}
                  </button>
                  <MoreMenu label={t("rp.outputActions", { name: key })} items={[
                    { label: t("run.outOpen"), onSelect: () => onShow({ title: key, value: v, md: isMd }) },
                    { label: t(copied === key ? "run.outCopied" : "run.outCopy"), onSelect: () => copy(pretty(v), key) },
                    { label: t("run.outDownload"), onSelect: () => save(key, v, isMd) },
                    { label: t("rp.showStep"), onSelect: () => onStep(agent) },
                  ]} />
                </li>);
            }))}
          </ul>
        </details>)}
      {arts.length > 0 && (
        <details>
          <summary>{t("rp.files", { n: String(arts.length) })}</summary>
          <ul className="rp-files">{arts.map((a) => (
            <li key={a.name}><button className="link small mono" onClick={() => onFile(a.name)}>{a.name}</button>
              <span className="muted small">{kb(a.size)}</span></li>))}</ul>
        </details>)}
      <div className="row small muted rp-folder"><code title={runDir}>{runDir}</code><span className="grow" />
        <button className="small" title={t("run.openFolderTitle")} onClick={onFolder}>{t("run.openFolder")}</button></div>
    </section>);
}
