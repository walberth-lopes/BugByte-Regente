# -*- coding: utf-8 -*-
"""Where can this task be executed? -- target repository resolution.

**The finding that defines this module:** measured against 100 real tasks and 12
real repositories on 06/09/2026, the natural field for answering this
(`components`) is empty in 100 out of 100. The strongest signal available -- a
branch already existing that names the task key -- covers 14 out of 100. The rest
are ambiguous by nature: the label `scamchecker` appears in 72 tasks and is at
once the name of ONE repository and the name of the whole product, which has
twelve.

In other words: **the information is missing, not hidden.** No cleverness in text
matching solves that -- it only swaps "I do not know" for "I got it wrong
confidently", which is infinitely worse in an engine that is going to write code.

So this module collects evidence and declares what it knows. It never picks a
winner in a tie and never invents something out of nothing. Ambiguity and absence
are legitimate outcomes, and they become a question for the human -- which is
exactly the kind of thing the NEEDS ME queue exists to receive.

FUTURE CONTRACT BETWEEN TaskProvider AND RepositoryProvider
-----------------------------------------------------------
What is missing is not code, it is **declared data**. In order of preference:

1. The task provider starts emitting the target (a field of its own, a
   component, a label convention). It is the only source that does not age,
   because whoever writes the task knows where it runs.
2. Until that exists, a map in the workspace configuration
   (`label -> repo`, `project -> repo`) covers the common case with zero
   guessing.
3. An analysis agent reads the code and proposes the target with evidence --
   expensive, and last for that reason, but the only one that resolves a new task
   in a new repository.

None of the three requires changing the Core: `ExternalTask.resources` and
`data` already carry the result, wherever it comes from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from ..ports.repository import Branch, RepoInfo
from ..ports.tasks import ExternalTask


class Confidence(str, Enum):
    """How much the engine knows about where the task runs.

    There is no invented intermediate degree. Either the evidence is declared by
    somebody, or it is observed in the world, or it does not exist.
    """
    #: Somebody declared it explicitly. There is nothing to interpret.
    DECLARED = "DECLARED"
    #: The world shows work already started in a repository (a branch with the key).
    OBSERVED = "OBSERVED"
    #: More than one candidate of equal strength. The engine does NOT break the tie.
    AMBIGUOUS = "AMBIGUOUS"
    #: No evidence at all. The engine does not guess.
    ABSENT = "ABSENT"

    @property
    def actionable(self) -> bool:
        return self in (Confidence.DECLARED, Confidence.OBSERVED)


@dataclass(frozen=True, slots=True)
class Evidence:
    """Why this repository is a candidate. Without it, nothing is auditable."""
    source: str
    detail: str


@dataclass(frozen=True, slots=True)
class Candidate:
    repo: RepoInfo
    evidence: tuple[Evidence, ...]
    #: Weight of the strongest evidence supporting this candidate.
    strength: int = 0


@dataclass(frozen=True, slots=True)
class Target:
    """The result of the resolution. It may legitimately have no repository."""
    task_key: str
    confidence: Confidence
    candidates: tuple[Candidate, ...] = ()
    reason: str = ""

    @property
    def repo(self) -> RepoInfo | None:
        """The target, when there is exactly one and the evidence supports it."""
        if self.confidence.actionable and len(self.candidates) == 1:
            return self.candidates[0].repo
        return None

    @property
    def base_branch(self) -> str:
        r = self.repo
        return r.base_branch if r else ""


#: Weights. `DECLARED` beats `OBSERVED` because a branch can be the leftovers of
#: an abandoned attempt, whereas a map is a statement by someone who knows.
WEIGHT_DECLARED = 100
WEIGHT_BRANCH = 50


@dataclass(slots=True)
class TargetResolver:
    """Joins declared and observed evidence. It does not interpret free text."""

    #: `label -> repo key`, coming from the workspace configuration.
    by_label: dict[str, str] = field(default_factory=dict)
    #: `project -> repo key`.
    by_project: dict[str, str] = field(default_factory=dict)
    #: `task key -> repo key`, for the one-off case that does not fit a rule.
    by_task: dict[str, str] = field(default_factory=dict)

    def resolve(self, task: ExternalTask, repos: list[RepoInfo],
                branches: dict[str, list[Branch]] | None = None) -> Target:
        by_key = {r.ref.key: r for r in repos}
        # A short name resolves too, so the configuration can say
        # `dashboard-api` instead of the whole key. A name AMBIGUOUS between two
        # repositories does not enter the index: that would reintroduce guessing.
        short_names: dict[str, list[RepoInfo]] = {}
        for r in repos:
            short_names.setdefault(r.name.lower(), []).append(r)
        by_name = {n: v[0] for n, v in short_names.items() if len(v) == 1}

        def find_repo(key: str) -> RepoInfo | None:
            return by_key.get(key) or by_name.get(key.lower())

        findings: dict[str, list[Evidence]] = {}
        strengths: dict[str, int] = {}

        def mark(repo: RepoInfo | None, ev: Evidence, weight: int) -> None:
            if repo is None:
                return
            findings.setdefault(repo.ref.key, []).append(ev)
            strengths[repo.ref.key] = max(strengths.get(repo.ref.key, 0), weight)

        # --- 1. declared -------------------------------------------------
        if task.key in self.by_task:
            mark(find_repo(self.by_task[task.key]),
                  Evidence("map:task", f"{task.key} -> {self.by_task[task.key]}"),
                  WEIGHT_DECLARED)
        for label in task.labels:
            if label in self.by_label:
                mark(find_repo(self.by_label[label]),
                      Evidence("map:label", f"label '{label}' -> {self.by_label[label]}"),
                      WEIGHT_DECLARED)
        if task.project in self.by_project:
            mark(find_repo(self.by_project[task.project]),
                  Evidence("map:project",
                            f"project '{task.project}' -> {self.by_project[task.project]}"),
                  WEIGHT_DECLARED)

        # --- 2. observed in the world ------------------------------------
        # Whole-word matching: `SG-11` must not match `SG-110`.
        target_re = re.compile(rf"\b{re.escape(task.key.upper())}\b")
        for key, items in (branches or {}).items():
            repo = by_key.get(key)
            if repo is None:
                continue
            for b in items:
                if target_re.search(b.name.upper()):
                    mark(repo, Evidence("branch", f"'{b.name}' names {task.key}"),
                          WEIGHT_BRANCH)
                    break

        if not findings:
            return Target(task_key=task.key, confidence=Confidence.ABSENT,
                        reason="no declared or observed evidence links this "
                               "task to a repository")

        best = max(strengths.values())
        winners = [k for k, f in strengths.items() if f == best]
        candidates = tuple(
            Candidate(repo=by_key[k], evidence=tuple(findings[k]), strength=strengths[k])
            for k in sorted(winners))

        if len(winners) > 1:
            return Target(task_key=task.key, confidence=Confidence.AMBIGUOUS,
                        candidates=candidates,
                        reason=f"{len(winners)} repositories with evidence of the same "
                               f"strength: {', '.join(winners)}")

        return Target(
            task_key=task.key,
            confidence=Confidence.DECLARED if best >= WEIGHT_DECLARED else Confidence.OBSERVED,
            candidates=candidates,
            reason="; ".join(e.detail for e in candidates[0].evidence))
