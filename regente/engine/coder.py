# -*- coding: utf-8 -*-
"""The validation loop: run the agent, observe the world, decide.

    run agent -> OBSERVE the work area -> reconcile claims -> test -> verdict
              \\-> retry only if the next attempt would know something new

The loop's shape is not the interesting part. This is:

    the agent's report is never an input to the verdict

Every decision below reads `observation`, which came from `git status` and the
filesystem. `outcome` -- what the agent said -- is carried alongside and used for
exactly two things: comparison, and the timeline. An agent may report three
edited files and a confident `COMPLETE`; if the work area is unchanged, the
verdict is `NO_CHANGE` and the gap is recorded as a discrepancy.

That ordering matters more than it looks. The previous version of this module
branched on `result.changed_anything`, which read the agent's own list. It was
correct-looking code that would have believed anything it was told.

Two other rules hold the loop shut:

**No verifier is not a pass.** A repository with no detectable test command
yields `READY_FOR_REVIEW` with the absence stated, never a green. The agent's own
claim about tests it ran is recorded and decides nothing -- verification by the
subject is not verification.

**A boundary violation ends the mission.** Not a retry, not a warning: an attempt
to write outside the work area or to edit the files that control where code gets
pushed goes to a person, whether or not it succeeded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from ..ports.agent import (AgentRunner, Budget, Claim, ContextPackage, Mission,
                           Outcome, Permissions, ProcessStatus, TestResult,
                           Verdict)
from ..ports.repository import RepoRef
from . import observation as obs
from . import testing
from .budget import RunBudget


@dataclass(frozen=True, slots=True)
class Attempt:
    """One turn of the loop. Kept whole so the timeline can be reconstructed.

    Both columns are here on purpose: what the agent claimed and what the engine
    found. A timeline that recorded only the verdict would lose the evidence
    that the two ever disagreed.
    """
    iteration: int
    status: ProcessStatus
    claim: Claim
    summary: str
    claimed_files: int
    observed_files: int
    observed_lines: int
    test_result: TestResult | None
    test_reason: str
    violations: tuple[str, ...] = ()
    discrepancies: tuple[str, ...] = ()
    duration_s: float = 0.0
    cost_usd: float = 0.0
    tool_calls: int = 0
    retry_reason: str = ""


@dataclass(frozen=True, slots=True)
class LoopResult:
    verdict: Verdict
    reason: str
    attempts: tuple[Attempt, ...] = ()
    #: What the ENGINE observed. Never the agent's list.
    changed_files: tuple[str, ...] = ()
    changed_lines: int = 0
    test_verdict: testing.TestVerdict | None = None
    baseline: testing.TestRun | None = None
    violations: tuple[obs.Violation, ...] = ()
    discrepancies: tuple[obs.Discrepancy, ...] = ()
    findings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    escalation_reason: str = ""
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    total_tool_calls: int = 0
    duration_s: float = 0.0
    #: Seconds to the first attempt that both changed something and did not
    #: break the tests. Useful work per unit of time, not volume of reasoning.
    time_to_useful_change_s: float | None = None

    @property
    def stopped_early(self) -> bool:
        return self.verdict in (Verdict.BLOCKED, Verdict.NEEDS_HUMAN,
                                Verdict.FAILED)

    @property
    def observed_change(self) -> bool:
        return bool(self.changed_files)


def measure_baseline(command: list[str] | None, path: str | Path,
                     timeout: int) -> testing.TestRun | None:
    """Snapshot the repository BEFORE the agent touches it.

    Without this, "broke now" cannot be told apart from "was already broken",
    and the engine would either escalate inherited debt or approve a regression.
    Both errors are silent, which is what makes the snapshot worth its cost.
    """
    if not command:
        return None
    return testing.run(command, path, timeout=timeout)


@dataclass(slots=True)
class ValidationLoop:
    agent: AgentRunner
    budget: Budget
    permissions: Permissions
    test_command: list[str] | None = None
    test_timeout: int = 900
    agent_name: str = "coder"
    #: Directories the agent must not touch, watched across the whole run.
    sentinel: obs.Sentinel | None = None
    forbidden_actions: tuple[str, ...] = ()
    #: Vendor-named paths that confer authority. Supplied by composition; the
    #: engine knows the concept and not the filenames.
    authority_paths: tuple[str, ...] = ()

    def execute(
        self,
        run_id: str,
        task_key: str,
        workspace_id: str,
        workspace_name: str,
        path: str,
        branch: str,
        context: ContextPackage,
        repo: RepoRef | None = None,
        baseline: testing.TestRun | None = None,
    ) -> LoopResult:
        started = time.monotonic()
        wallet = RunBudget(budget=self.budget)
        attempts: list[Attempt] = []
        previous_failures: tuple[str, ...] = ()
        useful_at: float | None = None
        last: Outcome | None = None
        observed = obs.Observation()
        test_verdict: testing.TestVerdict | None = None

        def finish(verdict: Verdict, reason: str) -> LoopResult:
            return self._finish(verdict, reason, attempts, last, observed,
                                test_verdict, baseline, wallet, started,
                                useful_at)

        while True:
            ceiling = wallet.exhausted()
            if not ceiling:
                return finish(Verdict.BLOCKED, ceiling.reason)

            wallet.attempts += 1
            mission = Mission(
                workspace_id=workspace_id, workspace_name=workspace_name,
                task_key=task_key, run_id=run_id, repo=repo, branch=branch,
                allowed_root=path, goal=context.goal, context=context,
                constraints=tuple(i.content or i.ref
                                  for i in context.of_kind("constraint")),
                forbidden_actions=self.forbidden_actions,
                validation_commands=((tuple(self.test_command),)
                                     if self.test_command else ()),
                withheld_authority=self.permissions.withheld(),
                permissions=self.permissions, budget=self.budget,
                agent=self.agent_name, iteration=wallet.attempts,
                previous_failures=previous_failures)

            turn_started = time.monotonic()
            try:
                outcome = self.agent.run(mission)
            except Exception as e:   # noqa: BLE001 - an agent must not kill the loop
                outcome = Outcome(status=ProcessStatus.ERROR,
                                  summary=f"{type(e).__name__}: {e}"[:300])
            turn_duration = time.monotonic() - turn_started
            last = outcome
            wallet.spend(outcome)

            # --- the engine looks for itself ---------------------------
            observed = obs.reconcile(
                obs.observe(path, expected_branch=branch, sentinel=self.sentinel,
                            authority_paths=self.authority_paths),
                outcome)

            test_verdict = self._judge_tests(path, baseline, observed)

            attempts.append(Attempt(
                iteration=wallet.attempts, status=outcome.status,
                claim=outcome.claim, summary=outcome.summary[:200],
                claimed_files=len(outcome.claimed_files),
                observed_files=len(observed.files),
                observed_lines=observed.changed_lines,
                test_result=test_verdict.result if test_verdict else None,
                test_reason=test_verdict.reason if test_verdict else "",
                violations=tuple(f"{v.kind}: {v.detail}"
                                 for v in observed.violations),
                discrepancies=tuple(f"{d.kind}: {d.detail}"
                                    for d in observed.discrepancies),
                duration_s=turn_duration, cost_usd=outcome.cost_usd,
                tool_calls=outcome.tool_calls))

            # --- a boundary breach ends it, successful or not ----------
            if observed.violations:
                return finish(
                    Verdict.NEEDS_HUMAN,
                    "the agent reached outside its authority: " +
                    "; ".join(f"{v.kind} {v.path}".strip()
                              for v in observed.violations[:5]))

            # --- the engine could not look -----------------------------
            if not observed.knows:
                return finish(
                    Verdict.NEEDS_HUMAN,
                    f"the work area could not be observed ({observed.unavailable}); "
                    f"absence of verification is not success")

            if (useful_at is None and observed.changed_anything
                    and test_verdict is not None and test_verdict.allows_delivery):
                useful_at = time.monotonic() - started

            if outcome.status is ProcessStatus.NEEDS_HUMAN or outcome.escalation_requested:
                return finish(Verdict.NEEDS_HUMAN,
                              outcome.escalation_reason or outcome.summary or
                              "the agent asked for a person")

            failure = self._failure_signature(outcome, observed, test_verdict)
            wallet.record(outcome, observed.changed_anything, failure)

            # --- did anything actually happen? -------------------------
            if not observed.changed_anything:
                decision = wallet.may_retry(outcome, False, failure)
                attempts[-1] = _with_retry_reason(attempts[-1], decision.reason)
                if not decision:
                    return finish(
                        Verdict.NO_CHANGE,
                        f"the work area is unchanged"
                        + (f" while the agent claimed "
                           f"{len(outcome.claimed_files)} changed file(s)"
                           if outcome.claims_a_change else "")
                        + f"; {decision.reason}")
                previous_failures = ("the previous attempt changed nothing in "
                                     "the work area",)
                continue

            # --- there are real changes: the tests decide --------------
            if test_verdict is None:
                # No way to verify is not a pass. It is a fact for the reviewer.
                return finish(
                    Verdict.READY_FOR_REVIEW,
                    "changes exist and nothing verified them: this repository "
                    "has no detectable test command, so no claim is made about "
                    "whether they work")

            if test_verdict.result is TestResult.PASSED:
                return finish(Verdict.READY_FOR_REVIEW,
                              "changes exist and the tests are green")

            if test_verdict.result is TestResult.PREEXISTING_FAILURE:
                return finish(Verdict.READY_FOR_REVIEW,
                              f"changes exist; the red is inherited "
                              f"({test_verdict.reason})")

            if test_verdict.result in (TestResult.ENVIRONMENT_FAILURE,
                                       TestResult.INFRASTRUCTURE_FAILURE):
                # Not the agent's fault and not fixable by another iteration.
                # Trying again would only buy the same missing dependency.
                return finish(Verdict.BLOCKED, test_verdict.reason)

            # Regression or unknown: another turn only if it would know more.
            wallet.validation_retries += 1
            decision = wallet.may_retry(outcome, True, failure)
            attempts[-1] = _with_retry_reason(attempts[-1], decision.reason)
            if not decision:
                return finish(
                    Verdict.REGRESSED
                    if test_verdict.result is TestResult.REGRESSION
                    else Verdict.BLOCKED,
                    f"{test_verdict.reason}; {decision.reason}")
            previous_failures = tuple(test_verdict.regressions[:8]) or (
                test_verdict.reason,)

    # ------------------------------------------------------------------
    def _judge_tests(self, path: str, baseline: testing.TestRun | None,
                     observed: obs.Observation) -> testing.TestVerdict | None:
        """Run the tests HERE. Never read the agent's account of its own run.

        The agent's claimed tests are kept in the outcome and shown in the
        timeline, and they are not consulted. A verdict that depends on the
        subject's self-report is not a verdict, and the previous version of this
        method fell back to exactly that when the engine had no command of its
        own -- which is the moment the fallback mattered most and was worth
        least.
        """
        if not self.test_command or not observed.knows:
            return None
        after = testing.run(self.test_command, path, timeout=self.test_timeout)
        return testing.classify(after, baseline)

    @staticmethod
    def _failure_signature(outcome: Outcome, observed: obs.Observation,
                           verdict: testing.TestVerdict | None) -> str:
        if verdict is not None and verdict.result not in (
                TestResult.PASSED, TestResult.PREEXISTING_FAILURE):
            return f"{verdict.result.value}:{verdict.reason}"
        if outcome.status is ProcessStatus.ERROR:
            return f"agent_error:{outcome.summary}"
        if not observed.changed_anything:
            return "no_change"
        return ""

    def _finish(self, verdict, reason, attempts, last, observed, test_verdict,
                baseline, wallet: RunBudget, started, useful_at) -> LoopResult:
        return LoopResult(
            verdict=verdict, reason=reason, attempts=tuple(attempts),
            changed_files=observed.paths(),
            changed_lines=observed.changed_lines,
            test_verdict=test_verdict, baseline=baseline,
            violations=observed.violations, discrepancies=observed.discrepancies,
            findings=tuple(f"{f.kind}: {f.text}"
                           for f in (last.findings if last else ())),
            blockers=tuple(last.blockers) if last else (),
            questions=tuple(last.questions) if last else (),
            escalation_reason=last.escalation_reason if last else "",
            total_cost_usd=wallet.cost_usd, total_tokens=wallet.tokens,
            total_tool_calls=wallet.tool_calls,
            duration_s=time.monotonic() - started,
            time_to_useful_change_s=useful_at)


def _with_retry_reason(attempt: Attempt, reason: str) -> Attempt:
    return Attempt(
        iteration=attempt.iteration, status=attempt.status, claim=attempt.claim,
        summary=attempt.summary, claimed_files=attempt.claimed_files,
        observed_files=attempt.observed_files,
        observed_lines=attempt.observed_lines, test_result=attempt.test_result,
        test_reason=attempt.test_reason, violations=attempt.violations,
        discrepancies=attempt.discrepancies, duration_s=attempt.duration_s,
        cost_usd=attempt.cost_usd, tool_calls=attempt.tool_calls,
        retry_reason=reason)
