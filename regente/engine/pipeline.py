# -*- coding: utf-8 -*-
"""What happens after the engine's own verdict: push, pull request, CI.

`TESTING` was where this engine's road ended. Every task that succeeded parked
there, the scheduler skipped it because it looked busy, and every later tick came
back clean. Milestone 8 made the engine say so instead of pretending; this module
is the road.

    verdict -> push -> PR_CREATED -> CI_RUNNING -> observed result

Each step is gated on the previous one having been *observed* to happen, never on
it having *appeared* to. The rule from every earlier milestone holds here, and is
why this file is short and suspicious:

    agent outcome != validation != CI result != review != task resolution

Two properties matter more than the sequence.

**Every stage revalidates.** Policy, identity and SHA are checked before each
remote mutation, not once at the start. Validation and mutation are separated in
time and the world moves in between.

**Every stage is idempotent.** A process can die between a remote mutation and
the row that records it; the local state then says the mutation never happened
while the remote says it did. So before repeating anything the engine asks the
remote what already exists. Assuming success loses work, assuming failure
duplicates it, and one of those two guesses writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable

from ..core.model import now
from ..core.states import TaskState
from ..ports.agent import Verdict
from ..ports.store import Store
from . import ci as ci_module
from .remote import RemoteDelivery, RemoteIdentity, Refused


class Step(str, Enum):
    """How far a delivery got. Each value is a fact on disk, not a hope."""
    NOT_STARTED = "NOT_STARTED"
    PUSHED = "PUSHED"
    PULL_REQUEST = "PULL_REQUEST"
    CI_OBSERVED = "CI_OBSERVED"
    REFUSED = "REFUSED"


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """What the delivery achieved, and why it stopped where it did."""
    step: Step
    reason: str
    delivery_id: str = ""
    commit_sha: str = ""
    push_target: str = ""
    pull_request: int | None = None
    pull_request_url: str = ""
    ci: ci_module.CIObservation | None = None
    #: Steps that were already done remotely when this run picked the work up.
    #: Empty on a first attempt, non-empty on a resume -- and the difference is
    #: worth keeping, because a resume that silently looks like a first attempt
    #: is a duplicate waiting to be counted as one.
    resumed: tuple[str, ...] = ()
    task_state: TaskState | None = None

    @property
    def delivered(self) -> bool:
        return self.step in (Step.PULL_REQUEST, Step.CI_OBSERVED)


@dataclass(slots=True)
class DeliveryStage:
    """Drives one task from a validated verdict to an observed CI result.

    Holds no authority of its own. Every mutation goes through `RemoteDelivery`,
    which asks the policy engine again each time. What this decides is whether
    the *evidence* justifies attempting the next step -- a different question
    from whether the step is permitted, and both have to be answered.
    """
    #: The states this stage walks, in order. It exists to tell two different
    #: situations apart, which a plain "is it already there?" check cannot:
    #: a **resume**, where the task is further along the same road because an
    #: earlier attempt got there, and a task that has **left the road**, moved
    #: by a human or another part of the engine while this delivery worked.
    #: The first is nothing to do. The second is somebody else's decision, and
    #: dragging the task back onto the road would overwrite it.
    ROAD = (TaskState.TESTING, TaskState.PR_CREATED, TaskState.CI_RUNNING)

    store: Store
    delivery: RemoteDelivery
    workspace_id: str
    clock: Callable[[], datetime] = now
    #: The branch a pull request targets. Configured, never guessed: a base
    #: inferred from a name is how a change lands on the wrong line of work.
    base_branch: str = "main"

    # ------------------------------------------------------------------
    def advance(self, identity: RemoteIdentity, area, verdict: Verdict,
                task_id: str, title: str, body: str = "",
                risk: str = "LOW") -> DeliveryOutcome:
        """Take the work as far as the evidence and the policy allow."""
        if not self._verdict_allows(verdict):
            return DeliveryOutcome(
                Step.REFUSED,
                f"verdict is {verdict.value}; only a change the engine itself "
                f"validated is delivered")

        row = self._open_or_resume(identity, task_id)
        resumed: list[str] = []

        # --- push -------------------------------------------------------
        try:
            pushed = self.delivery.push(area, identity, row, risk=risk)
        except Refused as e:
            return DeliveryOutcome(Step.REFUSED, str(e), delivery_id=row,
                                   commit_sha=identity.commit_sha)
        if pushed.already_done:
            resumed.append("push")

        # --- pull request -----------------------------------------------
        try:
            pull_request = self.delivery.open_pull_request(
                area, identity, row, base=self.base_branch, title=title,
                body=body, risk=risk)
        except Refused as e:
            return DeliveryOutcome(Step.PUSHED, str(e), delivery_id=row,
                                   commit_sha=identity.commit_sha,
                                   push_target=pushed.target,
                                   resumed=tuple(resumed))

        try:
            self._move(task_id, TaskState.PR_CREATED,
                       f"pull request #{pull_request.number}")
        except Refused as e:
            # The remote mutation happened and is recorded; the task is no
            # longer ours to move. Both facts are reported.
            return DeliveryOutcome(Step.PULL_REQUEST, str(e), delivery_id=row,
                                   commit_sha=identity.commit_sha,
                                   push_target=pushed.target,
                                   pull_request=pull_request.number,
                                   pull_request_url=pull_request.url,
                                   resumed=tuple(resumed),
                                   task_state=self._state_of(task_id))

        # --- CI ----------------------------------------------------------
        self._move(task_id, TaskState.CI_RUNNING,
                   f"observing checks for {identity.commit_sha[:12]}")
        observation = self.delivery.observe_ci(identity, row)

        return DeliveryOutcome(
            Step.CI_OBSERVED,
            observation.state.value
            + (f" / {observation.result.value}" if observation.result else ""),
            delivery_id=row, commit_sha=identity.commit_sha,
            push_target=pushed.target, pull_request=pull_request.number,
            pull_request_url=pull_request.url, ci=observation,
            resumed=tuple(resumed), task_state=self._state_of(task_id))

    # ------------------------------------------------------------------
    @staticmethod
    def _verdict_allows(verdict: Verdict) -> bool:
        """Only the engine's own conclusion opens this door.

        `READY_FOR_REVIEW` is the honest verdict for a change that exists and
        passed what could be run; `RESOLVED` demands complete evidence and is
        rarer. Everything else -- regressed, blocked, no change, needs a human --
        must never reach a remote, and listing the two that may is safer than
        listing the many that may not.
        """
        return verdict in (Verdict.RESOLVED, Verdict.READY_FOR_REVIEW)

    def _open_or_resume(self, identity: RemoteIdentity, task_id: str) -> str:
        """One delivery row per (task, run, commit). Reused on a resume.

        Opening a second row for work already in flight would split the ledger:
        two rows for one pull request, and no way to answer "what happened to
        this commit" without choosing which row to believe.
        """
        for existing in self.store.deliveries(identity.workspace_id,
                                              identity.task_key):
            if (existing["run_id"] == identity.run_id
                    and existing["commit_sha"] == identity.commit_sha):
                return existing["id"]
        return self.store.open_delivery(
            identity.workspace_id, identity.task_key, identity.run_id,
            identity.repo.provider, identity.repo.key, identity.branch,
            identity.commit_sha)

    def _move(self, task_id: str, destination: TaskState, reason: str) -> None:
        """Advance the task along the road, or refuse to touch it.

        Three outcomes, and the difference between them is the whole point:

        * **already there or further along the road** -- a resumed delivery
          re-walks states an earlier attempt reached. Nothing to do. Treating
          this as an error would make every resume fail on its second step;
          treating it as a reason to *move backwards* would undo real progress.
        * **on the road, behind the destination** -- the normal case: transition.
        * **anywhere else** -- somebody moved this task while the delivery ran.
          Refuse. The engine is the transition authority, and that authority is
          exactly what would be exercised wrongly by forcing the task back onto
          a road whose owner already left it.
        """
        current = self.store.task(task_id, self.workspace_id)
        if current is None:
            raise Refused(
                f"task {task_id} is not readable in this workspace; the "
                f"delivery will not move a task it cannot see")
        if current.state is destination:
            return
        if current.state not in self.ROAD:
            raise Refused(
                f"the task left the delivery road while this ran: it is "
                f"{current.state.value}, not on the way to {destination.value}. "
                f"Whoever moved it decided something; the delivery will not "
                f"overwrite that decision")
        if self.ROAD.index(current.state) > self.ROAD.index(destination):
            return           # a resume, already further along
        self.store.transition(task_id, destination, actor="pipeline",
                              reason=reason, workspace_id=self.workspace_id)

    def _state_of(self, task_id: str) -> TaskState | None:
        task = self.store.task(task_id, self.workspace_id)
        return task.state if task else None
