# -*- coding: utf-8 -*-
"""Coding agent contract. Structured in, structured out.

The rule that defines this file: **the engine never learns what happened by
reading free text.** An agent that writes "done!" has concluded nothing. What
concludes is a changed file, a green test and a satisfied criterion. So the
result is an object with required fields, not a narrative.

The distinction that is easiest to lose, and most expensive to lose:

    "the agent finished"  !=  "the task is resolved"

The first is a fact about a process; the second is a claim about the world, and
it rests on evidence the agent cannot award itself. That is why `Outcome` (what
the process did) and `Verdict` (what the engine concludes) are two different
types, and only the engine produces the second.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import Capability, Port
from .repository import RepoRef


class Outcome(str, Enum):
    """How the agent's PROCESS ended. A fact, not a judgement.

    Closed vocabulary on purpose: the engine picks the next move from this, and
    free text would force it to guess.
    """
    FINISHED = "FINISHED"              # the agent says it is done
    NO_PROGRESS = "NO_PROGRESS"        # it ran and changed nothing
    TIMEBOX = "TIMEBOX"                # ran out of time
    BUDGET = "BUDGET"                  # ran out of cost / iterations / tool calls
    NEEDS_HUMAN = "NEEDS_HUMAN"        # it stopped and asked
    ERROR = "ERROR"                    # the process broke


class TestResult(str, Enum):
    """Why a test is the colour it is.

    A red test does NOT mean a bad agent. Five causes produce the same colour,
    and confusing them costs in both directions: punishing the agent for a
    missing `pip install`, or approving a regression believing it was the
    environment.
    """
    PASSED = "PASSED"
    #: Was failing BEFORE the change and still fails. Inherited debt.
    PREEXISTING_FAILURE = "PREEXISTING_FAILURE"
    #: Passed before, broken now. The only one that condemns the change.
    REGRESSION = "REGRESSION"
    #: Missing dependency, credential, service down. Not the code's fault.
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
    #: The test runner itself did not run: missing binary, wrong command.
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
    #: It ran, but could not be classified. Never becomes PASSED out of optimism.
    UNKNOWN = "UNKNOWN"

    @property
    def condemns_the_change(self) -> bool:
        return self is TestResult.REGRESSION

    @property
    def proves_quality(self) -> bool:
        return self is TestResult.PASSED


class Verdict(str, Enum):
    """What the ENGINE concludes about the task. Only the engine produces this.

    `RESOLVED` demands complete evidence. When in doubt, `READY_FOR_REVIEW` --
    which is honest, useful, and does not lie to whoever reads the queue.
    """
    RESOLVED = "RESOLVED"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    NO_CHANGE = "NO_CHANGE"
    REGRESSED = "REGRESSED"
    BLOCKED = "BLOCKED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class Permissions:
    """What this worker may even attempt.

    Does not replace the Policy Engine -- that still judges every call. This
    avoids offering the agent what it could never use, which saves a whole cycle
    of attempt-and-refusal.
    """
    read: bool = True
    write_code: bool = False
    run_tests: bool = False
    commit: bool = False
    push: bool = False
    open_pr: bool = False
    network: bool = False

    def as_list(self) -> tuple[str, ...]:
        return tuple(c for c in ("read", "write_code", "run_tests", "commit",
                                 "push", "open_pr", "network")
                     if getattr(self, c))


@dataclass(frozen=True, slots=True)
class Budget:
    max_iterations: int = 6
    max_tool_calls: int = 120
    max_cost_usd: float = 5.0
    max_seconds: int = 1800


@dataclass(frozen=True, slots=True)
class Context:
    """The minimum that suffices. The agent investigates the rest with its tools.

    There is deliberately no heavy pre-analysis here: saving the agent some
    investigation masks the behaviour we want to measure, and moves work to the
    Orchestrator, which does it worse.
    """
    goal: str
    acceptance_criteria: tuple[str, ...] = ()
    repository: str = ""
    base_branch: str = ""
    relevant_files: tuple[str, ...] = ()
    architecture: str = ""
    related_tasks: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    existing_tests: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    #: Where the certainty about the repository came from. It travels with the
    #: request so the agent can DISAGREE -- and disagreement backed by evidence
    #: is a finding, not insubordination.
    target_evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "acceptance_criteria": list(self.acceptance_criteria),
            "repository": self.repository,
            "base_branch": self.base_branch,
            "relevant_files": list(self.relevant_files),
            "architecture": self.architecture,
            "related_tasks": list(self.related_tasks),
            "dependencies": list(self.dependencies),
            "existing_tests": list(self.existing_tests),
            "constraints": list(self.constraints),
            "target_evidence": list(self.target_evidence),
        }


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """input: task, workspace, context, permissions, budget, timeout."""
    run_id: str
    task_key: str
    agent: str
    path: str                         # the isolated area; nothing outside is touchable
    branch: str
    context: Context
    permissions: Permissions = field(default_factory=Permissions)
    budget: Budget = field(default_factory=Budget)
    repo: RepoRef | None = None
    #: Which iteration of the validation loop this is, from 1. The agent needs to
    #: know it is fixing, not starting.
    iteration: int = 1
    #: What failed on the previous iteration. Empty on the first.
    previous_failures: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "task": self.task_key, "agent": self.agent,
            "workspace": self.path, "branch": self.branch,
            "repository": self.repo.key if self.repo else None,
            "context": self.context.as_dict(),
            "permissions": list(self.permissions.as_list()),
            "budget": {
                "max_iterations": self.budget.max_iterations,
                "max_tool_calls": self.budget.max_tool_calls,
                "max_cost_usd": self.budget.max_cost_usd,
                "max_seconds": self.budget.max_seconds,
            },
            "iteration": self.iteration,
            "previous_failures": list(self.previous_failures),
        }


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    additions: int = 0
    deletions: int = 0
    status: str = "modified"


@dataclass(frozen=True, slots=True)
class Finding:
    """Something the agent observed that the engine must record.

    Task text, PR text and other people's code are UNTRUSTED data. An attempt at
    manipulation found in there becomes a finding -- never an instruction.
    """
    kind: str          # 'risk' | 'disagreement' | 'suggestion' | 'injection'
    text: str
    path: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """output: status, changed_files, commits, tests, findings, blockers, summary.

    `outcome` is required and comes from a closed vocabulary. An agent that
    returns anything else is treated as ERROR -- never as optimistic success.
    """
    outcome: Outcome
    summary: str
    changed_files: tuple[ChangedFile, ...] = ()
    commits: tuple[str, ...] = ()
    #: Test command executed and its raw output, for the engine to classify.
    #: The agent does NOT classify its own test: it reports, the engine judges.
    test_output: str = ""
    test_command: str = ""
    test_exit_code: int | None = None
    findings: tuple[Finding, ...] = ()
    blockers: tuple[str, ...] = ()
    #: Question to the human, when `outcome == NEEDS_HUMAN`.
    question: dict[str, Any] | None = None
    cost_usd: float = 0.0
    tokens: int = 0
    tool_calls: int = 0
    duration_seconds: float = 0.0

    @property
    def changed_anything(self) -> bool:
        return bool(self.changed_files)

    @property
    def changed_lines(self) -> int:
        return sum(f.additions + f.deletions for f in self.changed_files)


class CodingAgent(Port):
    """Runs a coding agent. The implementation picks the substrate.

    This port is what keeps the engine from being hostage to one harness. A
    runner can be a third-party headless agent, an in-house loop over
    LLMProvider, or a deterministic program. The Orchestrator does not change in
    any of those cases.
    """
    capability = Capability.RUNNER

    @abstractmethod
    def execute(self, request: ExecutionRequest) -> ExecutionResult: ...

    def cancel(self, run_id: str) -> None:
        return None
