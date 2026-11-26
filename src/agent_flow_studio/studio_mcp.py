"""The studio for Claude Code: an MCP server (stdio) that a Claude Code session uses to list the
flows, start a run (queued when another one goes), follow it, and read its outputs and changes.

It talks to the local editor's API (`agent-flow-studio-editor`, http://127.0.0.1:8765 by default)
and every answer carries the run's link (`…/#flow=…&run=…`), which opens the run in the studio —
in Claude Code's browser pane too. **It has no tool to decide a gate or approve a write**: human
decisions stay with the person in the studio, and a run follows its flow's autonomy as always.

Setup in Claude Code:  claude mcp add agent-flow-studio -- agent-flow-studio-mcp
(`--url` for another port, `--workspace` for a studio workspace other than the current one).
"""
from __future__ import annotations

import argparse
import re
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_URL = "http://127.0.0.1:8765"
HEADERS = {"X-Flow-Editor": "1"}
NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_DIFF = 20_000  # characters of diff per file sent back to the chat


class StudioError(Exception):
    pass


def _name(value: str, what: str) -> str:
    if not NAME.fullmatch(value or ""):
        raise StudioError(f"{what} must be letters, digits, - or _ (got {value!r})")
    return value


class Studio:
    """The calls to the local editor API (a client can be given, for tests)."""

    def __init__(self, url: str = DEFAULT_URL, workspace: str | None = None,
                 client: httpx.Client | None = None):
        self.url = url.rstrip("/")
        cookies = {"afs_ws": workspace} if workspace else None
        self.http = client or httpx.Client(base_url=self.url, timeout=30, cookies=cookies)
        if client is not None and cookies:
            self.http.cookies.update(cookies)

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        try:
            r = self.http.request(method, path, json=body, headers=HEADERS)
        except httpx.HTTPError as e:
            raise StudioError(f"the studio does not answer at {self.url} ({e}); start it with "
                              "agent-flow-studio-editor") from None
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else None
            raise StudioError(f"{r.status_code}: {detail or r.text[:300]}")
        return data

    def link(self, flow: str, run: str | None = None, step: str | None = None) -> str:
        parts = [f"flow={quote(flow)}"] + ([f"run={quote(run)}"] if run else []) \
            + ([f"step={quote(step)}"] if step else [])
        return f"{self.url}/#{'&'.join(parts)}"

    def list_flows(self) -> list[dict[str, Any]]:
        return [{k: f.get(k) for k in ("name", "title", "valid", "agents", "gates", "error")
                 if f.get(k) is not None} | {"link": self.link(f["name"])}
                for f in self._call("GET", "/api/flows")]

    def flow_inputs(self, flow: str) -> dict[str, Any]:
        raw = self._call("GET", f"/api/flows/{_name(flow, 'flow')}")
        spec = raw.get("spec", raw)
        inputs = {n: {k: v for k, v in (t or {}).items()
                      if k in ("type", "label", "description", "required", "default", "options")}
                  for n, t in (spec.get("inputs") or {}).items()}
        return {"flow": flow, "title": spec.get("title"), "description": spec.get("description"),
                "execution": spec.get("execution", "orchestrator"), "inputs": inputs,
                "legacy_input": None if inputs else (spec.get("input") or {}).get("label"),
                "link": self.link(flow)}

    def start_run(self, flow: str, inputs: dict[str, Any] | None = None,
                  run_id: str | None = None) -> dict[str, Any]:
        _name(flow, "flow")
        if run_id:
            _name(run_id, "run_id")
        typed = bool(self.flow_inputs(flow)["inputs"])
        body: dict[str, Any] = {"flow": flow, "mode": "full"}
        if typed:
            body |= {"inputs": inputs or {}, "run_id": run_id}
        else:
            body["input"] = run_id or str((inputs or {}).get("input") or "") or None
        try:
            r = self._call("POST", "/api/runs", body)
            return {"started": True, "run": r["id"], "input": r["input"], "status": r["status"],
                    "link": self.link(flow, r["input"])}
        except StudioError as e:
            if not str(e).startswith("409"):
                raise
        q = self._call("POST", "/api/runs-queue", body)  # another run goes: it waits its turn
        return {"started": False, "queued": True, "queue_id": q["id"], "name": q.get("name"),
                "note": "another run goes or waits for the person: this one starts by itself after it",
                "link": self.link(flow)}

    def _find(self, run: str) -> dict[str, Any]:
        for r in self._call("GET", "/api/runs"):
            if run in (r.get("id"), r.get("input")):
                return r
        opened = self._call("POST", "/api/runs/open", {"input": _name(run, "run")})
        return opened

    def run_status(self, run: str) -> dict[str, Any]:
        r = self._find(run)
        waits = [w for w in self._call("GET", "/api/runs-waiting").get("runs", [])
                 if w.get("input") == r["input"]]
        out = {"run": r["id"], "input": r["input"], "flow": r["flow"], "status": r["status"],
               "cost": r.get("cost"), "steps": r.get("nodes") or {},
               "done": r.get("done_agents") or [],
               "waits_for_you": [{"kind": w["kind"], "label": w.get("label")} for w in waits],
               "link": self.link(r["flow"], r["input"])}
        if r.get("gate"):
            out["gate"] = {k: r["gate"].get(k) for k in ("gate", "kind", "label", "message")}
        if waits or r["status"] in ("gate", "paused", "decided"):
            out["note"] = ("A person decides this in the studio (open the link); this tool "
                           "cannot decide gates or approve writes.")
        return out

    def run_outputs(self, run: str) -> dict[str, Any]:
        r = self._find(run)
        io = self._call("GET", f"/api/io/{quote(r['input'])}?flow={quote(r['flow'])}")
        return {"run": r["input"], "status": r["status"], "inputs": io.get("inputs"),
                "outputs": io.get("outputs"), "link": self.link(r["flow"], r["input"])}

    def run_changes(self, run: str) -> dict[str, Any]:
        r = self._find(run)
        ch = self._call("GET", f"/api/runs/{quote(r['input'])}/changes")
        for root in ch.get("roots", []):
            for f in root.get("files", []):
                if len(f.get("diff") or "") > MAX_DIFF:
                    f["diff"], f["cut"] = f["diff"][:MAX_DIFF], True
        return ch | {"link": self.link(r["flow"], r["input"])}


