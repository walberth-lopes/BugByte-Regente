# -*- coding: utf-8 -*-
"""Runners that do not depend on an LLM.

`ScriptedRunner` executes a declared script. It exists for two legitimate things
-- exercising the engine in tests and running the **shadow phase**, in which the
owner checks the orchestrator's decisions before any real worker touches code. It
does not pretend to be an agent: its outcome comes from the script, and so no
architectural defect can hide behind it.

`CommandRunner` executes an external process. That is the real path: any headless
agentic harness, a script of your own or a loop over LLMProvider all come in
through here without the Orchestrator knowing the difference.

The JSON keys below (summary, outcome, goal, limits, ...) are the wire contract
with that external process, and use the same words as the newer
`ports.agent` protocol so a script written for one reads like the other.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...ports.workspace import AgentRunner, RunRequest, RunResult


@dataclass(slots=True)
class ScriptedRunner(AgentRunner):
    name: str = "roteiro"
    #: task key -> declared outcome
    script: dict[str, dict[str, Any]] = field(default_factory=dict)
    default_value: dict[str, Any] = field(default_factory=lambda: {"ok": True, "summary": "no change"})

    def run(self, request: RunRequest) -> RunResult:
        key = request.context.get("key", request.task_id)
        d = self.script.get(key, self.default_value)
        # The area exists and belongs to the worker: writing in it proves the
        # isolation worked, and leaves a trace to inspect after the tick.
        Path(request.area.path).mkdir(parents=True, exist_ok=True)
        (Path(request.area.path) / "run.json").write_text(
            json.dumps({"run": request.run_id, "goal": request.goal, "outcome": d},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return RunResult(
            ok=bool(d.get("ok", True)),
            summary=str(d.get("summary", "")),
            outcome=str(d.get("outcome", "FINISHED" if d.get("ok", True) else "ERROR")),
            cost_usd=float(d.get("cost_usd", 0.0)),
            tokens=int(d.get("tokens", 0)),
            tool_calls=int(d.get("tool_calls", 0)),
            iterations=int(d.get("iterations", 1)),
            question=d.get("question"))


@dataclass(slots=True)
class CommandRunner(AgentRunner):
    """Executes an external command in the worker's area.

    Contract with the process: it receives the request as JSON on stdin and must
    print a result JSON on stdout. Unreadable output is treated as a failure --
    and not as a silent success -- because a worker that cannot report what it
    did cannot be considered to have succeeded.
    """
    command: list[str] = field(default_factory=list)
    name: str = "comando"
    timeout_slack: int = 120

    def run(self, request: RunRequest) -> RunResult:
        payload = json.dumps({
            "run_id": request.run_id, "task_id": request.task_id, "agent": request.agent,
            "goal": request.goal, "area": request.area.path,
            "branch": request.area.branch, "context": request.context,
            "limits": {"iterations": request.limit_iterations,
                        "tool_calls": request.limit_tool_calls,
                        "cost_usd": request.limit_cost_usd,
                        "seconds": request.limit_seconds},
        }, ensure_ascii=False)
        try:
            p = subprocess.run(
                self.command, input=payload, cwd=request.area.path,
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=request.limit_seconds + self.timeout_slack)
        except subprocess.TimeoutExpired:
            return RunResult(ok=False, summary=f"timed out after {request.limit_seconds}s",
                             outcome="TIMEBOX")
        if p.returncode != 0:
            return RunResult(ok=False, outcome="ERROR",
                             summary=f"rc={p.returncode}: {(p.stderr or '').strip()[:400]}")
        try:
            d = json.loads((p.stdout or "").strip() or "{}")
        except ValueError:
            return RunResult(ok=False, outcome="ERROR",
                             summary=f"output is not JSON: {(p.stdout or '')[:200]}")
        return RunResult(
            ok=bool(d.get("ok", False)), summary=str(d.get("summary", "")),
            outcome=str(d.get("outcome", "FINISHED" if d.get("ok") else "ERROR")),
            artifacts=d.get("artifacts") or {}, cost_usd=float(d.get("cost_usd", 0.0)),
            tokens=int(d.get("tokens", 0)), tool_calls=int(d.get("tool_calls", 0)),
            iterations=int(d.get("iterations", 0)), question=d.get("question"))
