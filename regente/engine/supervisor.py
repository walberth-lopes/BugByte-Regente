# -*- coding: utf-8 -*-
"""Supervisor: budgets, non-progress detection and the recovery ladder.

Failure is expected. What cannot be tolerated is **failing without moving**: the
agent that rewrites the same file, harvests the same error and tries again burns
an entire budget without producing anything, and the engine has to cut that off
by itself.

The recovery ladder is deliberately finite:

    failed -> retry (backoff) -> change of strategy -> escalate to the human

Each rung has to be *different* from the previous one. Retrying identically after
a deterministic error is just spending money more slowly -- which is why the
change of strategy (another agent, another model) comes before the second
give-up, and not after the fifth identical attempt.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..core.model import Run, RunState, Task, now
from ..core.states import ACTIVE, TaskState


@dataclass(frozen=True, slots=True)
class Budget:
    max_iterations: int = 24
    max_tool_calls: int = 120
    max_cost_usd: float = 5.0
    max_seconds: int = 2700
    #: Attempts per task before escalating. Three rungs: original, retry,
    #: alternative strategy.
    max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class StopVerdict:
    """What the supervisor orders. A closed vocabulary, not free text."""
    stop: bool
    reason: str = ""
    #: 'seguir' | 'retentar' | 'trocar_estrategia' | 'escalar' | 'abortar'
    #: The values stay as they are: the orchestrator branches on them.
    next_action: str = "seguir"


def over_budget(run: Run, orc: Budget, when: datetime | None = None) -> StopVerdict:
    ts = when or now()
    if run.iterations > orc.max_iterations:
        return StopVerdict(True, f"{run.iterations} iterations (cap {orc.max_iterations})", "escalar")
    if run.tool_calls > orc.max_tool_calls:
        return StopVerdict(True, f"{run.tool_calls} tool calls (cap {orc.max_tool_calls})", "escalar")
    if run.cost_usd > orc.max_cost_usd:
        return StopVerdict(True, f"US$ {run.cost_usd:.2f} spent (cap {orc.max_cost_usd:.2f})", "escalar")
    elapsed = (ts - run.started_at).total_seconds()
    if elapsed > orc.max_seconds:
        return StopVerdict(True, f"{int(elapsed)}s elapsed (cap {orc.max_seconds}s)", "trocar_estrategia")
    return StopVerdict(False)


def _signature(text: str) -> str:
    """Reduces a message to a comparable mark.

    Without this, 'timeout after 30.2s' and 'timeout after 31.7s' look like
    different errors and the repetition detector never fires.
    """
    limpo = "".join(c for c in text.lower() if not c.isdigit())
    return hashlib.sha1(" ".join(limpo.split()).encode("utf-8")).hexdigest()[:12]


@dataclass(slots=True)
class LoopDetector:
    """Keeps marks of what already happened in this run and flags repetition.

    Four patterns, all with the same meaning -- the state is not moving: same
    error, same file, same test, same decision.
    """
    limit: int = 3
    _marcas: dict[str, int] = field(default_factory=dict)

    def register(self, kind: str, detail: str) -> int:
        key = f"{kind}:{_signature(detail)}"
        self._marcas[key] = self._marcas.get(key, 0) + 1
        return self._marcas[key]

    def repeated(self, kind: str, detail: str) -> StopVerdict:
        n = self.register(kind, detail)
        if n >= self.limit:
            return StopVerdict(True, f"{kind} repeated {n}x with no progress: {detail[:120]}",
                            "trocar_estrategia")
        return StopVerdict(False)


def no_progress(task: Task, runs: list[Run], window: int = 3) -> StopVerdict:
    """Flags the task that burned several runs and never changed state.

    Comparing state between runs -- and not "did the agent write files?" -- is
    what tells work apart from agitation.
    """
    finished_runs = [r for r in runs if r.state is not RunState.RUNNING][-window:]
    if len(finished_runs) < window:
        return StopVerdict(False)
    if all(r.state in (RunState.FAILED, RunState.ABORTED) for r in finished_runs):
        return StopVerdict(True, f"{window} consecutive runs without leaving {task.state.value}", "escalar")
    return StopVerdict(False)


def next_recovery_step(task: Task, orc: Budget) -> str:
    """The recovery ladder, based on how many times the task has already failed."""
    if task.attempts <= 0:
        return "retentar"
    if task.attempts < orc.max_attempts - 1:
        return "trocar_estrategia"
    return "escalar"


def backoff_delay(attempts: int, base_segundos: int = 60, teto_segundos: int = 1800) -> timedelta:
    """Exponential with a cap. The cap exists so a task does not vanish for hours."""
    return timedelta(seconds=min(teto_segundos, base_segundos * (2 ** max(0, attempts))))


def resume_state(state: TaskState) -> TaskState:
    """Where the task whose worker died goes.

    Always READY -- and READY is the only state the scheduler dispatches from.
    Keeping the task in an active state to "preserve the progress" preserves
    nothing: the progress lives in the work area and in the branch, not in the
    state label, and the task ends up alive on paper and stopped in practice.

    What preserves the partial work is the area being addressed by the task and
    not by the run: the next attempt reopens the same tree, with the WIP commits.
    """
    if state in ACTIVE:
        return TaskState.READY
    return state
