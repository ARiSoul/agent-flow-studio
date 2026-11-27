"""Past runs: the saved events of each run folder (open again, totals, trash, what waits
for the person, a step's session and the files it changed).
A mixin of `Runner` (editor/runner.py): it uses the runner's state through `self`."""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from ... import changes, claude_sessions, workspaces
from ...builder import FlowError, load_flow, load_spec
from ...builder.compile import (
    load_graph,
)
from ...builder.types import compatible as types_compatible
from ...config import settings
from ...engine import GraphEngine
from ...tools import io as io_tool
from ...tools.guards import safe_artifact_path
from .events import _last_run, _last_run_events, _read_events, _restore_engine, _totals, _waited
from .model import GATE_SCAN, STALE_GATE, Run, RunError


class HistoryMixin:

    def _set_meta(self, run_input: str, key: str, value: Any) -> None:
        """One field of a saved run's run_meta.json (None removes it)."""
        path = safe_artifact_path(settings.runs_dir, run_input, "run_meta.json")
        if not _last_run_events(path.parent / "events.jsonl"):
            raise RunError(404, "no saved run in this folder")
        meta: dict[str, Any] = {}
        if path.is_file():
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
        path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        self._gates.clear()

    def rename(self, run_input: str, title: str) -> str:
        """A name for a saved run (the folder and id stay): shown in the list of past runs."""
        title = " ".join(title.split())[:80]
        self._set_meta(run_input, "title", title or None)
        return title

    def dismiss(self, run_input: str, dismissed: bool) -> None:
        """A run waiting at a gate leaves (or goes back to) the "waits for you" count, badge and
        notifications; it still waits and can be decided at any time."""
        self._set_meta(run_input, "dismissed", time.time() if dismissed else None)
        self._start_queued()  # a dismissed gate no longer holds the queue

    def delete_run(self, run_input: str) -> str:
        """Moves a saved run folder to `runs/.trash/` (of its workspace); never one that runs.
        Returns the name it got in the trash."""
        path = safe_artifact_path(settings.runs_dir, run_input, "events.jsonl").parent
        if not path.is_dir():
            raise RunError(404, "no saved run in this folder")
        events = _last_run_events(path / "events.jsonl")
        rid = (events[0].get("run") if events else None)
        live = next((r for r in self.runs.values() if r.id == rid), None)
        if live is not None and live.status == "running":
            raise RunError(409, "the run is still running: stop it first")
        trash = workspaces.scoped(settings.runs_dir) / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        meta_path = path / "run_meta.json"  # when it was deleted (the folder keeps its own time)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        except ValueError:
            meta = {}
        meta["deleted"] = time.time()
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        name = f"{path.name}-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.move(str(path), str(trash / name))
        if live is not None:
            self.runs.pop(live.id, None)
        return name

    @staticmethod
    def _meta_of(folder: Path) -> dict[str, Any]:
        try:
            meta = json.loads((folder / "run_meta.json").read_text(encoding="utf-8"))
            return meta if isinstance(meta, dict) else {}
        except (OSError, ValueError):
            return {}

    @classmethod
    def _title_of(cls, folder: Path) -> str:
        return str(cls._meta_of(folder).get("title") or "")

    # --- past runs: the log in runs/<id>/events.jsonl is enough to open one again ---------------
    def flow_outputs(self, flow: str, kind: str | None = None,
                     limit: int = 20) -> list[dict[str, Any]]:
        """Finished runs of a flow (this workspace) with their outputs, newest first, for "Fill
        from a run": only outputs whose type can feed an input of type `kind` (files stay in
        their run). Each output has a short preview; `output_value` gives the whole value."""
        try:
            spec, _ = load_spec(flow, base=self.flows_base())
        except FlowError:
            return []
        out = []
        for r in self.past_runs(flow, limit=100):
            if r["status"] not in ("done", "stopped") or any(
                    x.input == r["input"] and x.status == "running" for x in self.runs.values()):
                continue  # a failed run, or one still going
            items = []
            for agent, a in spec.agents.items():
                values = io_tool.outputs(r["input"], agent)
                for name, t in a.outputs.items():
                    if name not in values or t.type == "file":
                        continue
                    if kind and not types_compatible(t.type, kind):
                        continue
                    text = values[name] if isinstance(values[name], str) else json.dumps(
                        values[name], ensure_ascii=False)
                    items.append({"agent": agent, "name": name, "type": t.type,
                                  "label": t.label or name, "preview": text[:200]})
            if items:
                out.append({**{k: r[k] for k in ("input", "started", "status")},
                            "outputs": items})
            if len(out) >= limit:
                break
        return out

    def output_value(self, flow: str, run_input: str, agent: str, name: str) -> Any:
        """One output of a finished run of a flow of this workspace (for a hand-off)."""
        events = _last_run_events(safe_artifact_path(settings.runs_dir, run_input,
                                                     "events.jsonl"))
        if not events or events[0].get("flow") != flow:
            raise RunError(404, f"no run {run_input!r} of the flow {flow!r} here")
        values = io_tool.outputs(run_input, agent)
        if name not in values:
            raise RunError(404, f"the run {run_input!r} has no output {agent}.{name}")
        return values[name]

    def _run_summary(self, d: Path) -> dict[str, Any] | None:
        """What History shows of a run folder, cached by the size and time of its events file."""
        f = d / "events.jsonl"
        try:
            st = f.stat()
        except OSError:
            return None
        key = (st.st_mtime_ns, st.st_size)
        hit = self._summaries.get(f)
        if hit and hit[0] == key:
            return hit[1]
        every = _read_events(f)
        events = _last_run(every)
        if not events:
            return None
        start = events[0]
        last_end = max((i for i, e in enumerate(events) if e.get("type") == "end"), default=-1)
        last_go = max((i for i, e in enumerate(events)
                       if e.get("type") in ("delegate", "resume", "continue", "decision")),
                      default=-1)
        end = events[last_end] if last_end >= 0 else None
        # no end after the last thing it did: the studio stopped while it ran (crash, restart)
        status = str(end.get("status")) if end and last_end > last_go else "lost"
        gate = (end or {}).get("gate") or {}
        failed = next((str(e.get("agent")) for e in reversed(events)
                       if e.get("type") == "agent_done" and e.get("error")), None)
        # the first run plus each resume / "run from step" / earlier run in the same folder
        attempts = sum(1 for e in every if e.get("type") in ("start", "resume"))
        out = {
            "input": d.name, "run": start.get("run"), "flow": start.get("flow"),
            "workspace": start.get("workspace") or workspaces.STUDIO,
            "started": start.get("ts"), "ended": (end or {}).get("ts") if status != "lost" else None,
            "mode": start.get("mode"), "target": start.get("target"),
            "execution": start.get("execution"), "status": status, "gate": gate.get("label"),
            "failed": failed if status == "error" else None,
            "message": str((end or {}).get("message") or "")[:300] or None,
            "cost": round(sum(float(e.get("cost") or 0) for e in every  # all attempts
                              if e.get("type") == "result"), 4),
            "attempts": max(1, attempts),
        }
        if status == "gate":  # since when it waits
            out["since"] = (end or {}).get("ts")
        if status == "decided":
            out["decision"] = (end or {}).get("decision")
        fq = next((e for e in events if e.get("type") == "from_queue"), None)
        if fq:
            out["from_queue"] = fq.get("queued")
        out["waited"] = round(_waited(events, None if status == "gate" else (end or {}).get("ts")), 1)
        self._summaries[f] = (key, out)
        return out

    def past_runs(self, flow: str, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
        """The runs of a flow saved in runs/ (the last run in each folder), newest first."""
        return self.past_page(flow, limit, offset)[0]

    def past_stats(self, flow: str, days: int | None = None) -> dict[str, Any]:
        """Totals of the saved runs of a flow: how many, how many failed, the median time of the
        finished ones and what they cost. With `days`: only the runs started in that many last
        days, and `prev`, the same totals for the period just before (to see a trend)."""
        every = self._all_past(flow)
        if not days:
            return _totals(every)
        now, span = time.time(), days * 86400
        out = _totals([r for r in every if (r.get("started") or 0) >= now - span])
        out["prev"] = _totals([r for r in every if now - 2 * span <= (r.get("started") or 0) < now - span])
        out["days"] = days
        return out

    def past_page(self, flow: str, limit: int = 30, offset: int = 0, q: str = "",
                  status: str = "") -> tuple[list[dict[str, Any]], int]:
        """One page of the saved runs of a flow, newest first, and how many match. `q`: words to
        find in the name, id, failed step, gate or message; `status`: only runs in that state."""
        out = self._all_past(flow)
        if status:
            out = [r for r in out if r["status"] == status]
        for word in q.lower().split():
            out = [r for r in out if word in " ".join(str(r.get(k) or "") for k in (
                "title", "input", "failed", "gate", "message", "mode", "target")).lower()]
        return out[offset:offset + limit], len(out)

    def waiting(self) -> list[dict[str, Any]]:
        """The runs of this workspace (any flow) that wait for the person now: at a human gate,
        paused between steps, or with a write to approve. Each has `since`; a gate waiting for
        more than STALE_GATE seconds is `stale`, and the person may have `dismissed` it: the
        editor leaves both out of its count, badge and notifications (they still wait)."""
        here = workspaces.current().name
        now = time.time()
        out: list[dict[str, Any]] = []
        for r in self.runs.values():
            if (r.workspace or workspaces.STUDIO) != here:
                continue
            item: dict[str, Any] = {"id": r.id, "input": r.input, "flow": r.flow, "title": ""}
            try:
                meta = self._meta_of(safe_artifact_path(settings.runs_dir, r.input, "x").parent)
                item["title"] = str(meta.get("title") or "")
                item["dismissed"] = bool(meta.get("dismissed"))
            except ValueError:
                pass
            since = next((e.get("ts") for e in reversed(r.events)
                          if e.get("type") in ("gate", "end", "agent_done", "approval")), None)
            item["since"] = since
            if r.status == "gate" and r.gate and r.gate.get("kind") == "human":
                out.append(item | {"kind": "gate",
                                   "label": r.gate.get("label") or r.gate.get("gate")})
            elif r.status == "paused":
                out.append(item | {"kind": "paused", "label": r.target or ""})
            for info, fut in list(r.pending.values()):
                if not fut.done():
                    out.append(item | {"kind": "approval", "dismissed": False,
                                       "label": str(getattr(info, "summary", ""))[:120]})
        known = {r.id for r in self.runs.values()}
        out += [g for g in self._saved_gates(here) if g["run"] not in known]
        for x in out:
            x.pop("run", None)
            x.pop("workspace", None)
            x["stale"] = x["kind"] == "gate" and bool(x.get("since")) and \
                now - float(x["since"]) > STALE_GATE
        return out

    def _saved_gates(self, here: str) -> list[dict[str, Any]]:
        """Saved runs at a human gate (after a restart they are no longer in memory). The scan is
        kept for GATE_SCAN seconds, and dropped when a run ends or a run's meta changes."""
        root = workspaces.scoped(settings.runs_dir)
        hit = self._gates.get(root)
        if hit and time.time() - hit[0] < GATE_SCAN:
            return [dict(g) for g in hit[1] if g["workspace"] == here]
        items: list[dict[str, Any]] = []
        for d in (root.iterdir() if root.is_dir() else []):
            if not d.is_dir() or d.name.startswith((".", "@")) or d.name == "chats":
                continue
            s = self._run_summary(d)
            if s and s["status"] == "gate":
                meta = self._meta_of(d)
                items.append({"id": None, "run": s["run"], "input": d.name, "flow": s["flow"],
                              "title": str(meta.get("title") or ""), "kind": "gate",
                              "label": s.get("gate") or "", "since": s.get("since"),
                              "dismissed": bool(meta.get("dismissed")),
                              "workspace": s["workspace"]})
        self._gates[root] = (time.time(), items)
        return [dict(g) for g in items if g["workspace"] == here]

    def step_session(self, run_input: str, node: str) -> dict[str, Any]:
        """The Claude Code session of a step (its last one), the folder it ran in and the command
        that goes on with it in Claude Code — with its own tools: images, browser, files."""
        events = _last_run_events(safe_artifact_path(settings.runs_dir, run_input, "events.jsonl"))
        sid = next((str(e["session_id"]) for e in reversed(events) if e.get("type") == "result"
                    and e.get("agent") == node and e.get("session_id") and not e.get("sub")), None)
        if not sid:
            raise RunError(404, "this step has no session on record")
        cwd = claude_sessions.session_cwd(sid)
        if not cwd:
            raise RunError(404, "the session is not on this machine (Claude Code keeps sessions "
                                "for a limited time)")
        import sys
        return {"session_id": sid, "cwd": cwd,
                "command": claude_sessions.resume_command(sid, cwd, sys.platform == "win32")}

    def run_changes(self, run_input: str) -> dict[str, Any]:
        """What a run changed in the git roots of its workspace since it started (read only;
        see changes.py). `available` False: the run has no git baseline (older run, no git)."""
        events = _last_run_events(safe_artifact_path(settings.runs_dir, run_input, "events.jsonl"))
        if not events:
            raise RunError(404, "no saved run in this folder")
        base = events[0].get("git") or {}
        roots = []
        for name, snap in base.items():
            path = Path(str(snap.get("path") or ""))
            if not path.is_dir():
                roots.append({"name": name, "path": str(path), "missing": True, "files": []})
                continue
            roots.append({"name": name, "path": str(path), "branch": snap.get("branch"),
                          "head": snap.get("head"), "files": changes.changes(path, snap)})
        return {"available": bool(base), "roots": roots}

    def tool_call(self, run_id: str, tool_id: str) -> dict[str, Any]:
        """One tool call of a run, read on demand from the agent's Claude Code session on this
        machine (the run's events never keep arguments or results): its arguments and the start
        of its result, with secret-like text masked. Nothing is stored."""
        from claude_agent_sdk import get_session_messages

        from ... import claude_sessions
        run = self.get(run_id)
        call = next((e for e in run.events if e.get("type") == "tool" and e.get("id") == tool_id), None)
        if call is None:
            raise RunError(404, "no tool call with this id in the run")
        agent = call.get("agent")
        sessions = [call["session"]] if call.get("session") else []
        sessions += [str(e["session_id"]) for e in run.events if e.get("type") == "result"
                     and e.get("agent") == agent and e.get("session_id")]
        for sid in dict.fromkeys(sessions):
            try:
                messages = get_session_messages(sid)
            except (OSError, ValueError):
                continue
            args = result = None
            is_error = False
            for m in messages:
                content = (m.message or {}).get("content") if isinstance(m.message, dict) else None
                for b in content if isinstance(content, list) else []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use" and b.get("id") == tool_id:
                        args = b.get("input")
                    elif b.get("type") == "tool_result" and b.get("tool_use_id") == tool_id:
                        is_error = bool(b.get("is_error"))
                        c = b.get("content")
                        result = c if isinstance(c, str) else "\n".join(
                            str(x.get("text") or "") for x in (c or []) if isinstance(x, dict))
            if args is not None or result is not None:
                text = json.dumps(args, ensure_ascii=False, indent=1, default=str) if args is not None else ""
                res = claude_sessions.mask(result or "")
                return {"tool": call.get("tool"), "agent": agent, "session": sid,
                        "input": claude_sessions.mask(text)[:20_000],
                        "result": res[:20_000], "result_size": len(result or ""),
                        "cut": len(res) > 20_000, "is_error": is_error}
        raise RunError(404, "the session of this call is not on this machine any more")

    def _all_past(self, flow: str) -> list[dict[str, Any]]:
        """Every saved run of a flow in this workspace, newest first."""
        out = []
        root = workspaces.scoped(settings.runs_dir)  # the runs of this workspace
        if not root.is_dir():
            return []
        here = workspaces.current().name
        for d in root.iterdir():
            if not d.is_dir() or d.name.startswith((".", "@")) or d.name == "chats":
                continue
            s = self._run_summary(d)
            if s is None or s["flow"] != flow or s["workspace"] != here:
                continue
            live = next((r for r in self.runs.values() if r.id == s["run"]), None)
            item = {**s, "open": live is not None, "title": self._title_of(d)}
            if live is not None and live.status == "running":
                item |= {"status": "running", "ended": None}
            out.append(item)
        out.sort(key=lambda x: -(x["started"] or 0))
        return out

    def trash(self, flow: str | None = None) -> list[dict[str, Any]]:
        """The deleted runs of this workspace (runs/.trash), of one flow if given, newest first."""
        d = workspaces.scoped(settings.runs_dir) / ".trash"
        if not d.is_dir():
            return []
        out = []
        for t in d.iterdir():
            events = _last_run_events(t / "events.jsonl") if t.is_dir() else []
            if not events or (flow and events[0].get("flow") != flow):
                continue
            try:
                meta = json.loads((t / "run_meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            out.append({"name": t.name, "input": t.name.rsplit("-", 2)[0],
                        "title": meta.get("title") or "", "flow": events[0].get("flow"),
                        "started": events[0].get("ts"),
                        "deleted": meta.get("deleted") or t.stat().st_mtime})
        return sorted(out, key=lambda x: -x["deleted"])

    def restore_run(self, name: str) -> str:
        """Brings a deleted run back from the trash (under its old id, or a free one)."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
            raise RunError(400, "not a run in the trash")
        root = workspaces.scoped(settings.runs_dir)
        src = root / ".trash" / name
        if not src.is_dir():
            raise RunError(404, "not a run in the trash")
        base = name.rsplit("-", 2)[0]
        target, n = base, 1
        while (root / target).exists():
            n += 1
            target = f"{base}-{n}"
        shutil.move(str(src), str(root / target))
        return target

    def empty_trash(self, flow: str | None = None) -> tuple[int, list[str]]:
        """Deletes the runs in the trash (of one flow if given) for good. Returns how many were
        deleted and the ones that could not be (a file in use…)."""
        d = workspaces.scoped(settings.runs_dir) / ".trash"
        done, failed = 0, []
        for item in self.trash(flow):
            try:
                shutil.rmtree(d / item["name"])
                done += 1
            except OSError:
                failed.append(item["name"])
        return done, failed

    def open_past(self, run_input: str, register: bool = True) -> Run:
        """A saved run again, as it ended (done, at a gate, stopped…): its log, its outputs and the
        session of each agent, so the person can look at it, ask for more or answer its gate. The
        agents use the flow as it is now. `register=False`: only to look at it (it is not kept)."""
        path = safe_artifact_path(settings.runs_dir, run_input, "events.jsonl")
        events = _last_run_events(path)
        if not events:
            raise RunError(404, "no saved run in this folder")
        start = events[0]
        rid = str(start.get("run"))
        if rid in self.runs:  # still in memory: that one
            return self.runs[rid]
        flow = str(start.get("flow"))
        ws = workspaces.get_or_none(start.get("workspace") or workspaces.STUDIO) \
            or workspaces.STUDIO_WS
        env = start.get("environment")
        base = self.base if ws.studio else ws.flows_dir
        with workspaces.using(ws, env):
            compiled = load_flow(flow, base=base)  # the flow must still exist and be valid
        run = Run(rid, flow, run_input, base, mode=start.get("mode") or "full",
                  target=start.get("target"), started=float(start.get("ts") or time.time()),
                  workspace=ws.name, environment=env)
        run.events = [{k: v for k, v in e.items() if k != "run"} | {"i": i}
                      for i, e in enumerate(events)]
        end = next((e for e in reversed(events) if e.get("type") == "end"), None)
        run.status = (end or {}).get("status") or "stopped"
        if run.status == "running":
            run.status = "stopped"  # the editor stopped while it ran
        decided = run.status == "decided"
        if decided:  # its decision is kept: it goes on when no run goes
            run.status = "gate"  # restored at its gate, then decided again below
        run.gate = (end or {}).get("gate") if run.status == "gate" else None
        if run.status == "done" and (end or {}).get("gate"):
            run.gate = end["gate"]  # a stop with its message
        run.cost = sum(float(e.get("cost") or 0) for e in events if e.get("type") == "result")
        results = [e for e in events if e.get("type") == "result" and e.get("session_id")]
        run.done_agents = {str(e["agent"]) for e in events
                           if e.get("type") == "agent_done" and not e.get("error") and e.get("agent")}
        if start.get("execution") == "graph" and compiled.spec.execution == "graph":
            with workspaces.using(ws, env):
                graph = load_graph(flow, base=base)
            graph.policy.approver = self._approver(run)
            run.engine = GraphEngine(graph, run_input, host=run,
                                     emit=lambda ev: self._emit(run, ev), query_fn=self._query,
                                     route_fn=self._route, mode=run.mode, target=run.target)
            _restore_engine(run.engine, events, run)
        else:
            orch = [e for e in results if not e.get("agent")]
            run.session_id = orch[-1]["session_id"] if orch else None
        if decided and end and run.gate:
            run.status = "decided"
            run.after = (run.gate, end.get("decision") or "approve", str(end.get("comment") or ""),
                         float(end.get("ts") or time.time()))
            last_end = max(i for i, e in enumerate(events) if e.get("type") == "end")
            hold = next((e for e in reversed(events[last_end:]) if e.get("type") == "hold"), None)
            if hold and hold.get("on"):  # a change of decision was going on: still held
                run.holding = float(hold.get("ts") or time.time())
        if register:
            self.runs[run.id] = run
        return run
