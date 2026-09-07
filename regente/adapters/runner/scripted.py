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

The JSON keys below (resumo, desfecho, objetivo, limites, ...) are the wire
contract with that external process and stay as they are.
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
    default_value: dict[str, Any] = field(default_factory=lambda: {"ok": True, "resumo": "no change"})

    def run(self, request: RunRequest) -> RunResult:
        key = request.context.get("chave", request.task_id)
        d = self.script.get(key, self.default_value)
        # The area exists and belongs to the worker: writing in it proves the
        # isolation worked, and leaves a trace to inspect after the tick.
        Path(request.area.path).mkdir(parents=True, exist_ok=True)
        (Path(request.area.path) / "run.json").write_text(
            json.dumps({"run": request.run_id, "objetivo": request.goal, "desfecho": d},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return RunResult(
            ok=bool(d.get("ok", True)),
            summary=str(d.get("resumo", "")),
            outcome=str(d.get("desfecho", "concluido" if d.get("ok", True) else "error")),
            cost_usd=float(d.get("custo_usd", 0.0)),
            tokens=int(d.get("tokens", 0)),
            tool_calls=int(d.get("chamadas_tool", 0)),
            iterations=int(d.get("iteracoes", 1)),
            question=d.get("pergunta"))


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
            "run_id": request.run_id, "task_id": request.task_id, "agente": request.agent,
            "objetivo": request.goal, "area": request.area.path,
            "branch": request.area.branch, "contexto": request.context,
            "limites": {"iteracoes": request.limit_iterations,
                        "tool_calls": request.limit_tool_calls,
                        "custo_usd": request.limit_cost_usd,
                        "segundos": request.limit_seconds},
        }, ensure_ascii=False)
        try:
            p = subprocess.run(
                self.command, input=payload, cwd=request.area.path,
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=request.limit_seconds + self.timeout_slack)
        except subprocess.TimeoutExpired:
            return RunResult(ok=False, summary=f"timed out after {request.limit_seconds}s",
                             outcome="timebox")
        if p.returncode != 0:
            return RunResult(ok=False, outcome="error",
                             summary=f"rc={p.returncode}: {(p.stderr or '').strip()[:400]}")
        try:
            d = json.loads((p.stdout or "").strip() or "{}")
        except ValueError:
            return RunResult(ok=False, outcome="error",
                             summary=f"output is not JSON: {(p.stdout or '')[:200]}")
        return RunResult(
            ok=bool(d.get("ok", False)), summary=str(d.get("resumo", "")),
            outcome=str(d.get("desfecho", "concluido" if d.get("ok") else "error")),
            artifacts=d.get("artefatos") or {}, cost_usd=float(d.get("custo_usd", 0.0)),
            tokens=int(d.get("tokens", 0)), tool_calls=int(d.get("chamadas_tool", 0)),
            iterations=int(d.get("iteracoes", 0)), question=d.get("pergunta"))
