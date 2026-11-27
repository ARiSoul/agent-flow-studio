"""The run queue: runs asked for while another one goes, kept in runs/.queue.json, and
the decided runs that go on when nothing runs.
A mixin of `Runner` (editor/runner.py): it uses the runner's state through `self`."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from ... import workspaces
from ...builder import FlowError, load_flow
from ...config import settings
from .model import HOLD_FOR, Run, RunError


class QueueMixin:

    def _queue_file(self) -> Path:
        return settings.runs_dir / ".queue.json"

    def _save_queue(self) -> None:
        """The queue on disk (`runs/.queue.json`), so a restart keeps it. An item whose files were
        sent inside the request (not staged uploads) cannot be kept: it is saved without them and
        reported as lost after a restart."""
        items = []
        for i in self.queued:
            kw = i["_kw"]
            inline = any(isinstance(v, dict) and "content_b64" in v or (
                isinstance(v, list) and any(isinstance(x, dict) and "content_b64" in x for x in v))
                for v in (kw.get("inputs") or {}).values())
            items.append(self._public(i) | {"env": i["_env"], "kw": None if inline else kw})
        try:
            path = self._queue_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(items, ensure_ascii=False, default=str), encoding="utf-8")
        except OSError:
            pass  # the queue still works in memory

    def _load_queue(self) -> None:
        try:
            items = json.loads(self._queue_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        known = {i["id"] for i in self.queued}
        for it in items if isinstance(items, list) else []:
            if it.get("id") in known:
                continue  # already in memory (this studio queued it)
            ws = workspaces.get_or_none(it.get("workspace") or workspaces.STUDIO)
            if it.get("kw") is None or ws is None:
                self.queue_log = [*self.queue_log, {k: it.get(k) for k in (
                    "id", "flow", "name", "mode", "workspace")} | {
                    "ts": time.time(), "error": "not kept after the restart (its files were sent "
                    "with the request, or its workspace is gone): queue it again"}][-30:]
                continue
            self.queued.append({k: it.get(k) for k in ("id", "flow", "queued", "name", "mode",
                                                        "workspace")}
                               | {"_ws": ws, "_env": it.get("env"), "_kw": it["kw"]})

    def tick(self) -> None:
        """The queue's own clock (the server calls it every few seconds): once, after a start,
        the queue and the decided runs come back from disk; then decided runs and queued runs go
        on when nothing holds them (a gate that turned older than a day holds nothing)."""
        if not self._restored:
            self._restored = True
            self._load_queue()
            self._restore_decided()
        if self.queued or any(r.status == "decided" for r in self.runs.values()):
            self._start_queued()

    def _restore_decided(self) -> None:
        """Runs decided while another went, saved before a restart: back in memory, decided."""
        spaces = [workspaces.STUDIO_WS] + [w for w in (workspaces.get_or_none(x.get("name"))
                                                        for x in workspaces.list_all()) if w]
        seen: set[str] = set()
        for ws in spaces:
            if ws.name in seen:
                continue
            seen.add(ws.name)
            with workspaces.using(ws):
                root = workspaces.scoped(settings.runs_dir)
                for d in (root.iterdir() if root.is_dir() else []):
                    if not d.is_dir() or d.name.startswith((".", "@")) or d.name == "chats":
                        continue
                    sm = self._run_summary(d)
                    if sm and sm["status"] == "decided" and sm["run"] not in self.runs:
                        try:
                            self.open_past(d.name)
                        except (RunError, FlowError, ValueError, OSError):
                            pass  # the flow is gone or broken: it stays saved as it is

    def holds(self) -> list[dict[str, Any]]:
        """What keeps a queued run of the current workspace from starting: the run that goes (any
        workspace: one run at a time), or the runs that wait for the person. The same rule as the
        editor's count of what waits for you (`waiting()`, without gates older than a day or
        dismissed): starting a run then would block the person's decision."""
        live = self.active()
        if live is not None:
            return [{"kind": "running", "input": live.input, "flow": live.flow, "label": "",
                     "workspace": live.workspace or workspaces.STUDIO}]
        now = time.time()  # a change of decision holds any workspace, like the run that goes
        changing = [{"kind": "changing", "input": r.input, "flow": r.flow, "label": "",
                     "since": r.holding} for r in self.runs.values()
                    if r.status == "decided" and r.holding and now - r.holding < HOLD_FOR]
        return changing + [w for w in self.waiting() if not w.get("stale") and not w.get("dismissed")]

    def _holds_for(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        with workspaces.using(item["_ws"], item["_env"]):
            return self.holds()

    def enqueue(self, flow: str, **kw: Any) -> dict[str, Any]:
        """A run to start when no run goes and none waits for the person (only one runs at a
        time). The flow is checked now; inputs when it starts (an error shows in the queue)."""
        if not self.holds():
            raise RunError(409, "nothing is running or waiting: start the run now")
        load_flow(flow, base=self.flows_base())
        ws, env = workspaces.current(), workspaces.current_env()
        item = {"id": uuid.uuid4().hex[:10], "flow": flow, "queued": time.time(),
                "name": str(kw.get("run_id") or kw.get("run_input") or ""),
                "mode": kw.get("mode") or "full", "workspace": ws.name, "_ws": ws, "_env": env,
                "_kw": kw}
        self.queued.append(item)
        self._save_queue()
        return self._public(item)

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in item.items() if not k.startswith("_")}

    def queue(self) -> dict[str, Any]:
        """The queue of this workspace: waiting runs (their place and what holds them) and the
        last outcomes. The queue lives in memory: a restart of the studio clears it."""
        here = workspaces.current().name
        mine = [i for i in self.queued if i["workspace"] == here]
        held = self.holds() if mine else []
        return {"queued": [self._public(i) | {"place": n + 1, "held_by": held}
                           for n, i in enumerate(mine)],
                "log": [i for i in self.queue_log if i["workspace"] == here][-10:]}

    def start_queued_now(self, item_id: str) -> Run:
        """A queued run starts now, even while other runs wait for the person (a decision made
        meanwhile is kept and its run goes on when this one ends); never while another run goes."""
        item = next((i for i in self.queued if i["id"] == item_id), None)
        if item is None:
            raise RunError(404, "that run is not in the queue")
        if self.active():
            raise RunError(409, "a run is in progress: only one runs at a time")
        self.queued.remove(item)
        self._save_queue()
        kw = dict(item["_kw"])
        with workspaces.using(item["_ws"], item["_env"]):
            run = self.start(item["flow"], kw.pop("run_input", None), **kw)
        self._emit(run, {"type": "from_queue", "queued": item["queued"], "anyway": True})
        self.queue_log = [*self.queue_log, self._public(item) | {
            "ts": time.time(), "started": run.id, "input": run.input}][-30:]
        return run

    def unqueue(self, item_id: str) -> None:
        before = len(self.queued)
        self.queued = [i for i in self.queued if i["id"] != item_id]
        if len(self.queued) == before:
            raise RunError(404, "that run is not in the queue")
        self._save_queue()

    def _start_queued(self) -> None:
        """When nothing runs: first a run decided at its gate while another went (the person's
        decision never waits behind the queue), then the next queued run that nothing waiting
        for the person holds (called when a run's task ends, a waiting run is stopped or
        dismissed, and on each look at what waits)."""
        now = time.time()
        waiting_turn = sorted((r for r in self.runs.values() if r.status == "decided" and r.after
                               and not (r.holding and now - r.holding < HOLD_FOR)),
                              key=lambda r: r.after[3])  # type: ignore[index]  # in decision order
        decided = waiting_turn[0] if waiting_turn else None
        if decided is not None and not self.active():
            gate, decision, comment, _ = decided.after  # type: ignore[misc]
            decided.after = None
            try:
                with self._in(decided):
                    self._continue_gate(decided, gate, decision, comment)
                return
            except Exception as e:  # noqa: BLE001 — whatever failed, it ends; the queue goes on
                decided.status = "error"
                self._emit(decided, {"type": "end", "status": "error", "message":
                                     f"could not go on after the decision: {e}"[:500]})
        while self.queued:
            item = next((i for i in self.queued if not self._holds_for(i)), None)
            if item is None:
                return
            self.queued.remove(item)
            self._save_queue()
            out = self._public(item) | {"ts": time.time()}
            try:
                with workspaces.using(item["_ws"], item["_env"]):
                    kw = dict(item["_kw"])
                    run = self.start(item["flow"], kw.pop("run_input", None), **kw)
                self._emit(run, {"type": "from_queue", "queued": item["queued"]})
                out["started"], out["input"] = run.id, run.input
            except (RunError, FlowError, ValueError, OSError) as e:
                out["error"] = str(e)[:300]
            self.queue_log = [*self.queue_log, out][-30:]
