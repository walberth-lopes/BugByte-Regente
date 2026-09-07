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
_RETURN_TO_QUEUE: frozenset[TaskState] = ACTIVE

#: Progress transitions. The emergency exits are added on top afterwards.
_ADVANCES: dict[TaskState, frozenset[TaskState]] = {
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


#: States this engine currently has code to move a task OUT of.
#:
#: Not the same thing as `_ADVANCES`, which says which transitions are *legal*.
#: A transition can be perfectly legal and have nobody who performs it, and that
#: gap is invisible: the task sits in a busy-looking state, the scheduler skips
#: it because it appears to be in progress, and the engine reports quiet, clean
#: ticks forever.
#:
#: That is not hypothetical. A soak run found every successfully dispatched task
#: parked in `TESTING` on its fourth tick -- legal to leave, and no code in the
#: project leaving it. The pipeline beyond `TESTING` belongs to milestones that
#: do not exist yet, so the honest thing is for the engine to know where its own
#: road ends and say so, rather than drive tasks off it.
#:
#: When a later milestone adds the stage that advances a state, it adds the
#: state here. `test_states.py` checks the two sets against each other, so
#: forgetting is loud.
ENGINE_ADVANCES: frozenset[TaskState] = frozenset({
    S.DISCOVERED, S.READY, S.ASSIGNED, S.IMPLEMENTING, S.FAILED, S.BLOCKED,
    S.WAITING_HUMAN,
})


def engine_can_advance(state: TaskState) -> bool:
    return state in ENGINE_ADVANCES


def is_terminus(state: TaskState) -> bool:
    """An active state the engine can enter and cannot leave.

    The dangerous shape: it looks like work in progress and it is a dead end.
    """
    return state in ACTIVE and state not in ENGINE_ADVANCES


def allowed_from(source: TaskState) -> frozenset[TaskState]:
    """Every legal destination reachable from `source`."""
    if source in TERMINAL:
        return frozenset()
    output = _ADVANCES[source] | (_ESCAPES - {source})
    if source in _RETURN_TO_QUEUE:
        output |= {S.READY}
    return output


def resumable_from(paused_at: TaskState) -> frozenset[TaskState]:
    """Legal destinations when leaving WAITING_HUMAN, given where it paused.

    The human can: say carry on (the state of origin itself), say redo it (what
    that state could already reach), send it back to the queue, or close it out.
    They cannot teleport the task into a state it could not reach on its own --
    approving a deploy is not the same as declaring the task finished.

    Returning to the queue was missing here, and the omission ran against what
    the rule itself says: `allowed_from` already lets any active state go back
    to READY, so escalating a task REDUCED the human's options below the ones
    the engine had on its own. In practice that closed the only useful path
    after an escalation -- saying redo it -- and the decision died with
    InvalidTransition. Found by a long run, not by reading.
    """
    if paused_at in TERMINAL:
        return frozenset()
    exits = (frozenset({paused_at}) | _ADVANCES[paused_at]
             | (_ESCAPES - {S.WAITING_HUMAN}))
    if paused_at in _RETURN_TO_QUEUE:
        exits |= {S.READY}
    return exits


def can(source: TaskState, destination: TaskState, paused_at: TaskState | None = None) -> bool:
    if source is S.WAITING_HUMAN:
        if paused_at is None:
            # With no memory of where it paused, only the exits that do not
            # depend on it remain. Returning the task to the flow would mean
            # guessing.
            return destination in (_ESCAPES - {S.WAITING_HUMAN})
        return destination in resumable_from(paused_at)
    return destination in allowed_from(source)


def require(source: TaskState, destination: TaskState, paused_at: TaskState | None = None) -> None:
    """Validate or raise. The only door through which a transition enters the engine."""
    if not can(source, destination, paused_at):
        context = f" (paused at {paused_at.value})" if paused_at else ""
        raise InvalidTransition(
            f"{source.value} -> {destination.value} is not a valid transition{context}"
        )


def is_terminal(state: TaskState) -> bool:
    return state in TERMINAL


def is_active(state: TaskState) -> bool:
    return state in ACTIVE
