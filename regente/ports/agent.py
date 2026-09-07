# -*- coding: utf-8 -*-
"""The coding-agent contract. Structured in, structured out, claims marked as claims.

Two rules define this file.

**The engine never learns what happened by reading free text.** An agent that
writes "done!" has concluded nothing. What concludes is a changed file on disk, a
green test against a baseline, a satisfied criterion. So the mission is an object
with required fields and the result is an object with required fields.

**Everything the agent returns is a claim until the engine observes the world.**
The field names say so -- `claimed_files`, `claimed_tests`, `claim` -- because a
field called `changed_files` reads like a fact and gets used like one. Naming is
the cheapest guard available here, and the only one that works at the moment
somebody writes new code against this type six months from now.

Three types, three authorities, never merged:

    Outcome   -- what the agent's process did and says.        (the agent)
    Observation -- what the engine found in the workspace.     (the engine)
    Verdict   -- what the engine concludes about the task.     (the engine)

An agent may report `Claim.COMPLETE` and the engine may still conclude
`REGRESSED`, `UNKNOWN`, `FAILED` or `NEEDS_HUMAN`. That is not a failure of
cooperation; it is the whole design.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import Capability, Port
from .repository import RepoRef


class ProcessStatus(str, Enum):
    """How the agent's PROCESS ended. A fact about a process, not a judgement.

    Closed vocabulary on purpose: the engine picks its next move from this, and
    free text would force it to guess. An unrecognised word becomes `ERROR`,
    never optimistic success.
    """
    FINISHED = "FINISHED"              # the process ran to completion
    NO_PROGRESS = "NO_PROGRESS"        # it ran and reports changing nothing
    TIMEBOX = "TIMEBOX"                # ran out of time
    BUDGET = "BUDGET"                  # ran out of cost / iterations / tool calls
    NEEDS_HUMAN = "NEEDS_HUMAN"        # it stopped and asked
    ERROR = "ERROR"                    # the process broke


class Claim(str, Enum):
    """What the agent SAYS about the work. Separate from how its process ended.

    A process can finish cleanly while the work is unfinished, and a process can
    be killed after the work was already done. Folding the two into one field
    forces the engine to infer one from the other, and it infers wrong in both
    directions.
    """
    NONE = "NONE"                # said nothing about completion
    COMPLETE = "COMPLETE"        # says the goal is met
    PARTIAL = "PARTIAL"          # says it got some of the way
    BLOCKED = "BLOCKED"          # says it cannot proceed
    NEEDS_HUMAN = "NEEDS_HUMAN"  # says a person must decide


class TestResult(str, Enum):
    """Why a test is the colour it is.

    A red test does NOT mean a bad agent. Five causes produce the same colour,
    and confusing them costs in both directions: punishing the agent for a
    missing dependency, or approving a regression believing it was the
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

    Every write-shaped capability defaults to False. A permission object built
    by someone who forgot a field grants nothing.
    """
    read: bool = True
    write_code: bool = False
    run_tests: bool = False
    run_commands: bool = False
    commit: bool = False
    push: bool = False
    open_pr: bool = False
    deploy: bool = False
    network: bool = False
    write_tasks: bool = False

    ALL = ("read", "write_code", "run_tests", "run_commands", "commit", "push",
           "open_pr", "deploy", "network", "write_tasks")

    def as_list(self) -> tuple[str, ...]:
        return tuple(c for c in self.ALL if getattr(self, c))

    def withheld(self) -> tuple[str, ...]:
        """Capabilities deliberately NOT granted, named.

        Travels in the mission so the agent is told what it cannot do rather
        than discovering it by failing. Telling it is a courtesy; the refusal
        itself lives outside the model, in the sandbox and the policy engine.
        """
        return tuple(c for c in self.ALL if not getattr(self, c))


@dataclass(frozen=True, slots=True)
class Budget:
    max_iterations: int = 6
    max_tool_calls: int = 120
    max_cost_usd: float = 5.0
    max_seconds: int = 1800
    #: Wall-clock ceiling for ONE agent process, distinct from the loop's total.
    #: Without it a single hung process consumes the whole mission's time and the
    #: loop never gets to decide anything.
    max_process_seconds: int = 900
    #: How many times the engine re-runs the agent after a failed validation.
    #: Separate from `max_iterations` because a retry after a red test is a
    #: different decision from an iteration that is still exploring.
    max_validation_retries: int = 2


# ---------------------------------------------------------------------------
# Context: what the engine chose to hand over, and why
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ContextItem:
    """One piece of context, with the reason it was selected.

    `reason` is required. A context package whose items cannot explain
    themselves is prompt concatenation wearing a data structure, and it is
    impossible to debug later: when an agent does the wrong thing, the first
    question is always what it was told and why.
    """
    kind: str            # 'task' | 'evidence' | 'file' | 'test' | 'instructions' | ...
    ref: str             # path, key, or identifier
    reason: str          # why this earned its place
    content: str = ""    # inlined only when the agent cannot fetch it itself
    source: str = ""     # where it came from

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError(
                f"context item {self.kind}:{self.ref} has no reason. Every item "
                f"must justify its place or the package cannot be reviewed")


@dataclass(frozen=True, slots=True)
class ContextPackage:
    """The assembled context, plus what was deliberately left out.

    `excluded` matters as much as `items`. A package that lists only what it
    included cannot be audited for what it missed, and "the agent never saw the
    file" is the most common explanation for a wrong change.
    """
    goal: str
    items: tuple[ContextItem, ...] = ()
    excluded: tuple[tuple[str, str], ...] = ()   # (ref, why not)
    acceptance_criteria: tuple[str, ...] = ()

    def of_kind(self, kind: str) -> tuple[ContextItem, ...]:
        return tuple(i for i in self.items if i.kind == kind)

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "acceptance_criteria": list(self.acceptance_criteria),
            "items": [{"kind": i.kind, "ref": i.ref, "reason": i.reason,
                       "source": i.source, "content": i.content}
                      for i in self.items],
            "excluded": [{"ref": r, "reason": w} for r, w in self.excluded],
        }


# ---------------------------------------------------------------------------
# Mission: everything the agent is told
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Mission:
    """What the engine hands an agent. Identity first, then work, then limits.

    Identity is not bookkeeping. Every field of it appears in the refusals the
    engine issues later -- "this area holds another repository", "this branch
    belongs to another run" -- and a mission that could not say who it was for
    would make those refusals impossible to write.
    """
    # -- identity ------------------------------------------------------
    workspace_id: str
    workspace_name: str
    task_key: str
    run_id: str
    repo: RepoRef | None = None
    branch: str = ""

    # -- where it may work ---------------------------------------------
    #: The ONLY directory the agent may touch. Enforced by the sandbox and
    #: re-checked by the engine afterwards; stated here so the agent is not
    #: guessing at its own boundary.
    allowed_root: str = ""

    # -- the work ------------------------------------------------------
    goal: str = ""
    context: ContextPackage | None = None
    constraints: tuple[str, ...] = ()
    #: Actions that are refused outside the model. Listed so the agent does not
    #: waste a turn attempting them -- never as the mechanism that stops it.
    forbidden_actions: tuple[str, ...] = ()
    #: How the ENGINE will verify the work. Told in advance so the agent can aim
    #: at the same target; the engine runs them itself regardless.
    validation_commands: tuple[tuple[str, ...], ...] = ()
    #: Authority the agent does not hold. Named, not implied.
    withheld_authority: tuple[str, ...] = ()

    # -- limits --------------------------------------------------------
    permissions: Permissions = field(default_factory=Permissions)
    budget: Budget = field(default_factory=Budget)
    agent: str = "coder"
    #: Which turn of the validation loop this is, from 1.
    iteration: int = 1
    #: What failed on the previous turn. Empty on the first.
    previous_failures: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity": {
                "workspace_id": self.workspace_id,
                "workspace": self.workspace_name,
                "task": self.task_key,
                "run_id": self.run_id,
                "repository": self.repo.key if self.repo else None,
                "branch": self.branch,
            },
            "allowed_root": self.allowed_root,
            "goal": self.goal,
            "context": self.context.as_dict() if self.context else {},
            "constraints": list(self.constraints),
            "forbidden_actions": list(self.forbidden_actions),
            "validation_commands": [list(c) for c in self.validation_commands],
            "withheld_authority": list(self.withheld_authority),
            "permissions": list(self.permissions.as_list()),
            "budget": {
                "max_iterations": self.budget.max_iterations,
                "max_tool_calls": self.budget.max_tool_calls,
                "max_cost_usd": self.budget.max_cost_usd,
                "max_seconds": self.budget.max_seconds,
                "max_process_seconds": self.budget.max_process_seconds,
            },
            "iteration": self.iteration,
            "previous_failures": list(self.previous_failures),
        }


# ---------------------------------------------------------------------------
# Outcome: everything the agent says. All of it a claim.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ClaimedFile:
    path: str
    additions: int = 0
    deletions: int = 0
    status: str = "modified"


@dataclass(frozen=True, slots=True)
class ClaimedTest:
    """A test run the agent says it performed.

    Kept as raw command plus raw output plus exit code. The agent does not get
    to say whether its test passed: it reports, the engine classifies.
    """
    command: str = ""
    exit_code: int | None = None
    output: str = ""


@dataclass(frozen=True, slots=True)
class Finding:
    """Something the agent observed that the engine must record.

    Task text, pull request text and other people's code are UNTRUSTED data. An
    attempt at manipulation found in there becomes a finding -- never an
    instruction.
    """
    kind: str          # 'risk' | 'disagreement' | 'suggestion' | 'injection'
    text: str
    path: str = ""


@dataclass(frozen=True, slots=True)
class Outcome:
    """What the agent's process did and says. Every field is a claim.

    `status` is required and comes from a closed vocabulary. Anything else is
    `ERROR` -- never optimistic success.
    """
    status: ProcessStatus
    summary: str = ""
    claim: Claim = Claim.NONE
    claimed_files: tuple[ClaimedFile, ...] = ()
    claimed_tests: tuple[ClaimedTest, ...] = ()
    findings: tuple[Finding, ...] = ()
    blockers: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    #: Set when the agent explicitly asks for a person. Distinct from `questions`:
    #: an agent may wonder aloud without asking anyone to stop.
    escalation_requested: bool = False
    escalation_reason: str = ""
    cost_usd: float = 0.0
    tokens: int = 0
    tool_calls: int = 0
    duration_seconds: float = 0.0
    #: Raw output, kept for the timeline. Never parsed for control decisions.
    raw: str = ""

    @property
    def claims_a_change(self) -> bool:
        """The agent SAYS it changed something. Not that anything changed."""
        return bool(self.claimed_files)

    @property
    def claims_success(self) -> bool:
        return self.claim is Claim.COMPLETE

    def signature(self) -> str:
        """What makes two attempts 'the same attempt' for loop detection."""
        files = ",".join(sorted(f.path for f in self.claimed_files))
        return f"{self.status.value}|{self.claim.value}|{files}"


# ---------------------------------------------------------------------------
# Availability: what stands between a mission and a running agent
# ---------------------------------------------------------------------------

class AuthMode(str, Enum):
    """HOW an agent proves who it is. Shapes of mechanism, never products.

    The engine must be able to say "this workspace's agent authenticates by
    holding its own session" without knowing whose session, whose product or
    whose account. A client may bring a corporate subscription and no key at
    all; another a key and no subscription; another routes everything through an
    internal gateway. All three are ordinary, and an engine that recognised only
    one of them would be an engine for one client.

    These names describe the *shape* of the exchange -- which is genuinely the
    engine's business, because it decides what to report when the exchange fails
    -- and stop precisely there.
    """
    #: The tool holds its own login: a CLI sign-in, a corporate account, SSO.
    #: The engine resolves nothing and stores nothing. It can ask the tool
    #: whether it is signed in, and report the answer.
    SESSION = "SESSION"
    #: The engine resolves a credential through its own SecretProvider and hands
    #: it to the adapter, scoped to the workspace like every other secret.
    RESOLVED_SECRET = "RESOLVED_SECRET"
    #: The engine resolves a credential for an intermediary that authenticates
    #: onward. The agent never sees the real credential.
    GATEWAY = "GATEWAY"
    #: A host process authenticates on the agent's behalf. The engine cannot
    #: verify it, and says so rather than assuming it worked.
    DELEGATED = "DELEGATED"
    #: No credential is needed. A deterministic or purely local agent.
    NONE = "NONE"


class Readiness(str, Enum):
    """Why an agent cannot run, at the granularity a person can act on.

    `BLOCKED_AUTHENTICATION` tells its reader to go and authenticate, whatever
    that means for the agent this workspace configured. "A named environment
    variable is missing" would tell them to set that variable -- wrong advice
    for a client whose agent signs itself in, useless advice for a client behind
    a gateway, and a leak of one implementation into everyone's diagnosis.
    """
    READY = "READY"
    BLOCKED_EXECUTABLE = "BLOCKED_EXECUTABLE"
    BLOCKED_PROTOCOL = "BLOCKED_PROTOCOL"
    BLOCKED_AUTHENTICATION = "BLOCKED_AUTHENTICATION"
    BLOCKED_AGENT = "BLOCKED_AGENT"
    BLOCKED_POLICY = "BLOCKED_POLICY"
    BLOCKED_BUDGET = "BLOCKED_BUDGET"


@dataclass(frozen=True, slots=True)
class Check:
    """One axis of the diagnosis, with the reason either way.

    Three states, not two. `UNKNOWN` exists because an adapter that cannot
    determine something must be able to say so: `False` would send a person to
    fix a thing that may not be broken, and `True` would be a guess wearing a
    fact's clothes.
    """
    ok: bool | None
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.ok is not None

    def __bool__(self) -> bool:
        return self.ok is True

    @classmethod
    def yes(cls, detail: str = "") -> "Check":
        return cls(True, detail)

    @classmethod
    def no(cls, detail: str) -> "Check":
        return cls(False, detail)

    @classmethod
    def unknown(cls, detail: str) -> "Check":
        return cls(None, detail)


@dataclass(frozen=True, slots=True)
class AgentCapabilities:
    """What this agent can do at all. Not what it is permitted to do.

    Distinct from `Permissions`: capability is a fact about an implementation,
    permission is a decision by the engine. An agent that cannot run commands
    and an agent forbidden from running them look identical from outside and
    need opposite responses -- the first is a configuration to work within, the
    second a boundary to enforce.
    """
    edits_files: bool = False
    runs_commands: bool = False
    reaches_network: bool = False
    structured_output: bool = False
    resumable: bool = False
    reports_refused_attempts: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {"edits_files": self.edits_files,
                "runs_commands": self.runs_commands,
                "reaches_network": self.reaches_network,
                "structured_output": self.structured_output,
                "resumable": self.resumable,
                "reports_refused_attempts": self.reports_refused_attempts}


@dataclass(frozen=True, slots=True)
class AgentAvailability:
    """The six-axis diagnosis, and who fills in which axis.

    The first four belong to the ADAPTER: only it knows whether its executable
    is present, whether it speaks the protocol, whether its authentication -- of
    whatever shape -- currently holds, and whether the agent it fronts is
    reachable. The last two belong to the ENGINE: policy and budget are the
    engine's to judge and no adapter may claim them.

    That split is the same authority chain as everywhere else here. An adapter
    reporting `policy=ALLOW` would be a vendor granting itself permission.
    """
    auth_mode: AuthMode = AuthMode.NONE
    adapter: str = ""
    executable: Check = field(default_factory=lambda: Check.unknown("not probed"))
    protocol: Check = field(default_factory=lambda: Check.unknown("not probed"))
    authentication: Check = field(default_factory=lambda: Check.unknown("not probed"))
    agent: Check = field(default_factory=lambda: Check.unknown("not probed"))
    #: Filled by the engine, never by an adapter.
    policy: Check = field(default_factory=lambda: Check.unknown("not evaluated"))
    budget: Check = field(default_factory=lambda: Check.unknown("not evaluated"))
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)

    #: Order matters: the first failing axis is the one to report, because
    #: fixing a later axis while an earlier one is broken achieves nothing.
    ORDER = (("executable", Readiness.BLOCKED_EXECUTABLE),
             ("protocol", Readiness.BLOCKED_PROTOCOL),
             ("authentication", Readiness.BLOCKED_AUTHENTICATION),
             ("agent", Readiness.BLOCKED_AGENT),
             ("policy", Readiness.BLOCKED_POLICY),
             ("budget", Readiness.BLOCKED_BUDGET))

    @property
    def readiness(self) -> Readiness:
        for name, blocked in self.ORDER:
            check = getattr(self, name)
            # UNKNOWN blocks. An axis nobody could determine is not an axis that
            # passed, and treating it as one is how "we could not check" becomes
            # "it is fine".
            if not check:
                return blocked
        return Readiness.READY

    @property
    def ready(self) -> bool:
        return self.readiness is Readiness.READY

    def blocking_reason(self) -> str:
        for name, _ in self.ORDER:
            check = getattr(self, name)
            if not check:
                return f"{name}: {check.detail or 'unavailable'}"
        return "ready"

    def with_engine_verdict(self, policy: Check, budget: Check) -> "AgentAvailability":
        """A copy carrying the two axes only the engine may fill."""
        return AgentAvailability(
            auth_mode=self.auth_mode, adapter=self.adapter,
            executable=self.executable, protocol=self.protocol,
            authentication=self.authentication, agent=self.agent,
            policy=policy, budget=budget, capabilities=self.capabilities)

    def render(self) -> str:
        def mark(c: Check) -> str:
            return "YES" if c.ok is True else ("NO" if c.ok is False else "UNKNOWN")
        rows = [f"adapter        : {self.adapter or '(unnamed)'}",
                f"auth mode      : {self.auth_mode.value}"]
        for name, _ in self.ORDER:
            check = getattr(self, name)
            suffix = f"  -- {check.detail}" if check.detail else ""
            rows.append(f"{name:<15}: {mark(check)}{suffix}")
        rows.append(f"result         : {self.readiness.value}")
        return chr(10).join(rows)


class AgentRunner(Port):
    """Runs a coding agent. The implementation picks the substrate.

    This port is what keeps the engine from being hostage to one harness. An
    implementation may drive a third-party headless agent, an in-house loop over
    a model, or a deterministic program. Nothing above this line changes when
    that choice changes, and no name of any vendor appears on this side of it.
    """
    capability = Capability.RUNNER

    @abstractmethod
    def run(self, mission: Mission) -> Outcome: ...

    @abstractmethod
    def availability(self) -> AgentAvailability:
        """Can this agent run, and if not, which axis is missing?

        Required, not optional. An adapter that cannot answer forces the engine
        to guess, and the engine's only honest guess is "no" -- which would make
        every correctly configured agent look broken.

        The adapter fills the four axes it owns and leaves policy and budget
        alone; `with_engine_verdict` is how the engine adds its two.
        """

    def capabilities(self) -> AgentCapabilities:
        return self.availability().capabilities

    def cancel(self, run_id: str) -> None:
        return None
