// The one shell of the studio's windows (Connectors, Agent library, Compare): a labelled modal
// dialog that keeps the focus inside, focuses its first field on open and gives it back on
// close. With `dirty`, Escape, a backdrop click and × ask before unsaved changes are lost
// (`useModalGuard` does the same for switching the item in a list). `live` is announced.
import { useCallback, useEffect, useId, useRef, type ReactNode } from "react";
import { useDialog, trapFocus } from "./Dialog";
import { useI18n } from "./i18n";

/** Asks before unsaved changes are lost: resolves true when it may go on. */
export function useModalGuard(dirty: boolean): () => Promise<boolean> {
  const dialog = useDialog();
  const { t } = useI18n();
  return useCallback(async () => !dirty || dialog.confirm({
    title: t("modal.discardTitle"), message: t("modal.discardMessage"), ok: t("modal.discard"), danger: true,
  }), [dirty, dialog, t]);
}

export function Modal({ title, meta, metaTitle, onClose, dirty = false, wide, className, head, foot, live, children }: {
  title: string; meta?: ReactNode; metaTitle?: string; onClose: () => void; dirty?: boolean; wide?: boolean;
  className?: string; head?: ReactNode; foot?: ReactNode; live?: string; children: ReactNode;
}) {
  const { t } = useI18n();
  const id = useId();
  const box = useRef<HTMLDivElement>(null);
  const guard = useModalGuard(dirty);
  const close = async () => { if (await guard()) onClose(); };
  // Escape on the whole page (the focus can leave the window, e.g. after Save turns off); a
  // dialog on top (useDialog) stops its own Escape first
  const latest = useRef(close);
  latest.current = close;
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented || document.querySelector(".dialog-layer, .ctx-menu")) return;
      e.preventDefault();
      latest.current();
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, []);

  useEffect(() => {
    const back = document.activeElement;
    const first = box.current?.querySelector<HTMLElement>(
      ".modal-body input:not([type=hidden]), .modal-body select, .modal-body textarea, .modal-body button, button.primary");
    (first ?? box.current)?.focus();
    return () => { if (back instanceof HTMLElement && back.isConnected) back.focus(); };
  }, []);

  return (
    <div className="modal" onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div ref={box} className={`modal-box modal-shell${wide ? " wide" : ""}${className ? ` ${className}` : ""}`}
        role="dialog" aria-modal="true" aria-labelledby={`${id}-title`} tabIndex={-1}
        onKeyDown={(e) => {
          if (e.key === "Tab") trapFocus(e, box.current);
        }}>
        <header className="modal-head">
          <div className="modal-titles">
            <h2 id={`${id}-title`} className="modal-title">{title}</h2>
            {dirty && <span className="tag warn modal-dirty">● {t("modal.unsaved")}</span>}
            {meta && <span className="modal-meta" title={metaTitle}>{meta}</span>}
          </div>
          {head}
          <button className="icon modal-x" aria-label={t("modal.close")} title={t("modal.close")} onClick={close}>×</button>
        </header>
        <div className="modal-body">{children}</div>
        {foot && <footer className="modal-foot">{foot}</footer>}
        <div className="sr-only" role="status" aria-live="polite">{live ?? ""}</div>
      </div>
    </div>
  );
}
