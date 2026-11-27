"""The workspace context: the context keeper after a run, explore/update, and learning a
root's history from past Claude Code sessions (drafts the person saves).
A mixin of `Runner` (editor/runner.py): it uses the runner's state through `self`."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from ... import claude_sessions, context, workspaces
from ...builder.compile import (
    compile_graph,
)
from ...builder.spec import FlowSpec
from ...config import settings
from ...engine import GraphEngine
from ...tools import io as io_tool
from .model import RUN_ID, Run, RunError, new_run_id


class ContextMixin:

    # --- the workspace context ----------------------------------------------------------------
    async def _after_run(self, run: Run, ev: dict[str, Any]) -> None:
        """A full run of a workspace flow that ended done: "always" updates the workspace
        context as a last system step of the run; "suggest" marks the end (a button)."""
        how = run.context_update
        if ev.get("status") != "done" or run.mode != "full" or how in (None, "never"):
            return
        if how == "suggest" or run.context_done:
            ev["context"] = "suggest" if how == "suggest" else "updated"
            return
        run.context_done = True
        ok = await self._context_steps(run, "update", None, self._changes(run))
        ev["context"] = "updated" if ok else "failed"

    def _changes(self, run: Run) -> str:
        """What a run did (its end, stop message and outputs), for the context update."""
        outs: dict[str, dict[str, Any]] = {}
        names = {e.get("agent") for e in run.events if e.get("type") == "outputs"}
        for a in sorted(n for n in names if isinstance(n, str)):
            try:
                outs[a] = io_tool.outputs(run.input, a)
            except ValueError:
                continue
        events = [*run.events, {"type": "end", "status": "done", "gate": run.gate}]
        return context.run_changes(events, outs)

    def update_context(self, mode: Literal["explore", "update"], roots: list[str] | None = None,
                       after: str | None = None) -> Run:
        """A run of the context keeper on roots of the open workspace (all by default): explore
        writes their sections from scratch, update brings them up to date (`after`: the run
        whose changes it should know about)."""
        if self.active():
            raise RunError(409, "a run is already in progress; wait or stop it")
        ws, env = workspaces.current(), workspaces.current_env()
        if ws.studio or ws.spec is None:
            raise RunError(400, "the studio's own workspace has no context: open a workspace")
        names = roots or list(ws.spec.roots)
        unknown = [r for r in names if r not in ws.spec.roots]
        if unknown or not names:
            raise RunError(400, f"unknown roots: {', '.join(unknown)}" if unknown else
                           "this workspace has no roots yet: add a folder first")
        changes = ""
        if after:
            prev = next((r for r in self.runs.values() if r.input == after), None)
            if prev is not None:
                changes = self._changes(prev)
        rid = new_run_id("context")
        run = Run(uuid.uuid4().hex[:12], context.KEEPER_FLOW, rid, self.flows_base(),
                  workspace=ws.name, environment=env)
        self._emit(run, {"type": "start", "flow": context.KEEPER_FLOW, "input": rid,
                         "mode": "full", "target": None, "mock": settings.mock,
                         "model": settings.model, "execution": "graph", "context": mode,
                         "roots": names, "workspace": ws.name, "environment": env})
        self._spawn(run, self._drive_context(run, mode, names, changes))
        self.runs[run.id] = run  # only a run that started counts as active
        return run

    async def _drive_context(self, run: Run, mode: str, roots: list[str], changes: str) -> None:
        try:
            ok = await self._context_steps(run, mode, roots, changes)
            run.status = "done" if ok else "error"
            self._emit(run, {"type": "end", "status": run.status, "gate": None})
        except asyncio.CancelledError:
            run.status = "stopped"
            self._emit(run, {"type": "end", "status": "stopped"})
        except Exception as e:  # noqa: BLE001 — report in the trace instead of losing the run
            run.status = "error"
            self._emit(run, {"type": "end", "status": "error",
                             "message": f"{type(e).__name__}: {e}"})

    async def _context_steps(self, run: Run, mode: str, roots: list[str] | None,
                             changes: str) -> bool:
        """One context keeper session per root (in sequence; read-only tools on that root); the
        studio stores each section. Events are tagged `sub: "context:<root>"`. True when all
        roots were updated."""
        ws = workspaces.current()
        assert ws.spec is not None
        names = roots or list(ws.spec.roots)
        folders = ws.resolve(workspaces.current_env())[1]
        secs, st = context.sections(ws), context.state(ws)
        all_ok = True
        for root in names:
            self._emit(run, {"type": "context", "root": root, "status": "running", "mode": mode})
            try:
                values = {"mode": mode, "root": root, "current": secs.get(root, "")
                          if mode == "update" else "",
                          "since": (st.get(root) or {}).get("commit") or "", "changes": changes}
                result = await self._child(run, f"{run.input[:24]}--{root[:14]}",
                                           context.keeper_flow(root), context.KEEPER_AGENT,
                                           values, f"context:{root}")
                section = str(result.get("section") or "")
                if not section.strip():
                    raise RuntimeError("the context keeper returned no section")
                context.set_section(ws, root, section, commit=context.head(Path(folders[root])),
                                    by=run.input)
                self._audit_context(ws.name, root, mode, run.input)
                self._emit(run, {"type": "context", "root": root, "status": "done", "mode": mode,
                                 "summary": str(result.get("summary") or "")[:500]})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — one root fails, the others go on
                all_ok = False
                self._emit(run, {"type": "context", "root": root, "status": "error",
                                 "mode": mode, "message": f"{type(e).__name__}: {e}"[:500]})
        return all_ok

    # --- learning from past Claude Code sessions ----------------------------------------------
    @staticmethod
    def _learn_dir() -> Path:
        """Machine-local state of the learning (drafts, what was learned) of this workspace."""
        return workspaces.scoped(settings.runs_dir) / ".context"

    def learned(self, root: str) -> dict[str, dict[str, float]]:
        """{"sessions": {id: last_modified}, "memory": {file: mtime}} already learned for a root."""
        try:
            data = json.loads((self._learn_dir() / "learned.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"sessions": {}, "memory": {}}
        got = data.get(root) if isinstance(data, dict) else None
        got = got if isinstance(got, dict) else {}
        return {"sessions": dict(got.get("sessions") or {}), "memory": dict(got.get("memory") or {})}

    def drafts(self) -> dict[str, dict[str, Any]]:
        out = {}
        for f in sorted(self._learn_dir().glob("draft-*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and isinstance(d.get("root"), str):
                out[d["root"]] = d
        return out

    def discard_draft(self, root: str) -> None:
        if RUN_ID.fullmatch(root):
            (self._learn_dir() / f"draft-{root}.json").unlink(missing_ok=True)

    def apply_draft(self, root: str, section: str) -> None:
        """Saves the reviewed history of a root and records its sources as learned."""
        ws = workspaces.current()
        draft = self.drafts().get(root)
        if draft is None:
            raise RunError(404, "there is no draft history for this root")
        context.set_section(ws, context.history_of(root), section, by=draft.get("run"))
        d = self._learn_dir()
        try:
            data = json.loads((d / "learned.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        mine = self.learned(root)
        mine["sessions"].update(draft.get("sessions") or {})
        mine["memory"].update(draft.get("memory") or {})
        data[root] = mine
        d.mkdir(parents=True, exist_ok=True)
        (d / "learned.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
        self.discard_draft(root)
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        with (settings.runs_dir / "audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "event": "workspace_context_learn",
                                "workspace": ws.name, "root": root, "run": draft.get("run"),
                                "sessions": len(draft.get("sessions") or {}),
                                "memory": len(draft.get("memory") or {})}) + "\n")

    def sources(self, root: str) -> dict[str, Any]:
        """The Claude Code sessions and memory notes about a root's folder."""
        folder = self._root_folder(root)
        seen = self.learned(root)
        return {"root": root, "folder": folder,
                "sessions": claude_sessions.list_sessions(folder, seen["sessions"]),
                "memory": claude_sessions.list_memory(folder, seen["memory"])}

    def _root_folder(self, root: str) -> str:
        ws = workspaces.current()
        if ws.studio or ws.spec is None:
            raise RunError(400, "the studio's own workspace has no context: open a workspace")
        if root not in ws.spec.roots:
            raise RunError(400, f"unknown root: {root}")
        return ws.resolve(workspaces.current_env())[1][root]

    def estimate(self, root: str, sessions: list[str], memory: list[str]) -> dict[str, int]:
        folder = self._root_folder(root)
        known = {s["id"]: s for s in claude_sessions.list_sessions(folder)}
        return claude_sessions.estimate(folder, [known[s] for s in sessions if s in known],
                                        memory)

    def learn_context(self, root: str, sessions: list[str], memory: list[str]) -> Run:
        """Reads the chosen past sessions and memory notes about a root (one reader each), then
        the history keeper writes the root's history, checked against the code. The result is a
        draft the person reviews (`apply_draft`); nothing is saved by the run."""
        if self.active():
            raise RunError(409, "a run is already in progress; wait or stop it")
        folder = self._root_folder(root)
        if not sessions and not memory:
            raise RunError(400, "choose at least one session or memory note")
        known = {s["id"]: s for s in claude_sessions.list_sessions(folder)}
        unknown = [s for s in sessions if s not in known]
        if unknown:
            raise RunError(400, f"unknown sessions: {', '.join(unknown[:3])}")
        notes_known = {m["file"]: m for m in claude_sessions.list_memory(folder)}
        memory = [m for m in memory if m in notes_known]
        ws, env = workspaces.current(), workspaces.current_env()
        rid = new_run_id("context")
        run = Run(uuid.uuid4().hex[:12], context.KEEPER_FLOW, rid, self.flows_base(),
                  workspace=ws.name, environment=env)
        picked = [known[s] for s in sessions]
        self._emit(run, {"type": "start", "flow": context.KEEPER_FLOW, "input": rid,
                         "mode": "full", "target": None, "mock": settings.mock,
                         "model": settings.model, "execution": "graph", "context": "learn",
                         "roots": [root], "workspace": ws.name, "environment": env})
        self._spawn(run, self._drive_learn(run, root, folder, picked,
                                           [notes_known[m] for m in memory]))
        self.runs[run.id] = run
        return run

    async def _child(self, run: Run, child: str, spec_data: dict[str, Any], agent: str,
                     values: dict[str, Any], tag: str) -> dict[str, Any]:
        """One system agent as a child engine of `run`; returns its outputs."""
        spec = FlowSpec.model_validate(spec_data)
        compiled = compile_graph(spec, Path(context.__file__).parent)
        compiled.policy.approver = self._approver(run)
        io_tool.save_flow_inputs(child, {k: v for k, v in values.items() if v})
        engine = GraphEngine(compiled, child, host=run, query_fn=self._query, route_fn=self._route,
                             emit=lambda e: self._emit(run, {**e, "sub": tag}))
        out = await engine.run()
        if out.status != "done":
            raise RuntimeError(out.message or f"the agent stopped ({out.status})")
        return io_tool.outputs(child, agent)

    async def _drive_learn(self, run: Run, root: str, folder: str,
                           sessions: list[dict[str, Any]], memory: list[dict[str, Any]]) -> None:
        try:
            ok = await self._learn_steps(run, root, folder, sessions, memory)
            run.status = "done" if ok else "error"
            self._emit(run, {"type": "end", "status": run.status, "gate": None})
        except asyncio.CancelledError:
            run.status = "stopped"
            self._emit(run, {"type": "end", "status": "stopped"})
        except Exception as e:  # noqa: BLE001 — report in the trace instead of losing the run
            run.status = "error"
            self._emit(run, {"type": "end", "status": "error",
                             "message": f"{type(e).__name__}: {e}"})

    async def _learn_steps(self, run: Run, root: str, folder: str,
                           sessions: list[dict[str, Any]], memory: list[dict[str, Any]]) -> bool:
        ws = workspaces.current()
        sources: list[tuple[str, str, Any]] = [("memory", "Memory notes", None)] if memory else []
        sources += [("session", s["title"] or s["first_prompt"][:60] or s["id"], s)
                    for s in sessions]
        notes, used_sessions, used_memory = [], {}, {}
        for n, (kind, title, s) in enumerate(sources, 1):
            self._emit(run, {"type": "context", "root": root, "status": "running",
                             "mode": "learn", "source": title, "n": n, "of": len(sources)})
            try:
                text = (await asyncio.to_thread(claude_sessions.memory_digest, folder,
                                                [m["file"] for m in memory])
                        if kind == "memory" else
                        await asyncio.to_thread(claude_sessions.digest, s["id"], folder, s))
                if not text.strip():
                    raise RuntimeError("the source has no conversation text")
                out = await self._child(run, f"{run.input[:24]}--r{n}", context.reader_flow(),
                                        context.READER_AGENT,
                                        {"root": root, "kind": kind, "source": text},
                                        f"learn:{n}")
                got = str(out.get("notes") or "").strip()
                if got and got != "Nothing to keep.":
                    notes.append(f"#### From {kind} `{title}`\n{got}")
                if kind == "memory":
                    used_memory = {m["file"]: m["modified"] for m in memory}
                else:
                    used_sessions[s["id"]] = s["modified"] or 0
                self._emit(run, {"type": "context", "root": root, "status": "done",
                                 "mode": "learn", "source": title, "n": n, "of": len(sources)})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — one source fails, the others go on
                self._emit(run, {"type": "context", "root": root, "status": "error",
                                 "mode": "learn", "source": title, "n": n, "of": len(sources),
                                 "message": f"{type(e).__name__}: {e}"[:500]})
        if not used_sessions and not used_memory:
            return False
        secs = context.sections(ws)
        self._emit(run, {"type": "context", "root": root, "status": "running", "mode": "history"})
        try:
            out = await self._child(
                run, f"{run.input[:24]}--h", context.historian_flow(root),
                context.HISTORIAN_AGENT,
                {"root": root, "notes": "\n\n".join(notes)[:context.MAX_NOTES] or "Nothing to keep.",
                 "current": secs.get(context.history_of(root), ""),
                 "context": secs.get(root, "")}, "learn:history")
            section = str(out.get("section") or "").strip()
            if not section:
                raise RuntimeError("the history keeper returned no section")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._emit(run, {"type": "context", "root": root, "status": "error",
                             "mode": "history", "message": f"{type(e).__name__}: {e}"[:500]})
            return False
        draft = {"root": root, "run": run.input, "section": section,
                 "summary": str(out.get("summary") or "")[:500],
                 "sessions": used_sessions, "memory": used_memory, "created": time.time()}
        d = self._learn_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"draft-{root}.json").write_text(json.dumps(draft, indent=1), encoding="utf-8")
        self._emit(run, {"type": "context", "root": root, "status": "done", "mode": "history",
                         "draft": True, "summary": draft["summary"]})
        return True

    @staticmethod
    def _audit_context(ws: str, root: str, mode: str, run_id: str) -> None:
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        with (settings.runs_dir / "audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "event": "workspace_context", "workspace": ws,
                                "root": root, "mode": mode, "run": run_id}) + "\n")
