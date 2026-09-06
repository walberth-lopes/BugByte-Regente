# -*- coding: utf-8 -*-
"""The validation loop: implement, test, analyse, fix, test -- with hard limits.

An agent that codes and stops has not finished; it has produced a draft nobody
checked. So the unit of work here is not "the agent ran", it is the loop:

    implement -> test -> passed? -> yes: done for review
                              \\-> no: analyse -> fix -> test

Two rules keep the loop from becoming a money furnace:

**Every limit is hard, and checked before spending, not after.** Iterations,
tool calls, cost and wall time. A budget checked after the call has already been
paid.

**No progress ends the loop, even with budget left.** The same failure twice in
a row means the next attempt is a third identical purchase. The detector
compares failure signatures rather than counting attempts, because
"timeout after 30.2s" and "timeout after 31.7s" are the same failure wearing
different digits.

And the distinction the whole milestone rests on:

    "the agent finished"  !=  "the task is resolved"

The agent reports an `Outcome`. Only this module produces a `Verdict`, and only
from evidence: changes exist, tests are green against a baseline, no policy was
violated. Missing evidence yields `READY_FOR_REVIEW`, which is honest, useful,
and does not lie to whoever reads the queue.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from ..ports.agent import (Budget, CodingAgent, Context, ExecutionRequest,
                           ExecutionResult, Outcome, Permissions, TestResult,
                           Verdict)
from ..ports.repository import RepoRef
from . import testing
from .supervisor import LoopDetector


@dataclass(frozen=True, slots=True)
class Attempt:
    """One turn of the loop. Kept whole so the timeline can be reconstructed."""
    iteration: int
    outcome: Outcome
    summary: str
    changed_files: int
    changed_lines: int
    test_result: TestResult | None
    test_reason: str
    duration_s: float
    cost_usd: float
    tool_calls: int


@dataclass(frozen=True, slots=True)
class LoopResult:
    verdict: Verdict
    reason: str
    attempts: tuple[Attempt, ...] = ()
    changed_files: tuple[str, ...] = ()
    changed_lines: int = 0
    commits: tuple[str, ...] = ()
    test_verdict: testing.TestVerdict | None = None
    baseline: testing.TestRun | None = None
    findings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    question: dict | None = None
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    total_tool_calls: int = 0
    duration_s: float = 0.0
    #: Seconds from loop start to the first attempt that both changed something
    #: and did not break the tests. The metric the engine optimises: useful work
    #: per unit of time, not volume of reasoning.
    time_to_useful_change_s: float | None = None

    @property
    def stopped_early(self) -> bool:
        return self.verdict in (Verdict.BLOCKED, Verdict.NEEDS_HUMAN, Verdict.FAILED)


def measure_baseline(command: list[str] | None, path: str | Path,
                     timeout: int) -> testing.TestRun | None:
    """Snapshot the repository BEFORE the agent touches it.

    Without this, "broke now" cannot be told apart from "was already broken", and
    the engine would either escalate inherited debt or approve a regression. Both
    errors are silent, which is what makes the snapshot worth its cost.
    """
    if not command:
        return None
    return testing.run(command, path, timeout=timeout)


@dataclass(slots=True)
class ValidationLoop:
    agent: CodingAgent
    budget: Budget
    permissions: Permissions
    test_command: list[str] | None = None
    test_timeout: int = 900
    agent_name: str = "coder"
    #: Two identical failures end the loop. Three would buy one more copy of the
    #: same answer.
    loop_detector: LoopDetector = field(default_factory=lambda: LoopDetector(limit=2))

    def execute(
        self,
        run_id: str,
        task_key: str,
        path: str,
        branch: str,
        context: Context,
        repo: RepoRef | None = None,
        baseline: testing.TestRun | None = None,
    ) -> LoopResult:
        started = time.monotonic()
        attempts: list[Attempt] = []
        previous_failures: tuple[str, ...] = ()
        cost, tokens, tool_calls = 0.0, 0, 0
        useful_at: float | None = None
        last: ExecutionResult | None = None
        verdict_of_tests: testing.TestVerdict | None = None

        for iteration in range(1, self.budget.max_iterations + 1):
            elapsed = time.monotonic() - started
            # Checked BEFORE spending: a budget verified after the call has
            # already been paid.
            if elapsed > self.budget.max_seconds:
                return self._finish(Verdict.BLOCKED,
                                    f"timebox: {int(elapsed)}s of {self.budget.max_seconds}s",
                                    attempts, last, verdict_of_tests, baseline,
                                    cost, tokens, tool_calls, started, useful_at)
            if cost > self.budget.max_cost_usd:
                return self._finish(Verdict.BLOCKED,
                                    f"budget: US$ {cost:.2f} of {self.budget.max_cost_usd:.2f}",
                                    attempts, last, verdict_of_tests, baseline,
                                    cost, tokens, tool_calls, started, useful_at)
            if tool_calls > self.budget.max_tool_calls:
                return self._finish(Verdict.BLOCKED,
                                    f"budget: {tool_calls} tool calls of "
                                    f"{self.budget.max_tool_calls}",
                                    attempts, last, verdict_of_tests, baseline,
                                    cost, tokens, tool_calls, started, useful_at)

            request = ExecutionRequest(
                run_id=run_id, task_key=task_key, agent=self.agent_name,
                path=path, branch=branch, context=context,
                permissions=self.permissions, budget=self.budget, repo=repo,
                iteration=iteration, previous_failures=previous_failures)

            turn_started = time.monotonic()
            try:
                result = self.agent.execute(request)
            except Exception as e:   # noqa: BLE001 - an agent must not kill the loop
                result = ExecutionResult(outcome=Outcome.ERROR,
                                         summary=f"{type(e).__name__}: {e}"[:300])
            turn_duration = time.monotonic() - turn_started
            last = result
            cost += result.cost_usd
            tokens += result.tokens
            tool_calls += result.tool_calls

            # The agent reports; the engine judges. It never classifies its own test.
            verdict_of_tests = self._judge_tests(result, path, baseline)

            attempts.append(Attempt(
                iteration=iteration, outcome=result.outcome, summary=result.summary[:200],
                changed_files=len(result.changed_files),
                changed_lines=result.changed_lines,
                test_result=verdict_of_tests.result if verdict_of_tests else None,
                test_reason=verdict_of_tests.reason if verdict_of_tests else "",
                duration_s=turn_duration, cost_usd=result.cost_usd,
                tool_calls=result.tool_calls))

            if (useful_at is None and result.changed_anything
                    and verdict_of_tests is not None
                    and verdict_of_tests.allows_delivery):
                useful_at = time.monotonic() - started

            if result.outcome is Outcome.NEEDS_HUMAN:
                return self._finish(Verdict.NEEDS_HUMAN, result.summary, attempts,
                                    last, verdict_of_tests, baseline, cost, tokens,
                                    tool_calls, started, useful_at)

            if result.outcome is Outcome.ERROR:
                stop = self.loop_detector.repeated("agent_error", result.summary)
                if stop.stop:
                    return self._finish(Verdict.FAILED, stop.reason, attempts, last,
                                        verdict_of_tests, baseline, cost, tokens,
                                        tool_calls, started, useful_at)
                previous_failures = (result.summary[:300],)
                continue

            if result.outcome in (Outcome.TIMEBOX, Outcome.BUDGET):
                return self._finish(Verdict.BLOCKED,
                                    f"the agent stopped on its own: {result.outcome.value}",
                                    attempts, last, verdict_of_tests, baseline,
                                    cost, tokens, tool_calls, started, useful_at)

            if not result.changed_anything:
                stop = self.loop_detector.repeated("no_change", result.summary)
                if stop.stop or result.outcome is Outcome.NO_PROGRESS:
                    return self._finish(Verdict.NO_CHANGE,
                                        "the agent produced no change",
                                        attempts, last, verdict_of_tests, baseline,
                                        cost, tokens, tool_calls, started, useful_at)
                previous_failures = ("nothing changed on the previous attempt",)
                continue

            # --- there are changes: the tests decide -------------------
            if verdict_of_tests is None:
                # No way to test is not a pass. It is a fact the reviewer needs.
                return self._finish(Verdict.READY_FOR_REVIEW,
                                    "changes exist, but this repository has no "
                                    "detectable test command",
                                    attempts, last, None, baseline, cost, tokens,
                                    tool_calls, started, useful_at)

            if verdict_of_tests.result is TestResult.PASSED:
                return self._finish(Verdict.READY_FOR_REVIEW,
                                    "changes exist and the tests are green",
                                    attempts, last, verdict_of_tests, baseline,
                                    cost, tokens, tool_calls, started, useful_at)

            if verdict_of_tests.result is TestResult.PREEXISTING_FAILURE:
                return self._finish(Verdict.READY_FOR_REVIEW,
                                    f"changes exist; the red is inherited "
                                    f"({verdict_of_tests.reason})",
                                    attempts, last, verdict_of_tests, baseline,
                                    cost, tokens, tool_calls, started, useful_at)

            if verdict_of_tests.result in (TestResult.ENVIRONMENT_FAILURE,
                                           TestResult.INFRASTRUCTURE_FAILURE):
                # Not the agent's fault, and not fixable by another iteration.
                # Trying again would only buy the same missing dependency.
                return self._finish(Verdict.BLOCKED, verdict_of_tests.reason,
                                    attempts, last, verdict_of_tests, baseline,
                                    cost, tokens, tool_calls, started, useful_at)

            # Regression or unknown: worth another turn, with the failure named.
            stop = self.loop_detector.repeated("test_failure", verdict_of_tests.reason)
            if stop.stop:
                return self._finish(
                    Verdict.REGRESSED if verdict_of_tests.result is TestResult.REGRESSION
                    else Verdict.BLOCKED,
                    stop.reason, attempts, last, verdict_of_tests, baseline,
                    cost, tokens, tool_calls, started, useful_at)
            previous_failures = tuple(verdict_of_tests.regressions[:8]) or (
                verdict_of_tests.reason,)

        return self._finish(Verdict.BLOCKED,
                            f"exhausted {self.budget.max_iterations} iterations "
                            f"without a green result",
                            attempts, last, verdict_of_tests, baseline, cost,
                            tokens, tool_calls, started, useful_at)

    # ------------------------------------------------------------------
    def _judge_tests(self, result: ExecutionResult, path: str,
                     baseline: testing.TestRun | None) -> testing.TestVerdict | None:
        """Prefer running the tests here over trusting the agent's report.

        The agent may report its own run, and that report is useful context --
        but the engine re-runs when it can. A verdict that depends on the
        subject's self-report is not a verdict.
        """
        if self.test_command:
            after = testing.run(self.test_command, path, timeout=self.test_timeout)
            return testing.classify(after, baseline)
        if result.test_command and result.test_exit_code is not None:
            reported = testing.TestRun(result.test_command, result.test_exit_code,
                                       result.test_output, 0.0)
            return testing.classify(reported, baseline)
        return None

    def _finish(self, verdict, reason, attempts, last, test_verdict, baseline,
                cost, tokens, tool_calls, started, useful_at) -> LoopResult:
        return LoopResult(
            verdict=verdict, reason=reason, attempts=tuple(attempts),
            changed_files=tuple(f.path for f in (last.changed_files if last else ())),
            changed_lines=last.changed_lines if last else 0,
            commits=tuple(last.commits) if last else (),
            test_verdict=test_verdict, baseline=baseline,
            findings=tuple(f"{f.kind}: {f.text}" for f in (last.findings if last else ())),
            blockers=tuple(last.blockers) if last else (),
            question=last.question if last else None,
            total_cost_usd=cost, total_tokens=tokens, total_tool_calls=tool_calls,
            duration_s=time.monotonic() - started,
            time_to_useful_change_s=useful_at)