def build_server(studio: Studio) -> Any:
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name="agent-flow-studio", instructions=(
        "Agent Flow Studio runs agent flows. List the flows, read a flow's inputs, start a run "
        "with the inputs filled from the conversation, then follow it. Give the person the run "
        "link: gates and write approvals are decided by the person in the studio, never here."))

    @server.tool(description="The flows of the studio (name, title, valid) with a link to each.")
    def list_flows() -> list[dict[str, Any]]:
        return studio.list_flows()

    @server.tool(description="The inputs a flow takes (type, label, required, default, options).")
    def flow_inputs(flow: str) -> dict[str, Any]:
        return studio.flow_inputs(flow)

    @server.tool(description="Starts a run of a flow with these inputs (queued when another run "
                 "goes or waits for the person). Returns the run and its link in the studio.")
    def start_run(flow: str, inputs: dict[str, Any] | None = None,
                  run_id: str | None = None) -> dict[str, Any]:
        return studio.start_run(flow, inputs, run_id)

    @server.tool(description="The state of a run (by run id or run folder name): each step, what "
                 "waits for the person, the cost, the link.")
    def run_status(run: str) -> dict[str, Any]:
        return studio.run_status(run)

    @server.tool(description="The inputs and the saved outputs of a run.")
    def run_outputs(run: str) -> dict[str, Any]:
        return studio.run_outputs(run)

    @server.tool(description="What a run changed in the git folders of its workspace (files, +/−, "
                 "diffs), compared with their state when it started.")
    def run_changes(run: str) -> dict[str, Any]:
        return studio.run_changes(run)

    return server


def main() -> None:
    ap = argparse.ArgumentParser(description="Agent Flow Studio as an MCP server for Claude Code")
    ap.add_argument("--url", default=DEFAULT_URL, help="the studio editor (default %(default)s)")
    ap.add_argument("--workspace", help="a studio workspace (default: the editor's current one)")
    a = ap.parse_args()
    build_server(Studio(a.url, a.workspace)).run("stdio")


if __name__ == "__main__":
    main()
