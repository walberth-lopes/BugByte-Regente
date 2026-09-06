# -*- coding: utf-8 -*-
"""Coding agents that live in an external process.

`ExternalAgent` is the real implementation of the port: it hands a JSON request
to a process on stdin and reads a JSON result from stdout. Any headless coding
agent fits behind it -- the engine never learns which one, and swapping it is a
configuration line.

`DeterministicAgent` runs a small program that is NOT a language model. It exists
to prove the harness, and its name says so. What it demonstrates is the chain --
mission selection, target resolution, isolation, structured outcome,
baseline-aware testing, verdict -- and nothing about whether a model can solve a
task. Presenting one as the other would be the most flattering lie this project
could tell itself.

Both refuse to guess. A process that returns unparseable output is an ERROR, not
an optimistic success: an agent that cannot report what it did has not
demonstrated that it did anything.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...ports.agent import (ChangedFile, CodingAgent, ExecutionRequest,
                            ExecutionResult, Finding, Outcome)

#: Outcomes the engine accepts from a process. Anything else is ERROR -- the
#: vocabulary is closed precisely so an unknown word cannot become success.
VALID_OUTCOMES = {o.value for o in Outcome}


def parse_result(payload: Any) -> ExecutionResult:
    """Turn a process's JSON into a result. Never trusts, always checks."""
    if not isinstance(payload, dict):
        return ExecutionResult(outcome=Outcome.ERROR,
                               summary=f"expected an object, got {type(payload).__name__}")
    raw_outcome = str(payload.get("outcome", "")).upper()
    if raw_outcome not in VALID_OUTCOMES:
        return ExecutionResult(
            outcome=Outcome.ERROR,
            summary=f"unknown outcome {raw_outcome!r}; expected one of "
                    f"{', '.join(sorted(VALID_OUTCOMES))}")
    return ExecutionResult(
        outcome=Outcome(raw_outcome),
        summary=str(payload.get("summary", "")),
        changed_files=tuple(
            ChangedFile(path=str(f.get("path", "")),
                        additions=int(f.get("additions", 0)),
                        deletions=int(f.get("deletions", 0)),
                        status=str(f.get("status", "modified")))
            for f in (payload.get("changed_files") or []) if isinstance(f, dict)),
        commits=tuple(str(c) for c in (payload.get("commits") or [])),
        test_output=str(payload.get("test_output", "")),
        test_command=str(payload.get("test_command", "")),
        test_exit_code=(int(payload["test_exit_code"])
                        if payload.get("test_exit_code") is not None else None),
        findings=tuple(
            Finding(kind=str(f.get("kind", "note")), text=str(f.get("text", "")),
                    path=str(f.get("path", "")))
            for f in (payload.get("findings") or []) if isinstance(f, dict)),
        blockers=tuple(str(b) for b in (payload.get("blockers") or [])),
        question=payload.get("question") if isinstance(payload.get("question"), dict) else None,
        cost_usd=float(payload.get("cost_usd", 0.0)),
        tokens=int(payload.get("tokens", 0)),
        tool_calls=int(payload.get("tool_calls", 0)),
        duration_seconds=float(payload.get("duration_seconds", 0.0)))


