# -*- coding: utf-8 -*-
"""The execution chain, in shadow: from the real task to an execution candidate.

    task -> candidate repository -> context -> base branch
         -> resources/isolation -> risk/policy -> execution candidate

Every link can reject, and rejecting is a normal outcome -- not a failure. The
value of this module lies in saying **at which link** the work stopped: "there is
nothing to do", "I do not know where to do it" and "I may not do it" demand
completely different actions from the owner, and a single number would hide them.

Nothing here mutates the world. Nothing here writes state. The chain is
recomputable from scratch at any moment, and that is what makes it safe to run at
will.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from ..core.policy import Action, AutonomyLevel, Decision, Effect, PolicyContext, PolicyEngine
from ..core.risk import RiskAssessment, RiskEngine
from ..ports.repository import Branch, RepoCapability, RepoInfo
from ..ports.tasks import ExternalTask
from .target import Target, Confidence, TargetResolver


class Stage(str, Enum):
    """Where the chain stopped. A closed vocabulary: each value demands an action.

    Nothing persists or parses these -- they are reported and counted -- so the
    member and its value are kept identical.
    """
    NO_WORK = "NO_WORK"                      # the source says it is not available
    NO_TARGET = "NO_TARGET"                  # nobody knows which repository it runs in
    AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"    # more than one candidate, tied
    REPO_UNUSABLE = "REPO_UNUSABLE"          # archived, or with no base branch
    NO_CAPABILITY = "NO_CAPABILITY"          # the adapter cannot do what is needed
    BLOCKED_BY_POLICY = "BLOCKED_BY_POLICY"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    CANDIDATE = "CANDIDATE"                  # passed everything

    @property
    def executable(self) -> bool:
        return self is Stage.CANDIDATE


@dataclass(frozen=True, slots=True)
class Step:
    task: ExternalTask
    link: Stage
    reason: str
    target: Target | None = None
    repo: RepoInfo | None = None
    base_branch: str = ""
    work_branch: str = ""
    resources: tuple[str, ...] = ()
    risk: RiskAssessment | None = None
    decision: Decision | None = None

    @property
    def summary(self) -> str:
        where = self.repo.ref.key if self.repo else "-"
        return f"{self.task.key:<10} {self.link.value:<20} {where}"


@dataclass(slots=True)
class ChainReport:
    workspace: str
    tasks: int = 0
    repos: int = 0
    by_stage: Counter = field(default_factory=Counter)
    by_confidence: Counter = field(default_factory=Counter)
    steps: tuple[Step, ...] = ()
    mutations: int = 0

    @property
    def candidates(self) -> tuple[Step, ...]:
        return tuple(p for p in self.steps if p.link.executable)


#: The action the engine would have to execute to work on a task. Declared here
#: so that policy and risk are evaluated against the SAME action that would
#: really be requested -- evaluating a generic action would give a verdict that
#: corresponds to nothing.
WORK_ACTION = "repo.branch"

#: Capabilities without which there is no way to start code work.
REQUIRED_CAPS = (RepoCapability.READ_FILES, RepoCapability.CLONE)


def build(
    workspace_name: str,
    workspace_id: str,
    tasks: list[ExternalTask],
    repos: list[RepoInfo],
    resolver: TargetResolver,
    policy: PolicyEngine,
    risk: RiskEngine,
    autonomy: AutonomyLevel,
    branches: dict[str, list[Branch]] | None = None,
    environment: str = "staging",
    organization: str = "*",
    client: str = "*",
) -> ChainReport:
    rel = ChainReport(workspace=workspace_name, tasks=len(tasks), repos=len(repos))
    steps: list[Step] = []

    for t in tasks:
        # --- link 1: is there work? ------------------------------------
        if not t.status.available:
            steps.append(Step(t, Stage.NO_WORK,
                                f"the source says {t.external_status or t.status.value}"))
            continue

        # --- link 2: where? --------------------------------------------
        target = resolver.resolve(t, repos, branches)
        rel.by_confidence[target.confidence.value] += 1
        if target.confidence is Confidence.ABSENT:
            steps.append(Step(t, Stage.NO_TARGET, target.reason, target=target))
            continue
        if target.confidence is Confidence.AMBIGUOUS:
            steps.append(Step(t, Stage.AMBIGUOUS_TARGET, target.reason, target=target))
            continue

        repo = target.repo
        assert repo is not None   # guaranteed by Confidence.actionable + 1 candidate

        # --- link 3: can we work in it? --------------------------------
        if not repo.usable:
            steps.append(Step(t, Stage.REPO_UNUSABLE,
                                "; ".join(repo.anomalies) or "no base branch",
                                target=target, repo=repo))
            continue
        missing = [c.value for c in REQUIRED_CAPS if not repo.can(c)]
        if missing:
            # Finding this out now saves a whole cycle -- and saves an
            # escalation to the human for a reason the engine already knew.
            steps.append(Step(t, Stage.NO_CAPABILITY,
                                f"the provider does not offer: {', '.join(missing)}",
                                target=target, repo=repo))
            continue

        # --- link 4: isolation -----------------------------------------
        # The resource is scoped by workspace. A repository of the same name in
        # another client is another resource, and cannot contend for the same lock.
        resources = (repo.ref.resource(workspace_id),)
        branch = f"regente/{t.key.lower()}"

        # --- link 5: risk and policy -----------------------------------
        assessment = risk.assess({
            "action": WORK_ACTION,
            "environment": environment,
            "category": "repo",
            "paths": [t.title, *t.labels],
        })
        decision = policy.decide(PolicyContext(
            action=Action(kind=WORK_ACTION, resource=repo.ref.key,
                          environment=environment),
            organization=organization, client=client, workspace=workspace_name,
            project=t.project or "*", agent="coder",
            risk=assessment.level.name, autonomy=autonomy))

        common = dict(target=target, repo=repo, base_branch=repo.base_branch,
                     work_branch=branch, resources=resources,
                     risk=assessment, decision=decision)
        if decision.effect == Effect.DENY:
            steps.append(Step(t, Stage.BLOCKED_BY_POLICY, decision.reason, **common))
            continue
        if decision.effect == Effect.HUMAN_APPROVAL:
            steps.append(Step(t, Stage.NEEDS_HUMAN, decision.reason, **common))
            continue

        steps.append(Step(t, Stage.CANDIDATE,
                            f"risk {assessment.level.name}; base {repo.base_branch}",
                            **common))

    rel.steps = tuple(steps)
    rel.by_stage = Counter(p.link.value for p in steps)
    return rel


def render(rel: ChainReport, limit: int = 10) -> str:
    lines = [
        "EXECUTION CHAIN (shadow)",
        "",
        f"  Workspace                  {rel.workspace}",
        f"  Tasks                      {rel.tasks}",
        f"  Repositories               {rel.repos}",
        f"  Mutations                  {rel.mutations}   <- has to be 0",
        "",
        "  WHERE THE CHAIN STOPPED",
    ]
    for link, n in rel.by_stage.most_common():
        lines.append(f"    {link:<22} {n}")

    if rel.by_confidence:
        lines += ["", "  TARGET CONFIDENCE (of those that had work)"]
        for c, n in rel.by_confidence.most_common():
            lines.append(f"    {c:<22} {n}")

    candidates = rel.candidates
    lines += ["", f"  EXECUTION CANDIDATES ({len(candidates)})"]
    for p in candidates[:limit]:
        lines.append(f"    {p.task.key:<10} -> {p.repo.ref.key}")
        lines.append(f"                  base={p.base_branch}  branch={p.work_branch}")
        lines.append(f"                  resource={p.resources[0]}")
        lines.append(f"                  risk={p.risk.level.name}  policy={p.decision.effect}"
                      f" ({p.decision.rule})")
        lines.append(f"                  evidence: {p.target.reason[:70]}")
    if len(candidates) > limit:
        lines.append(f"    ... {len(candidates) - limit} more")

    ambiguous = [p for p in rel.steps if p.link is Stage.AMBIGUOUS_TARGET]
    if ambiguous:
        lines += ["", f"  AMBIGUOUS -- the engine does NOT break the tie ({len(ambiguous)})"]
        for p in ambiguous[:5]:
            lines.append(f"    {p.task.key:<10} {p.reason[:80]}")
    return "\n".join(lines)
