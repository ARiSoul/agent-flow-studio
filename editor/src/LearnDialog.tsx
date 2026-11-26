// Learn a root's history from past Claude Code sessions and memory notes: the person picks the
// sources (the studio's own sessions are set apart), sees what it costs, and starts the run. The
// result is a draft the context panel shows for review; nothing is saved by the run.
import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { useI18n } from "./i18n";
import type { ContextSources, MemorySource, SessionSource } from "./types";
import { wsErrorText } from "./Workspaces";

function size(n: number | null): string {
  if (!n) return "";
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} kB`;
}

export function LearnDialog({ root, onClose, onStart }: {
  root: string;
  onClose: () => void;
  onStart: (sessions: string[], memory: string[]) => void;
}) {
  const { t, lang } = useI18n();
  const [src, setSrc] = useState<ContextSources | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [notes, setNotes] = useState<Set<string>>(new Set());
  const [est, setEst] = useState<{ chars: number; tokens: number } | null>(null);
  const [estimating, setEstimating] = useState(false);

  useEffect(() => {
    api.contextSources(root).then((s) => {
      setSrc(s);
      setPicked(new Set(s.sessions.filter((x) => x.kind === "person" && x.state !== "learned").map((x) => x.id)));
      setNotes(new Set(s.memory.filter((x) => x.state !== "learned").map((x) => x.file)));
    }).catch((e) => setError(wsErrorText(e, lang)));
  }, [root, lang]);

  // the estimate follows the choice (each session is read once; the server keeps it)
  useEffect(() => {
    if (!src || (!picked.size && !notes.size)) { setEst(null); return; }
    setEstimating(true);
    const h = setTimeout(() => {
      api.contextEstimate(root, [...picked], [...notes]).then(setEst)
        .catch(() => setEst(null)).finally(() => setEstimating(false));
    }, 400);
    return () => clearTimeout(h);
  }, [root, src, picked, notes]);

  const [mine, others] = useMemo(() => {
    const all = src?.sessions ?? [];
    return [all.filter((s) => s.kind === "person"), all.filter((s) => s.kind !== "person")];
  }, [src]);
  const when = (s: number | null) => (s ? new Date(s * 1000).toLocaleDateString(lang) : "");
  const toggle = (set: Set<string>, put: (s: Set<string>) => void, key: string) => {
    const n = new Set(set);
    if (n.has(key)) n.delete(key); else n.add(key);
    put(n);
  };
  const state = (s: SessionSource | MemorySource) => (
    <span className={`badge learn-${s.state}`}>{t(`learn.state.${s.state}`)}</span>);
  const sessionRow = (s: SessionSource) => (
    <li key={s.id}>
      <label className="learn-row">
        <input type="checkbox" checked={picked.has(s.id)} onChange={() => toggle(picked, setPicked, s.id)} />
        <span className="grow">
          <b>{s.title || s.first_prompt || s.id}</b>
          {s.title && s.first_prompt && s.first_prompt !== s.title &&
            <span className="muted small learn-prompt">{s.first_prompt}</span>}
        </span>
        {s.kind !== "person" && <span className="badge">{t(`learn.kind.${s.kind}`)}</span>}
        {state(s)}
        <span className="muted small">{[s.branch, when(s.modified), size(s.size)].filter(Boolean).join(" · ")}</span>
      </label>
    </li>);
  const nothing = src && !src.sessions.length && !src.memory.length;

  return (
    <div className="modal" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal-box cmd-manager learn-dialog" role="dialog" aria-modal="true" aria-labelledby="learn-title">
        <div className="panel-head">
          <h2 id="learn-title">{t("learn.title", { root })}</h2>
          <span className="grow" />
          <button className="small" onClick={onClose}>{t("dialog.close")}</button>
        </div>
        <div className="learn-body">
          <p className="muted small">{t("learn.hint")}</p>
          <p className="small learn-privacy">{t("learn.privacy")}</p>
          {error && <p className="field-err">{error}</p>}
          {!src && !error && <p className="muted">{t("learn.loading")}</p>}
          {nothing && <p className="muted">{t("learn.none", { folder: src.folder })}</p>}
          {src && src.memory.length > 0 && (<>
            <h3>{t("learn.memory")}</h3>
            <ul className="learn-list">
              {src.memory.map((m) => (
                <li key={m.file}>
                  <label className="learn-row">
                    <input type="checkbox" checked={notes.has(m.file)} onChange={() => toggle(notes, setNotes, m.file)} />
                    <span className="grow"><b>{m.name}</b>
                      {m.description && <span className="muted small learn-prompt">{m.description}</span>}</span>
                    {state(m)}
                    <span className="muted small">{when(m.modified)}</span>
                  </label>
                </li>))}
            </ul>
          </>)}
          {mine.length > 0 && (<>
            <h3>{t("learn.sessions")}</h3>
            <ul className="learn-list">{mine.map(sessionRow)}</ul>
          </>)}
          {others.length > 0 && (
            <details className="learn-others">
              <summary className="small">{t("learn.others", { n: String(others.length) })}</summary>
              <ul className="learn-list">{others.map(sessionRow)}</ul>
            </details>)}
        </div>
        <div className="row learn-foot">
          <span className="muted small grow">
            {picked.size + notes.size === 0 ? t("learn.pickSome")
              : estimating ? t("learn.estimating")
              : est ? t("learn.estimate", { tokens: est.tokens.toLocaleString(lang), chars: est.chars.toLocaleString(lang),
                n: String(picked.size + (notes.size ? 1 : 0)) }) : ""}
          </span>
          <button className="primary" disabled={!picked.size && !notes.size}
            onClick={() => onStart([...picked], [...notes])}>{t("learn.start")}</button>
        </div>
      </div>
    </div>
  );
}
