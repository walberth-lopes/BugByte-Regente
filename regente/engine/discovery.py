# -*- coding: utf-8 -*-
"""Target discovery: investigating when nobody declared where a task runs.

Same philosophy as the declared resolver, extended: besides saying **which**
repository, it says **what was considered and why it was rejected**. Without
that, a correct target and a lucky target are indistinguishable -- and on the day
the lucky one is wrong, nobody knows where to look.

What this module does NOT do, and not for lack of room:

- It does not score text similarity. On the real board, one label matched 72
  tasks while naming exactly one repository out of twelve; any similarity score
  there turns "I don't know" into "I'm confidently wrong", which in an engine
  that writes code is worse.
- It does not break ties. A tie is an outcome, not a problem to solve.
- It does not decide on its own that a discovery became truth. See `Source`.

Searches cost I/O against the repository provider, so they run cheapest-first
and stop as soon as the evidence is conclusive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from ..ports import AdapterError
from ..ports.repository import Branch, RepoInfo, RepositoryProvider
from ..ports.tasks import ExternalTask
from .target import (WEIGHT_BRANCH, WEIGHT_DECLARED, Confidence, Evidence,
                     TargetResolver)


class Source(str, Enum):
    """Where the claim came from that this task runs in that repository.

    The distinction exists because a discovery that is right today can be wrong
    tomorrow -- code moves. Promoting DISCOVERED to VALIDATED requires repeated
    confirmation, and that promotion belongs to the system, not to one run.
    """
    DECLARED = "DECLARED"      # a human, or the origin, wrote it down
    DISCOVERED = "DISCOVERED"  # the engine investigated and found evidence
    VALIDATED = "VALIDATED"    # discovered, then confirmed by a real execution

    @property
    def trusted_without_review(self) -> bool:
        return self in (Source.DECLARED, Source.VALIDATED)


#: Weights for the evidence investigation produces. All below WEIGHT_DECLARED:
#: no amount of investigation outranks someone who knows and wrote it down.
WEIGHT_SIBLING_BRANCH = 40   # a branch belonging to a SIBLING task (same parent)
WEIGHT_MODULE = 30           # the repository contains the module the task names

#: Below this the engine proposes no target -- it asks. A single weak signal does
#: not justify writing code into a repository.
CONFIDENCE_FLOOR = WEIGHT_MODULE

#: How many examined-but-empty repositories to name in the record. A record that
#: lists nothing looks like nothing was examined; a record that lists three
#: hundred is unreadable. When the list is cut, the record says so.
MAX_EXAMINED_RECORDED = 8


@dataclass(frozen=True, slots=True)
class Rejected:
    """A candidate considered and dropped, with the reason. Auditable."""
    repo: str
    reason: str


@dataclass(frozen=True, slots=True)
class Discovery:
    """The investigation result, in the shape that gets persisted."""
    task_key: str
    repo: str | None
    confidence: Confidence
    source: Source
    evidence: tuple[Evidence, ...] = ()
    alternatives_considered: tuple[Rejected, ...] = ()
    strength: int = 0
    #: Cost of the investigation, so nobody finds out late that resolving the
    #: target got more expensive than doing the work.
    queries: int = 0

    @property
    def actionable(self) -> bool:
        return bool(self.repo) and self.confidence.actionable

    def as_dict(self) -> dict:
        return {
            "task": self.task_key,
            "repository": self.repo,
            "confidence": self.confidence.value,
            "source": self.source.value,
            "evidence": [f"{e.source}: {e.detail}" for e in self.evidence],
            "alternatives_considered": [r.repo for r in self.alternatives_considered],
            "reason_rejected": [f"{r.repo}: {r.reason}"
                                for r in self.alternatives_considered],
            "strength": self.strength,
            "queries": self.queries,
        }


def _code_like_words(text: str) -> set[str]:
    """Pull plausible identifiers out of a title or description.

    Only things SHAPED LIKE CODE: `snake_case`, `path/to/file`, `file.py`. An
    ordinary English or Portuguese word never qualifies -- ordinary words are
    what would produce similarity matching, which this module exists to avoid.
    """
    found = set()
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*(?:[./][A-Za-z0-9_]+)+|"
                         r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", text):
        candidate = m.group(0).strip("./")
        if len(candidate) >= 8:
            found.add(candidate)
    return found


def _examined_without_evidence(catalog: list[RepoInfo],
                               strengths: dict[str, int]) -> tuple[Rejected, ...]:
    """Repositories that were looked at and produced nothing."""
    empty = [r.ref.key for r in catalog if r.ref.key not in strengths]
    shown = [Rejected(k, "examined; no evidence linked it to this task")
             for k in sorted(empty)[:MAX_EXAMINED_RECORDED]]
    if len(empty) > MAX_EXAMINED_RECORDED:
        shown.append(Rejected(
            f"+{len(empty) - MAX_EXAMINED_RECORDED} more",
            f"examined; list truncated at {MAX_EXAMINED_RECORDED}"))
    return tuple(shown)


@dataclass(slots=True)
class Investigator:
    """Looks for target evidence by querying the repository provider."""

    repos: RepositoryProvider
    resolver: TargetResolver = field(default_factory=TargetResolver)
    #: Query ceiling per task. It exists so investigation never costs more than
    #: the work it enables.
    max_queries: int = 30

    def investigate(
        self,
        task: ExternalTask,
        catalog: list[RepoInfo],
        branches: dict[str, list[Branch]] | None = None,
        siblings: dict[str, list[str]] | None = None,
    ) -> Discovery:
        """`siblings` maps a task key to the keys of tasks under the same parent."""
        branches = branches or {}
        queries = 0

        # --- 1. declared beats everything, and skips the investigation ----
        declared = self.resolver.resolve(task, catalog, branches)
        if declared.confidence is Confidence.DECLARED:
            c = declared.candidates[0]
            return Discovery(
                task_key=task.key, repo=c.repo.ref.key, confidence=Confidence.DECLARED,
                source=Source.DECLARED, evidence=c.evidence, strength=c.strength)

        by_key = {r.ref.key: r for r in catalog}
        found: dict[str, list[Evidence]] = {}
        strengths: dict[str, int] = {}

        def mark(key: str, ev: Evidence, weight: int) -> None:
            if key not in by_key:
                return
            found.setdefault(key, []).append(ev)
            strengths[key] = max(strengths.get(key, 0), weight)

        # --- 2. a branch carrying the task's own key ---------------------
        own = re.compile(rf"\b{re.escape(task.key.upper())}\b")
        for key, items in branches.items():
            for b in items:
                if own.search(b.name.upper()):
                    mark(key, Evidence("branch", f"'{b.name}' names {task.key}"),
                         WEIGHT_BRANCH)
                    break

        # --- 3. a branch belonging to a SIBLING task ---------------------
        # Subtasks under one parent almost always live in the same repository.
        # It is indirect, so it is worth less than the task's own branch.
        for sibling in (siblings or {}).get(task.key, ()):
            pattern = re.compile(rf"\b{re.escape(sibling.upper())}\b")
            for key, items in branches.items():
                for b in items:
                    if pattern.search(b.name.upper()):
                        mark(key, Evidence("sibling_branch",
                                           f"'{b.name}' belongs to sibling {sibling}"),
                             WEIGHT_SIBLING_BRANCH)
                        break

        # --- 4. the repository contains the module the task names --------
        wanted = _code_like_words(f"{task.title} {task.description}")
        if wanted:
            for key in list(by_key):
                if queries >= self.max_queries:
                    break
                for path in sorted(wanted)[:3]:
                    queries += 1
                    try:
                        self.repos.read_file(key, path)
                    except AdapterError:
                        # A CONFIRMED absence is not evidence against: the file
                        # may simply be new, and the task may be about creating it.
                        continue
                    mark(key, Evidence("module", f"contains '{path}', named by the task"),
                         WEIGHT_MODULE)
                    break

        if not found:
            return Discovery(
                task_key=task.key, repo=None, confidence=Confidence.ABSENT,
                source=Source.DISCOVERED, queries=queries,
                alternatives_considered=_examined_without_evidence(catalog, {}))

        best = max(strengths.values())
        winners = sorted(k for k, f in strengths.items() if f == best)
        rejected = tuple(
            Rejected(k, f"weaker evidence ({strengths[k]} < {best})")
            for k in sorted(strengths) if k not in winners)
        # A repository that was examined and yielded nothing is still an
        # alternative that was considered. Leaving it out makes the record read
        # as though it was never looked at, which is the opposite of the truth
        # and exactly what an auditor would need to know.
        rejected += _examined_without_evidence(catalog, strengths)

        if best < CONFIDENCE_FLOOR:
            return Discovery(
                task_key=task.key, repo=None, confidence=Confidence.ABSENT,
                source=Source.DISCOVERED, strength=best, queries=queries,
                evidence=tuple(found[winners[0]]),
                alternatives_considered=rejected + tuple(
                    Rejected(k, f"below the confidence floor ({best} < {CONFIDENCE_FLOOR})")
                    for k in winners))

        if len(winners) > 1:
            return Discovery(
                task_key=task.key, repo=None, confidence=Confidence.AMBIGUOUS,
                source=Source.DISCOVERED, strength=best, queries=queries,
                evidence=tuple(e for k in winners for e in found[k]),
                alternatives_considered=rejected + tuple(
                    Rejected(k, "tied with another candidate") for k in winners))

        winner = winners[0]
        return Discovery(
            task_key=task.key, repo=winner, confidence=Confidence.OBSERVED,
            source=Source.DISCOVERED, strength=best, queries=queries,
            evidence=tuple(found[winner]), alternatives_considered=rejected)


def render(d: Discovery) -> str:
    """The requested shape: repository, confidence, evidence, alternatives."""
    lines = [
        f"repository: {d.repo or '(none)'}",
        f"confidence: {d.confidence.value}",
        f"source: {d.source.value}",
        "",
        "evidence:",
    ]
    lines += [f"- {e.source}: {e.detail}" for e in d.evidence] or ["- (none)"]
    if d.alternatives_considered:
        lines += ["", "alternatives_considered:"]
        lines += [f"- {r.repo}" for r in d.alternatives_considered]
        lines += ["", "reason_rejected:"]
        lines += [f"- {r.repo}: {r.reason}" for r in d.alternatives_considered]
    return "\n".join(lines)
