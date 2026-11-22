// Choose a folder on this machine without typing its path: the local server lists the subfolders
// (names only). Git repositories and folders that hold a studio workspace are marked.
import { useEffect, useState } from "react";
import { api, errorText, type FolderList } from "./api";
import { useI18n } from "./i18n";

export function FolderBrowser({ start, onPick, onClose }: {
  start: string; onPick: (path: string) => void; onClose: () => void;
}) {
  const { t } = useI18n();
  const [list, setList] = useState<FolderList | null>(null);
  const [typed, setTyped] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const go = (path: string) => {
    setErr(null);
    api.folders(path).then((r) => { setList(r); setTyped(r.path); })
      .catch((e) => setErr(errorText(e, t)));  // e.g. "restart the server" (an older server)
  };
  useEffect(() => { go(start); }, []); // eslint-disable-line react-hooks/exhaustive-deps -- once, at the start folder
  return (
    <div className="folder-browser" role="group" aria-label={t("fb.label")}>
      <div className="row">
        <button type="button" className="small" disabled={!list?.parent} title={t("fb.up")}
          onClick={() => list?.parent && go(list.parent)}>↑</button>
        <input className="mono" value={typed} aria-label={t("fb.path")} onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); go(typed); } }} />
        <button type="button" className="small" onClick={() => go(typed)}>{t("fb.go")}</button>
      </div>
      <div className="row fb-roots">
        <button type="button" className="small link" onClick={() => list && go(list.home)}>{t("fb.home")}</button>
        {list?.roots.map((r) => <button key={r} type="button" className="small link" onClick={() => go(r)}>{r}</button>)}
      </div>
      {err && <span className="field-err">{err}</span>}
      <ul className="fb-list" role="listbox" aria-label={t("fb.folders")}>
        {list && list.folders.length === 0 && <li className="muted small">{t("fb.empty")}</li>}
        {list?.folders.map((f) => (
          <li key={f.path}>
            <button type="button" className="fb-item" onClick={() => go(f.path)} title={f.path}>
              📁 {f.name}
              {f.repo && <span className="badge">git</span>}
              {f.workspace && <span className="badge sub">{t("fb.workspace")}</span>}
            </button>
          </li>))}
      </ul>
      <div className="row">
        <span className="muted small grow">
          {list?.workspace ? t("fb.hasWorkspace") : list?.repo ? t("fb.isRepo") : ""}
        </span>
        <button type="button" className="small" onClick={onClose}>{t("dialog.cancel")}</button>
        <button type="button" className="small primary" disabled={!list} onClick={() => list && onPick(list.path)}>
          {t("fb.choose")}</button>
      </div>
    </div>
  );
}
