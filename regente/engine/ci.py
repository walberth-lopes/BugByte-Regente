# -*- coding: utf-8 -*-
"""Classifying what CI said, and refusing to classify what it did not say.

Deliberately mirrors `engine/testing.py`. The distinctions are identical -- a red
check has the same five possible causes as a red local test -- and inventing a
second taxonomy would guarantee the two drift until nobody can say which one a
verdict came from.

Three conditions are NOT results, and keeping them out of the result type is the
point of this module:

  - **pending**: checks are still running. The absence of an answer.
  - **unavailable**: the provider could not be read. Also the absence of an
    answer, arriving by a different route.
  - **no checks**: the provider confirmed there is nothing to run.

Only the third is a fact about the change; the first two are facts about the
conversation. Folding any of them into `PASSED` is how a network blip becomes a
clean bill of health.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..ports.agent import TestResult
from ..ports.delivery import PipelineStatus


class CIState(str, Enum):
    """Where the observation stands. Not the same thing as a result."""
    PENDING = "PENDING"            # still running; ask again
    UNAVAILABLE = "UNAVAILABLE"    # could not be read; ask again, then escalate
    NO_CHECKS = "NO_CHECKS"        # confirmed: nothing runs here
    CONCLUDED = "CONCLUDED"        # there is a result to classify

    @property
    def is_answer(self) -> bool:
        return self in (CIState.NO_CHECKS, CIState.CONCLUDED)


@dataclass(frozen=True, slots=True)
class CIObservation:
    state: CIState
    #: Only meaningful when `state is CONCLUDED`.
    result: TestResult | None = None
    reason: str = ""
    green: tuple[str, ...] = ()
    red: tuple[str, ...] = ()
    running: tuple[str, ...] = ()
    #: Checks that were already red on the base commit. Inherited, not caused.
    preexisting: tuple[str, ...] = ()
    regressions: tuple[str, ...] = ()
    url: str = ""

    @property
    def allows_progress(self) -> bool:
        """May the task move on from CI_RUNNING?

        `NO_CHECKS` allows progress and proves nothing -- the two are not in
        tension. The engine advances because there is nothing left to wait for,
        and records that no verification happened so no later reader mistakes it
        for evidence.
        """
        if self.state is CIState.NO_CHECKS:
            return True
        if self.state is not CIState.CONCLUDED:
            return False
        return self.result in (TestResult.PASSED, TestResult.PREEXISTING_FAILURE)

    @property
    def condemns_the_change(self) -> bool:
        return (self.state is CIState.CONCLUDED
                and self.result is TestResult.REGRESSION)


#: Check names whose failure is about the environment rather than the change.
#: Matched on the check's own name because that is all the provider gives
#: without fetching logs, and fetching logs for every red check would make
#: observation cost more than the work.
ENVIRONMENT_CHECK_MARKS = (
    "setup", "install", "provision", "bootstrap", "dependencies", "docker",
    "credential", "auth", "login",
)


def _looks_environmental(names: tuple[str, ...]) -> bool:
    return bool(names) and all(
        any(mark in n.lower() for mark in ENVIRONMENT_CHECK_MARKS) for n in names)


def classify(status: PipelineStatus,
             baseline: PipelineStatus | None = None) -> CIObservation:
    """`baseline` is the same repository's checks on the BASE commit.

    Without it a regression cannot be claimed, exactly as with local tests. A red
    check with no baseline is `UNKNOWN`: honest, and it approves nothing.
    """
    green = tuple(c.name for c in status.checks if c.green)
    red = tuple(c.name for c in status.checks if c.red)
    running = status.running

    if status.confirmed_no_checks:
        return CIObservation(
            CIState.NO_CHECKS,
            reason="the provider confirmed there is no check for this commit; "
                   "nothing was verified",
            url=status.url)

    if running:
        return CIObservation(CIState.PENDING,
                             reason=f"{len(running)} check(s) still running",
                             green=green, red=red, running=running, url=status.url)

    if status.all_green:
        return CIObservation(CIState.CONCLUDED, result=TestResult.PASSED,
                             reason=f"{len(green)} check(s) passed",
                             green=green, url=status.url)

    if _looks_environmental(red):
        return CIObservation(
            CIState.CONCLUDED, result=TestResult.ENVIRONMENT_FAILURE,
            reason=f"only environment checks failed: {', '.join(red)}",
            green=green, red=red, url=status.url)

    if baseline is None:
        return CIObservation(
            CIState.CONCLUDED, result=TestResult.UNKNOWN,
            reason="red with no baseline: a regression cannot be claimed",
            green=green, red=red, url=status.url)

    was_red = {c.name for c in baseline.checks if c.red}
    regressions = tuple(sorted(set(red) - was_red))
    preexisting = tuple(sorted(set(red) & was_red))

    if regressions:
        return CIObservation(
            CIState.CONCLUDED, result=TestResult.REGRESSION,
            reason=f"{len(regressions)} check(s) were green on the base and broke",
            green=green, red=red, preexisting=preexisting,
            regressions=regressions, url=status.url)

    return CIObservation(
        CIState.CONCLUDED, result=TestResult.PREEXISTING_FAILURE,
        reason=f"{len(preexisting)} check(s) were already red on the base",
        green=green, red=red, preexisting=preexisting, url=status.url)


def unavailable(reason: str) -> CIObservation:
    """The provider could not be read. Never a result -- ask again."""
    return CIObservation(CIState.UNAVAILABLE, reason=reason)


@dataclass(frozen=True, slots=True)
class WaitPolicy:
    """How long the engine waits for an answer before asking a human.

    Both budgets exist because the two failures look identical from inside a
    loop and need opposite responses: checks that never finish mean something is
    wrong with the pipeline, while a provider that never answers means something
    is wrong with the connection.
    """
    max_pending_seconds: int = 1800
    max_unavailable_attempts: int = 5

    def exhausted(self, pending_seconds: float, unavailable_attempts: int) -> str:
        if pending_seconds > self.max_pending_seconds:
            return (f"checks pending for {int(pending_seconds)}s, over the "
                    f"{self.max_pending_seconds}s budget")
        if unavailable_attempts >= self.max_unavailable_attempts:
            return (f"the provider was unreadable {unavailable_attempts} times; "
                    f"unavailability is not a CI result and will not be treated "
                    f"as one")
        return ""
