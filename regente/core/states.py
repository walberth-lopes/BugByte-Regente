# -*- coding: utf-8 -*-
"""The state machine of the work unit.

Two rules that hold for the whole file:

1. **The table is explicit.** There is no "any state goes to any state". A
   transition outside the table raises `InvalidTransition` -- that is what turns
   an orchestration bug into a loud error, instead of a task stranded in a state
   nobody knows how to interpret.

2. **`WAITING_HUMAN` remembers where it came from.** It is not a destination: it
   is a pause. The task returns to its state of origin once the human decides,
   which is why the exit transition is validated against `resumable_from()` and
   not against a fixed list. Without that, approving a deploy would send the
   task back to the beginning.
"""

from __future__ import annotations

from enum import Enum

from .errors import InvalidTransition


class TaskState(str, Enum):
    DISCOVERED = "DISCOVERED"
    ANALYZING = "ANALYZING"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    PR_CREATED = "PR_CREATED"
    CI_RUNNING = "CI_RUNNING"
    AI_REVIEW = "AI_REVIEW"
    WAITING_HUMAN = "WAITING_HUMAN"
    APPROVED = "APPROVED"
    MERGING = "MERGING"
    DEPLOYING = "DEPLOYING"
    QA_STAGING = "QA_STAGING"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


S = TaskState

#: States nothing leaves. Work that lands here is never scheduled again.
TERMINAL: frozenset[TaskState] = frozenset({S.DONE, S.CANCELLED})

#: States in which a live worker exists (or ought to). These are the ones
#: post-crash recovery has to sweep.
ACTIVE: frozenset[TaskState] = frozenset({
    S.ASSIGNED, S.IMPLEMENTING, S.TESTING, S.CI_RUNNING,
    S.AI_REVIEW, S.MERGING, S.DEPLOYING,
})

#: Emergency exits available from any non-terminal state. `WAITING_HUMAN` is
#: here because escalating is always legitimate -- the engine is never left
#: without the option of stopping to ask.
_ESCAPES: frozenset[TaskState] = frozenset({
    S.BLOCKED, S.FAILED, S.CANCELLED, S.WAITING_HUMAN,
})

#: Return to the queue: the worker vanished and the work becomes schedulable
#: again.
#:
#: Without this transition, a task recovered from a crash sits in an active
#: state that no tick ever schedules -- alive on paper and stopped in practice.
#: It is the worst possible failure mode for an engine that promises to resume
#: on its own, because nothing flags it: no error, no queue, just a task that
#: never moves again.
_DEVOLVEM_A_FILA: frozenset[TaskState] = ACTIVE

#: Progress transitions. The emergency exits are added on top afterwards.
_AVANCOS: dict[TaskState, frozenset[TaskState]] = {
    S.DISCOVERED:    frozenset({S.ANALYZING}),
    S.ANALYZING:     frozenset({S.READY}),
    S.READY:         frozenset({S.ASSIGNED}),
    # ASSIGNED goes back to READY when the worker dies before starting: the task
    # loses its owner and returns to the queue without passing through FAILED.
    S.ASSIGNED:      frozenset({S.IMPLEMENTING, S.READY}),
    S.IMPLEMENTING:  frozenset({S.TESTING}),
    # TESTING goes back to IMPLEMENTING in the normal fix cycle: a red test is
    # not an engine failure, it is work.
    S.TESTING:       frozenset({S.PR_CREATED, S.IMPLEMENTING}),
    S.PR_CREATED:    frozenset({S.CI_RUNNING, S.AI_REVIEW}),
    S.CI_RUNNING:    frozenset({S.AI_REVIEW, S.IMPLEMENTING}),
    S.AI_REVIEW:     frozenset({S.APPROVED, S.IMPLEMENTING}),
    S.APPROVED:      frozenset({S.MERGING}),
    # MERGING can go straight to DONE on a project whose deploy the engine does
    # not govern.
    S.MERGING:       frozenset({S.DEPLOYING, S.DONE}),
    S.DEPLOYING:     frozenset({S.QA_STAGING, S.DONE}),
    # A failed QA sends the task back to code -- the most expensive path is also
    # the most common one.
    S.QA_STAGING:    frozenset({S.DONE, S.IMPLEMENTING}),
    # Unblocking/retrying re-enters through analysis: the world moved on while
    # the task was stopped.
    S.BLOCKED:       frozenset({S.READY, S.ANALYZING}),
    S.FAILED:        frozenset({S.READY, S.ANALYZING}),
    S.WAITING_HUMAN: frozenset(),   # the exit is computed, see `resumable_from()`
    S.DONE:          frozenset(),
    S.CANCELLED:     frozenset(),
}


def allowed_from(source: TaskState) -> frozenset[TaskState]:
    """Every legal destination reachable from `source`."""
    if source in TERMINAL:
        return frozenset()
    output = _AVANCOS[source] | (_ESCAPES - {source})
    if source in _DEVOLVEM_A_FILA:
        output |= {S.READY}
    return output


def resumable_from(pausado_em: TaskState) -> frozenset[TaskState]:
    """Legal destinations when leaving WAITING_HUMAN, given where it paused.

    The human can: say carry on (the state of origin itself), say redo it (what
    that state could already reach) or close it out. They cannot teleport the
    task into a state it could not reach on its own -- approving a deploy is not
    the same as declaring the task finished.
    """
    if pausado_em in TERMINAL:
        return frozenset()
    return frozenset({pausado_em}) | _AVANCOS[pausado_em] | (_ESCAPES - {S.WAITING_HUMAN})


def can(source: TaskState, destination: TaskState, pausado_em: TaskState | None = None) -> bool:
    if source is S.WAITING_HUMAN:
        if pausado_em is None:
            # With no memory of where it paused, only the exits that do not
            # depend on it remain. Returning the task to the flow would mean
            # guessing.
            return destination in (_ESCAPES - {S.WAITING_HUMAN})
        return destination in resumable_from(pausado_em)
    return destination in allowed_from(source)


def require(source: TaskState, destination: TaskState, pausado_em: TaskState | None = None) -> None:
    """Validate or raise. The only door through which a transition enters the engine."""
    if not can(source, destination, pausado_em):
        contexto = f" (paused at {pausado_em.value})" if pausado_em else ""
        raise InvalidTransition(
            f"{source.value} -> {destination.value} is not a valid transition{contexto}"
        )


def is_terminal(state: TaskState) -> bool:
    return state in TERMINAL


def is_active(state: TaskState) -> bool:
    return state in ACTIVE
