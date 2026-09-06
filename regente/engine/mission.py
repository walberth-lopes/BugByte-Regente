# -*- coding: utf-8 -*-
"""Mission selection: choosing the one task that may be executed now.

`REFUSED` is a first-class outcome, not an error. An engine that always finds
something to do will eventually do the wrong thing, and the moment it is most
likely to do so is exactly the moment nothing safe is available. So refusal
carries the same weight as approval, and it names the criterion that failed --
"nothing to do", "I don't know where", and "I may not" demand opposite actions
from the owner, and one number would hide the difference.

Every criterion here is a veto, never a score. Scoring would let a strong signal
compensate for a missing one: high confidence in the target would silently pay
for someone already working on the task. Vetoes do not trade against each other.

**The sharpest criterion is also the least obvious.** The strongest evidence that
a task belongs to a repository -- an existing branch naming it -- is the same
evidence that a person already started the work. The two readings are opposite
and the signal is identical. So a branch is accepted as target evidence *and*
inspected for commits ahead of the base: ahead means someone is mid-flight, and
the engine steps back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..core.policy import (Action, AutonomyLevel, Effect, PolicyContext,
                           PolicyEngine)
from ..core.risk import RiskAssessment, RiskEngine, RiskLevel
from ..ports import AdapterError
from ..ports.agent import Budget, Permissions
from ..ports.repository import (Branch, RepoCapability, RepoInfo,
                                RepositoryProvider)
from ..ports.tasks import ExternalTask
from .discovery import Discovery, Investigator, Source
from .target import Confidence

#: The action the engine would have to take to start work on a task. Policy and
#: risk are evaluated against THIS action, not a generic one -- a verdict about
#: an action nobody would take is a verdict about nothing.
WORK_ACTION = "repo.branch"

#: Capabilities without which code work cannot even begin.
REQUIRED_CAPABILITIES = (RepoCapability.READ_FILES, RepoCapability.CLONE)


class Refusal(str, Enum):
    """Why this task may not be executed now. One per failed criterion."""
    NO_WORK_AVAILABLE = "NO_WORK_AVAILABLE"          # the origin says it is not free
    HUMAN_WORK_IN_FLIGHT = "HUMAN_WORK_IN_FLIGHT"    # someone already started
    TARGET_UNKNOWN = "TARGET_UNKNOWN"                # no evidence of a repository
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"            # more than one candidate, tied
    TARGET_CONFIDENCE_TOO_LOW = "TARGET_CONFIDENCE_TOO_LOW"
    REPO_UNUSABLE = "REPO_UNUSABLE"                  # archived, or no base branch
    PROVIDER_CANNOT = "PROVIDER_CANNOT"              # the adapter lacks a capability
    BLOCKED_BY_DEPENDENCY = "BLOCKED_BY_DEPENDENCY"
    RISK_TOO_HIGH = "RISK_TOO_HIGH"
    POLICY_DENIED = "POLICY_DENIED"
    NEEDS_HUMAN_APPROVAL = "NEEDS_HUMAN_APPROVAL"
    SCOPE_TOO_LARGE = "SCOPE_TOO_LARGE"


@dataclass(frozen=True, slots=True)
class Rejection:
    task_key: str
    refusal: Refusal
    detail: str


@dataclass(frozen=True, slots=True)
class Briefing:
    """Everything that must be visible BEFORE anything runs.

    This is not a log line. It is the disclosure a person reads to decide whether
    the engine understood the job -- which is only useful if it is produced
    before the work, never after.
    """
    task_key: str
    task_title: str
    target: str
    evidence: tuple[str, ...]
    confidence: Confidence
    target_source: Source
    policy: Effect
    policy_rule: str | None
    risk: RiskLevel
    risk_signals: tuple[str, ...]
    workspace: str
    branch: str
    base_branch: str
    baseline_command: tuple[str, ...] | None
    agent: str
    budget: Budget
    timeout_seconds: int
    permissions: Permissions
    expected_mutations: tuple[str, ...]
    forbidden_mutations: tuple[str, ...]

    def render(self) -> str:
        def row(label: str, value: object) -> str:
            return f"  {label:<22} {value}"

        lines = [
            "MISSION BRIEFING",
            "",
            row("TASK", f"{self.task_key}  {self.task_title[:56]}"),
            row("TARGET", self.target),
            "  EVIDENCE",
        ]
        lines += [f"    - {e}" for e in self.evidence] or ["    - (none)"]
        lines += [
            row("CONFIDENCE", f"{self.confidence.value} ({self.target_source.value})"),
            row("POLICY", f"{self.policy}"
                          + (f" ({self.policy_rule})" if self.policy_rule else "")),
            row("RISK", f"{self.risk.name}"
                        + (f" -- {'; '.join(self.risk_signals)}" if self.risk_signals else "")),
            row("WORKSPACE", self.workspace),
            row("BRANCH", f"{self.branch}  (from {self.base_branch})"),
            row("BASELINE", " ".join(self.baseline_command) if self.baseline_command
                            else "(not measured yet -- needs the clone)"),
            row("AGENT", self.agent),
            row("BUDGET", f"{self.budget.max_iterations} iterations, "
                          f"{self.budget.max_tool_calls} tool calls, "
                          f"US$ {self.budget.max_cost_usd:.2f}"),
            row("TIMEOUT", f"{self.timeout_seconds}s"),
            row("PERMISSIONS", ", ".join(self.permissions.as_list())),
            "",
            "  EXPECTED MUTATIONS",
        ]
        lines += [f"    + {m}" for m in self.expected_mutations]
        lines += ["", "  FORBIDDEN MUTATIONS"]
        lines += [f"    x {m}" for m in self.forbidden_mutations]
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Mission:
    """An approved mission. Carries the discovery so it can be persisted."""
    task: ExternalTask
    repo: RepoInfo
    discovery: Discovery
    risk: RiskAssessment
    policy_effect: Effect
    policy_rule: str | None
    branch: str
    resource: str


@dataclass(frozen=True, slots=True)
class Selection:
    """The result of looking at the whole board. Either one mission, or none."""
    mission: Mission | None
    rejections: tuple[Rejection, ...] = ()
    considered: int = 0

    @property
    def refused(self) -> bool:
        return self.mission is None

    def render(self) -> str:
        if self.mission is not None:
            return (f"SELECTED {self.mission.task.key} -> {self.mission.repo.ref.key}\n"
                    f"  {len(self.rejections)} other task(s) did not qualify")
        counts: dict[str, int] = {}
        for r in self.rejections:
            counts[r.refusal.value] = counts.get(r.refusal.value, 0) + 1
        lines = ["REFUSED -- no task satisfies the safety criteria",
                 f"  considered: {self.considered}", ""]
        for reason, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {reason:<28} {n}")
        return "\n".join(lines)


#: Mutations this milestone expects, and the ones it must never make. Stated as
#: data so the briefing cannot drift from what the code actually allows.
EXPECTED_MUTATIONS = (
    "clone the repository into an isolated area",
    "create an isolated branch inside that clone",
    "write code inside that clone only",
    "run tests inside that clone",
    "commit to the isolated branch",
)
FORBIDDEN_MUTATIONS = (
    "push to any remote",
    "open or update a pull request",
    "merge anything",
    "deploy anything",
    "write to the external task (status, comment, label)",
    "touch the base branch",
    "touch the source clone or any path outside the isolated area",
)


@dataclass(slots=True)
class MissionPlanner:
    """Picks at most one task, and explains every rejection."""

    repos: RepositoryProvider
    investigator: Investigator
    policy: PolicyEngine
    risk: RiskEngine
    autonomy: AutonomyLevel
    workspace_name: str
    workspace_id: str
    organization: str = "*"
    client: str = "*"
    environment: str = "staging"
    #: Highest risk this milestone will execute autonomously. LOW on purpose:
    #: the first proof of a write path is not the place to find out whether the
    #: risk engine is calibrated.
    max_risk: RiskLevel = RiskLevel.LOW
    agent_name: str = "coder"

    def select(
        self,
        tasks: list[ExternalTask],
        catalog: list[RepoInfo],
        branches: dict[str, list[Branch]] | None = None,
        siblings: dict[str, list[str]] | None = None,
        completed: set[str] | None = None,
        branch_is_ahead=None,
    ) -> Selection:
        """`branch_is_ahead(repo_key, branch_name) -> bool` detects live human work.

        Injected rather than called directly so selection stays testable without
        a repository, and so the cost of asking is paid only for the few tasks
        that get that far.
        """
        rejections: list[Rejection] = []
        completed = completed or set()

        for task in sorted(tasks, key=lambda t: (t.priority, t.key)):
            verdict = self._evaluate(task, catalog, branches or {}, siblings or {},
                                     completed, branch_is_ahead)
            if isinstance(verdict, Mission):
                return Selection(mission=verdict, rejections=tuple(rejections),
                                 considered=len(tasks))
            rejections.append(verdict)
        return Selection(mission=None, rejections=tuple(rejections),
                         considered=len(tasks))

    # ------------------------------------------------------------------
    def _evaluate(self, task, catalog, branches, siblings, completed,
                  branch_is_ahead) -> Mission | Rejection:
        def no(refusal: Refusal, detail: str) -> Rejection:
            return Rejection(task.key, refusal, detail)

        # --- 1. is there work at all? ---------------------------------
        if not task.status.available:
            return no(Refusal.NO_WORK_AVAILABLE,
                      f"the origin says {task.external_status or task.status.value}")

        # --- 2. blocking dependencies ---------------------------------
        pending = [v.key for v in task.links if v.blocking and v.key not in completed]
        if pending:
            return no(Refusal.BLOCKED_BY_DEPENDENCY,
                      f"waits on {', '.join(sorted(pending))}")

        # --- 3. where does it run? ------------------------------------
        discovery = self.investigator.investigate(task, catalog, branches, siblings)
        if discovery.confidence is Confidence.AMBIGUOUS:
            return no(Refusal.TARGET_AMBIGUOUS,
                      f"{len(discovery.alternatives_considered)} candidates, tied")
        if not discovery.actionable:
            return no(Refusal.TARGET_UNKNOWN, discovery.as_dict()["reason_rejected"][:1]
                      and "no evidence links this task to a repository"
                      or "no evidence")
        if discovery.confidence is Confidence.OBSERVED and discovery.strength < 40:
            return no(Refusal.TARGET_CONFIDENCE_TOO_LOW,
                      f"strength {discovery.strength} is below what justifies writing code")

        by_key = {r.ref.key: r for r in catalog}
        repo = by_key.get(discovery.repo or "")
        if repo is None:
            return no(Refusal.TARGET_UNKNOWN,
                      f"'{discovery.repo}' is not in the visible catalog")

        # --- 4. can we work there? ------------------------------------
        if not repo.usable:
            return no(Refusal.REPO_UNUSABLE, "; ".join(repo.anomalies) or "unusable")
        missing = [c.value for c in REQUIRED_CAPABILITIES if not repo.can(c)]
        if missing:
            return no(Refusal.PROVIDER_CANNOT,
                      f"the provider does not offer: {', '.join(missing)}")

        # --- 5. is a person already inside? ---------------------------
        # The same branch that proves the target can prove someone is mid-flight.
        # Reading it both ways is the point: one reading enables the work, the
        # other forbids it, and only looking at the commits tells them apart.
        if branch_is_ahead is not None:
            for b in branches.get(repo.ref.key, ()):
                if task.key.upper() not in b.name.upper():
                    continue
                try:
                    if branch_is_ahead(repo.ref.key, b.name):
                        return no(Refusal.HUMAN_WORK_IN_FLIGHT,
                                  f"branch '{b.name}' already has commits beyond "
                                  f"{repo.base_branch}")
                except AdapterError as e:
                    # Not being able to check is not permission to proceed.
                    return no(Refusal.HUMAN_WORK_IN_FLIGHT,
                              f"could not verify whether '{b.name}' is ahead: {e}")

        # --- 6. risk and scope ----------------------------------------
        assessment = self.risk.assess({
            "action": WORK_ACTION,
            "environment": self.environment,
            "category": "repo",
            "paths": [task.title, *task.labels],
        })
        if assessment.level > self.max_risk:
            return no(Refusal.RISK_TOO_HIGH,
                      f"{assessment.level.name}: {'; '.join(assessment.reasons[:2])}")

        # --- 7. policy -------------------------------------------------
        decision = self.policy.decide(PolicyContext(
            action=Action(kind=WORK_ACTION, resource=repo.ref.key,
                          environment=self.environment),
            organization=self.organization, client=self.client,
            workspace=self.workspace_name, project=task.project or "*",
            agent=self.agent_name, risk=assessment.level.name,
            autonomy=self.autonomy))
        if decision.effect == Effect.DENY:
            return no(Refusal.POLICY_DENIED, decision.reason)
        if decision.effect == Effect.HUMAN_APPROVAL:
            return no(Refusal.NEEDS_HUMAN_APPROVAL, decision.reason)

        return Mission(
            task=task, repo=repo, discovery=discovery, risk=assessment,
            policy_effect=decision.effect, policy_rule=decision.rule,
            branch=f"regente/{task.key.lower()}",
            resource=repo.ref.resource(self.workspace_id))


def briefing_for(
    mission: Mission,
    workspace_path: str,
    agent: str,
    budget: Budget,
    permissions: Permissions,
    baseline_command: tuple[str, ...] | None,
    timeout_seconds: int,
) -> Briefing:
    return Briefing(
        task_key=mission.task.key,
        task_title=mission.task.title,
        target=mission.repo.ref.key,
        evidence=tuple(f"{e.source}: {e.detail}" for e in mission.discovery.evidence),
        confidence=mission.discovery.confidence,
        target_source=mission.discovery.source,
        policy=mission.policy_effect,
        policy_rule=mission.policy_rule,
        risk=mission.risk.level,
        risk_signals=mission.risk.reasons,
        workspace=workspace_path,
        branch=mission.branch,
        base_branch=mission.repo.base_branch,
        baseline_command=baseline_command,
        agent=agent,
        budget=budget,
        timeout_seconds=timeout_seconds,
        permissions=permissions,
        expected_mutations=EXPECTED_MUTATIONS,
        forbidden_mutations=FORBIDDEN_MUTATIONS,
    )
