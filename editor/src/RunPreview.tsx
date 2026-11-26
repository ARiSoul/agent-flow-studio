// The workspace's web app (a game, a site) as the person runs it on this machine: its preview URL
// (Board → workspace settings → Preview) in a frame of the Run panel, next to the steps, with
// Reload and "Open in a tab". The studio never starts it: the setting says how ("npm run dev").
import { useEffect, useState } from "react";
import { api, currentWorkspaceName } from "./api";
import { useI18n } from "./i18n";

export type PreviewSpec = { url: string; start?: string };

/** The current workspace's preview setting (null: none, or the studio's own workspace). */
export function usePreview(): PreviewSpec | null {
  const [p, setP] = useState<PreviewSpec | null>(null);
  useEffect(() => {
    const name = currentWorkspaceName();
    if (!name || name === "studio") return;
    api.workspace(name).then((w) => setP(w.spec?.preview?.url ? w.spec.preview : null)).catch(() => setP(null));
  }, []);
  return p;
}

export function PreviewView({ preview }: { preview: PreviewSpec }) {
  const { t } = useI18n();
  const [nonce, setNonce] = useState(0);
  const [up, setUp] = useState<boolean | null>(null);
  useEffect(() => {  // a quick look: does the dev server answer? (no-cors: any answer counts)
    let live = true;
    fetch(preview.url, { mode: "no-cors" }).then(() => live && setUp(true)).catch(() => live && setUp(false));
    return () => { live = false; };
  }, [preview.url, nonce]);
  return (
    <div className="rp-preview">
      <div className="row small">
        <code className="grow rp-preview-url" title={preview.url}>{preview.url}</code>
        <button className="small" onClick={() => setNonce((n) => n + 1)}>{t("rp.pvReload")}</button>
        <a className="small" href={preview.url} target="_blank" rel="noreferrer">{t("rp.pvOpen")}</a>
      </div>
      {up === false ? (
        <p className="field-err small">{t("rp.pvDown")}{preview.start ? <> <code>{preview.start}</code></> : null}</p>
      ) : (
        <iframe key={nonce} className="rp-preview-frame" src={preview.url} title={t("rp.preview")}
          sandbox="allow-scripts allow-same-origin allow-pointer-lock allow-forms" />)}
      <p className="muted small">{t("rp.pvNote")}</p>
    </div>);
}
