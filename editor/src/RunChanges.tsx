// What the open run changed in the git roots of its workspace since it started (read only): a
// list of files with their status and +/− counts, and the diff of the one you open. The server
// compares with a snapshot taken at the start, so earlier work of the person is not shown as the
// run's (a file they had changed too is marked).
import { useEffect, useState } from "react";
import { diffLines } from "./diffLines";
import { useI18n } from "./i18n";
import { useCopy } from "./RunParts";

export type ChangedFile = { path: string; status: "modified" | "added" | "deleted" | "renamed"; before: boolean;
  added: number | null; removed: number | null; binary: boolean; diff: string; cut: boolean };
export type Changes = { available: boolean; roots: { name: string; path: string; branch?: string; missing?: boolean; files: ChangedFile[] }[] };

const MARK: Record<ChangedFile["status"], string> = { modified: "M", added: "A", deleted: "D", renamed: "R" };

/** The changes of a run, loaded again when `key` changes (the run's state, a refresh). */
export function useChanges(input: string | undefined, key: unknown) {
  const [data, setData] = useState<Changes | null>(null);
  useEffect(() => {
    if (!input) { setData(null); return; }
    let live = true;
    fetch(`/api/runs/${encodeURIComponent(input)}/changes`).then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (live) setData(d); }).catch(() => { if (live) setData(null); });
    return () => { live = false; };
  }, [input, key]);
  return data;
}

export const changedCount = (c: Changes | null) => (c ? c.roots.reduce((k, r) => k + r.files.length, 0) : 0);

export function ChangesView({ data, onRefresh }: { data: Changes | null; onRefresh: () => void }) {
  const i18n = useI18n();
  const { t } = i18n;
  const [open, setOpen] = useState<string | null>(null);
  const [copied, copy] = useCopy();
  if (!data) return <p className="muted small">{t("rp.chLoading")}</p>;
  if (!data.available) return <p className="muted small">{t("rp.chNone")}</p>;
  const many = data.roots.length > 1;
  return (
    <div className="rp-changes">
      <div className="row small muted"><span className="grow">{i18n.tn("rp.chFiles", changedCount(data))}</span>
        <button className="link small" onClick={onRefresh}>{t("rp.chRefresh")}</button></div>
      {data.roots.map((r) => (
        <section key={r.name}>
          {many && <h4 className="rp-ch-root small mono" title={r.path}>{r.name}{r.branch ? ` · ${r.branch}` : ""}</h4>}
          {r.missing && <p className="field-err small">{t("rp.chMissing", { path: r.path })}</p>}
          {!r.missing && r.files.length === 0 && <p className="muted small">{t("rp.chClean")}</p>}
          <ul className="rp-ch-files">
            {r.files.map((f) => {
              const key = `${r.name}/${f.path}`;
              const on = open === key;
              return (
                <li key={key} className={`ch-${f.status}`}>
                  <button className="rp-ch-file" aria-expanded={on} onClick={() => setOpen(on ? null : key)}>
                    <span className="rp-ch-mark" title={t(`rp.ch.${f.status}`)}>{MARK[f.status]}</span>
                    <span className="mono grow rp-ch-path" title={f.path}>{f.path}</span>
                    {f.before && <span className="badge warn" title={t("rp.chBeforeTitle")}>{t("rp.chBefore")}</span>}
                    {f.binary ? <span className="muted small">{t("rp.chBinary")}</span>
                      : <span className="small"><span className="ch-add">+{f.added ?? 0}</span> <span className="ch-del">−{f.removed ?? 0}</span></span>}
                  </button>
                  {on && (
                    <div className="rp-ch-diff">
                      {f.diff ? <>
                        <div className="row small"><span className="grow" />
                          <button className="link small" onClick={() => copy(f.diff, key)}>{t(copied === key ? "run.outCopied" : "rp.chCopy")}</button></div>
                        <table className="rp-diff mono small"><tbody>
                          {diffLines(f.diff).filter((l) => l.kind !== "meta").map((l, n) => (
                            <tr key={n} className={`d-${l.kind}`}>
                              <td className="ln">{l.kind === "hunk" ? "" : l.old ?? ""}</td>
                              <td className="ln">{l.kind === "hunk" ? "" : l.new ?? ""}</td>
                              <td className="tx">{l.kind === "add" ? "+" : l.kind === "del" ? "−" : l.kind === "hunk" ? "" : " "}{l.text}</td>
                            </tr>))}
                        </tbody></table>
                        {f.cut && <p className="muted small">{t("rp.chCut")}</p>}
                      </> : <p className="muted small">{t(f.binary ? "rp.chBinaryNote" : f.status === "deleted" ? "rp.chDeleted" : "rp.chNoDiff")}</p>}
                    </div>)}
                </li>);
            })}
          </ul>
        </section>))}
      <p className="muted small">{t("rp.chNote")}</p>
    </div>);
}
