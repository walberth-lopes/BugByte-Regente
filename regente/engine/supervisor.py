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
    #: 'follow' | 'retry' | 'change_strategy' | 'escalate' | 'abort'
    #: A closed vocabulary the orchestrator branches on. It reaches storage
    #: only inside free-text reasons and summaries, so renaming it needed no
    #: migration: old rows keep the words they were written with, which is
    #: what an append-only history is for.
    next_action: str = "follow"


def over_budget(run: Run, budget: Budget, when: datetime | None = None) -> StopVerdict:
    ts = when or now()
    if run.iterations > budget.max_iterations:
        return StopVerdict(True, f"{run.iterations} iterations (cap {budget.max_iterations})", "escalate")
    if run.tool_calls > budget.max_tool_calls:
        return StopVerdict(True, f"{run.tool_calls} tool calls (cap {budget.max_tool_calls})", "escalate")
    if run.cost_usd > budget.max_cost_usd:
        return StopVerdict(True, f"US$ {run.cost_usd:.2f} spent (cap {budget.max_cost_usd:.2f})", "escalate")
    elapsed = (ts - run.started_at).total_seconds()
    if elapsed > budget.max_seconds:
        return StopVerdict(True, f"{int(elapsed)}s elapsed (cap {budget.max_seconds}s)", "change_strategy")
    return StopVerdict(False)


def _signature(text: str) -> str:
    """Reduces a message to a comparable mark.

    Without this, 'timeout after 30.2s' and 'timeout after 31.7s' look like
    different errors and the repetition detector never fires.
    """
    cleaned = "".join(c for c in text.lower() if not c.isdigit())
    return hashlib.sha1(" ".join(cleaned.split()).encode("utf-8")).hexdigest()[:12]


@dataclass(slots=True)
class LoopDetector:
    """Keeps marks of what already happened in this run and flags repetition.

    Four patterns, all with the same meaning -- the state is not moving: same
    error, same file, same test, same decision.
    """
    limit: int = 3
    _marks: dict[str, int] = field(default_factory=dict)

    def register(self, kind: str, detail: str) -> int:
        key = f"{kind}:{_signature(detail)}"
        self._marks[key] = self._marks.get(key, 0) + 1
        return self._marks[key]

    def repeated(self, kind: str, detail: str) -> StopVerdict:
        n = self.register(kind, detail)
        if n >= self.limit:
            return StopVerdict(True, f"{kind} repeated {n}x with no progress: {detail[:120]}",
                            "change_strategy")
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
        return StopVerdict(True, f"{window} consecutive runs without leaving {task.state.value}", "escalate")
    return StopVerdict(False)


def next_recovery_step(task: Task, budget: Budget) -> str:
    """The recovery ladder, based on how many times the task has already failed."""
    if task.attempts <= 0:
        return "retry"
    if task.attempts < budget.max_attempts - 1:
        return "change_strategy"
    return "escalate"


def backoff_delay(attempts: int, base_seconds: int = 60, cap_seconds: int = 1800) -> timedelta:
    """Exponential with a cap. The cap exists so a task does not vanish for hours."""
    return timedelta(seconds=min(cap_seconds, base_seconds * (2 ** max(0, attempts))))


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
