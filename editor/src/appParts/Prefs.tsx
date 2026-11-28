/** The ⚙ Preferences popover of the top bar: language, theme and node style (browser
 * preferences, rarely changed, so they live out of the work bar). */
import { useEffect, useRef, useState } from "react";
import { Icon } from "../icons";
import { LANGS, useI18n, type Lang } from "../i18n";
import { NODE_STYLES, type NodeStyle } from "../model";
import { THEMES, type Theme } from "../theme";

type PanelKey = "pal" | "bottom" | "insp";

export function Prefs({ theme, setTheme, nodeStyle, setNodeStyle, panels, togglePanel }: {
  theme: Theme; setTheme: (t: Theme) => void; nodeStyle: NodeStyle; setNodeStyle: (s: NodeStyle) => void;
  /** Which panels are hidden (null: no panels here, e.g. the Board), and showing/hiding one. */
  panels?: Record<PanelKey, boolean> | null; togglePanel?: (p: PanelKey) => void;
}) {
  const { t, lang, setLang } = useI18n();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node) && e.target !== btn.current) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); btn.current?.focus(); } };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    box.current?.querySelector<HTMLElement>("select")?.focus();
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open]);
  return (
    <span className="prefs">
      <button ref={btn} className={`prefs-btn${open ? " on" : ""}`} aria-label={t("app.prefs")} title={t("app.prefsTitle")}
        aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(!open)}><Icon id="gear" size={16} />
        <span className="prefs-text" aria-hidden="true">{t("app.prefs")}</span></button>
      {open && (
        <div ref={box} className="prefs-pop" role="dialog" aria-label={t("app.prefs")}>
          <label>{t("app.language")}
            <select value={lang} aria-label={t("app.language")} onChange={(e) => setLang(e.target.value as Lang)}>
              {Object.entries(LANGS).map(([k, l]) => <option key={k} value={k}>{l.label}</option>)}
            </select></label>
          <label>{t("app.theme")}
            <select value={theme} aria-label={t("app.theme")} onChange={(e) => setTheme(e.target.value as Theme)}>
              {(Object.keys(THEMES) as Theme[]).map((k) => <option key={k} value={k}>{t(`theme.${k}`)}</option>)}
            </select></label>
          <label>{t("app.nodeStyle")}
            <select value={nodeStyle} aria-label={t("app.nodeStyle")} onChange={(e) => setNodeStyle(e.target.value as NodeStyle)}>
              {NODE_STYLES.map((k) => <option key={k} value={k}>{t(`app.nodeStyle.${k}`)}</option>)}
            </select></label>
          {panels && togglePanel && <fieldset className="prefs-panels"><legend>{t("app.panels")}</legend>
            {([["pal", "Ctrl+B"], ["bottom", "Ctrl+J"], ["insp", "Ctrl+Alt+B"]] as const).map(([p, keys]) => (
              <label key={p} className="check"><input type="checkbox" checked={!panels[p]} onChange={() => togglePanel(p)} />
                <span>{t(`app.panel.${p}`)}</span><kbd>{keys}</kbd></label>))}
          </fieldset>}
        </div>)}
    </span>
  );
}
