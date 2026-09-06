# -*- coding: utf-8 -*-
"""When a discovery may be promoted to VALIDATED, and when a commit may happen.

Two decisions live here, and both are deliberately kept away from the agent and
away from the loop that ran it.

**Promotion is not "the agent managed to work there".** An agent can write into
any repository it is handed; that it succeeded says something about the agent and
nothing about whether the target was right. Confirmation has to come from a fact
the agent could not manufacture: the repository's own tests moved from red to
green on the behaviour the task named. A repository whose tests were already
green cannot be confirmed by them staying green -- nothing was demonstrated, and
promoting on that would launder a guess into a fact.

**A commit is an engine action, never an agent action.** The agent may report
"ready to commit"; that is an observation, exactly as `Outcome` is an observation
and `Verdict` is a conclusion. The authority to write history belongs to the
engine, gated by policy, and the agent's opinion is not part of the condition.

Both rules exist as pure functions so they can be reasoned about, tested and
argued with in one place, rather than being spread as `if`s across the runner.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ports.agent import TestResult, Verdict
from .discovery import Discovery, Source


@dataclass(frozen=True, slots=True)
class Judgement:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


def may_promote_target(discovery: Discovery, loop_result) -> Judgement:
    """May this DISCOVERED target become VALIDATED?

    Every condition is independent of the agent's self-report. The order runs
    cheapest-first, and each refusal names what was missing rather than returning
    a bare no -- a promotion that silently does not happen is indistinguishable
    from one that was never attempted.
    """
    if discovery.source is not Source.DISCOVERED:
        return Judgement(False, f"source is {discovery.source.value}, not DISCOVERED")

    tests = loop_result.test_verdict
    baseline = loop_result.baseline

    # Absence of verification is never success. This is the same rule that
    # governs PREEXISTING_FAILURE, applied to promotion: with no measurement,
    # there is nothing to promote on.
    if tests is None or baseline is None:
        return Judgement(False, "no verification ran; nothing was demonstrated")
    if not baseline.ran:
        return Judgement(False, "the baseline never ran; before and after are not comparable")

    if tests.result is not TestResult.PASSED:
        return Judgement(False, f"tests ended as {tests.result.value}, not PASSED")

    # The sharp one. Green tests over an already-green baseline prove the agent
    # broke nothing -- a fine thing, and no evidence at all about whether this
    # was the right repository.
    if baseline.green:
        return Judgement(False, "the baseline was already green; staying green "
                                "demonstrates nothing about the target")

    if not loop_result.changed_files:
        return Judgement(False, "no file changed; the green cannot be attributed")

    return Judgement(True, "the repository's own tests went from red to green "
                           "after a change in it")


#: The action the engine asks policy about before writing history.
COMMIT_ACTION = "repo.commit"


def may_commit(verdict: Verdict, loop_result) -> Judgement:
    """Does the ENGINE consider this change worth committing?

    Answered before policy is consulted, and without reading the agent's
    opinion. `Outcome.FINISHED` does not appear in this function on purpose: an
    agent declaring itself done is an observation, and observations do not
    authorise writes.
    """
    if not loop_result.changed_files:
        return Judgement(False, "nothing changed; there is nothing to commit")

    if verdict not in (Verdict.RESOLVED, Verdict.READY_FOR_REVIEW):
        return Judgement(False, f"verdict is {verdict.value}")

    tests = loop_result.test_verdict
    if tests is None:
        return Judgement(False, "no test verdict; an unverified change is not "
                                "committed by the engine")
    if tests.result is TestResult.PASSED:
        return Judgement(True, "changes exist and the tests are green")
    if tests.result is TestResult.PREEXISTING_FAILURE:
        # Inherited debt does not block delivery, but it is stated in the reason
        # so the commit is never mistaken for a fully green run.
        return Judgement(True, f"changes exist; the red is inherited "
                               f"({len(tests.preexisting)} preexisting failure(s))")
    return Judgement(False, f"tests ended as {tests.result.value}")