@dataclass(slots=True)
class ExternalAgent(CodingAgent):
    """Runs a headless coding agent as a child process.

    The contract with the process: it receives the request as JSON on stdin and
    must print a JSON result on stdout. Its working directory is the isolated
    area, and nothing outside it is its business.
    """

    command: list[str]
    name: str = "external"
    timeout_slack: int = 120
    env: dict[str, str] = field(default_factory=dict)

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "command": " ".join(self.command[:3])}

    def verify(self) -> None:
        if not self.command:
            raise ValueError("no command configured for the external agent")

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        payload = json.dumps(request.as_dict(), ensure_ascii=False)
        started = time.monotonic()
        import os
        environment = {**os.environ, **self.env}
        try:
            p = subprocess.run(
                self.command, input=payload, cwd=request.path,
                capture_output=True, encoding="utf-8", errors="replace",
                env=environment,
                timeout=request.budget.max_seconds + self.timeout_slack)
        except subprocess.TimeoutExpired:
            return ExecutionResult(
                outcome=Outcome.TIMEBOX,
                summary=f"the process exceeded {request.budget.max_seconds}s",
                duration_seconds=time.monotonic() - started)
        except FileNotFoundError as e:
            return ExecutionResult(outcome=Outcome.ERROR,
                                   summary=f"agent command not found: {e}")

        duration = time.monotonic() - started
        if p.returncode != 0 and not (p.stdout or "").strip():
            return ExecutionResult(
                outcome=Outcome.ERROR,
                summary=f"exit {p.returncode}: {(p.stderr or '').strip()[:300]}",
                duration_seconds=duration)
        try:
            parsed = json.loads((p.stdout or "").strip() or "null")
        except ValueError:
            return ExecutionResult(
                outcome=Outcome.ERROR,
                summary=f"output is not JSON: {(p.stdout or '')[:200]}",
                duration_seconds=duration)
        result = parse_result(parsed)
        if result.duration_seconds:
            return result
        return ExecutionResult(**{**result.__dict__, "duration_seconds": duration}) \
            if not hasattr(result, "__slots__") else _with_duration(result, duration)


def _with_duration(result: ExecutionResult, duration: float) -> ExecutionResult:
    """Rebuild the frozen result with the measured wall time.

    The process may report its own duration; when it does not, the engine's
    measurement is the honest one -- and it is never overwritten when present,
    because the process knows its own internals better than the wrapper does.
    """
    return ExecutionResult(
        outcome=result.outcome, summary=result.summary,
        changed_files=result.changed_files, commits=result.commits,
        test_output=result.test_output, test_command=result.test_command,
        test_exit_code=result.test_exit_code, findings=result.findings,
        blockers=result.blockers, question=result.question,
        cost_usd=result.cost_usd, tokens=result.tokens,
        tool_calls=result.tool_calls, duration_seconds=duration)


@dataclass(slots=True)
class DeterministicAgent(CodingAgent):
    """A NON-model agent, used to prove the harness. Never evidence about an LLM.

    It applies edits declared in configuration, then reports them in the same
    structured shape a real agent must use. Because the edits are declared, every
    run is reproducible -- which is what makes it useful for proving the chain
    and useless for proving intelligence.
    """

    #: task key -> {"edits": {relative path: content}, "outcome": ..., "summary": ...}
    script: dict[str, dict[str, Any]] = field(default_factory=dict)
    fallback: dict[str, Any] = field(default_factory=lambda: {
        "outcome": "NO_PROGRESS", "summary": "no edit declared for this task"})
    name: str = "deterministic"

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "kind": "deterministic-not-a-model"}

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        started = time.monotonic()
        plan = self.script.get(request.task_key, self.fallback)
        area = Path(request.path)

        changed: list[ChangedFile] = []
        for relative, content in (plan.get("edits") or {}).items():
            target = (area / relative).resolve()
            # The isolated area is the whole world. A path escaping it is a
            # defect in whoever wrote the script, and it stops here rather than
            # somewhere in the user's filesystem.
            if not str(target).startswith(str(area.resolve())):
                return ExecutionResult(
                    outcome=Outcome.ERROR,
                    summary=f"refused: '{relative}' escapes the isolated area",
                    duration_seconds=time.monotonic() - started)
            before = target.read_text(encoding="utf-8") if target.is_file() else ""
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            changed.append(ChangedFile(
                path=relative,
                additions=len(content.splitlines()),
                deletions=len(before.splitlines()),
                status="modified" if before else "added"))

        outcome = str(plan.get("outcome", "FINISHED" if changed else "NO_PROGRESS")).upper()
        if outcome not in VALID_OUTCOMES:
            outcome = "ERROR"
        return ExecutionResult(
            outcome=Outcome(outcome),
            summary=str(plan.get("summary", f"{len(changed)} file(s) written")),
            changed_files=tuple(changed),
            question=plan.get("question") if isinstance(plan.get("question"), dict) else None,
            blockers=tuple(str(b) for b in (plan.get("blockers") or [])),
            tool_calls=len(changed),
            duration_seconds=time.monotonic() - started)
