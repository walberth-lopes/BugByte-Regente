# -*- coding: utf-8 -*-
"""Scheduler: picks what runs now. A pure function, testable without a database.

The engine's stated goal is **useful work completed per unit of time**, not
volume of analysis. The scheduler is where that becomes code: it maximises
parallelism, but never at the price of two workers running each other over.

Do not parallelise blindly. Before opening two slots, the plan checks:

- **dependency**: have the parents completed?
- **resource conflict**: do the two touch the same file, migration, module,
  API or repository exclusively?
- **cycle**: does the pair block itself without anyone noticing?
- **limit**: is there a slot, and was the daily dispatch cap respected?

A conflict is declared, not guessed. A task states which resource keys it
touches (`repo:acme/api`, `migration:acme/api`, `file:src/auth.py`) and the
scheduler treats any intersection as mutual exclusion. Detecting a genuine
semantic conflict requires reading the code -- that is the job of an analysis
agent, which feeds this list. The scheduler never decides on its own that two
diffs "probably" coexist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .graph import DependencyGraph


@dataclass(frozen=True, slots=True)
class Candidate:
    """What the scheduler needs to know about a task. Nothing beyond that."""
    task_id: str
    priority: int = 100
    resources: frozenset[str] = frozenset()
    key: str = ""


@dataclass(frozen=True, slots=True)
class Limits:
    max_workers: int = 2
    max_dispatches_per_day: int = 8


@dataclass(frozen=True, slots=True)
class Deferred:
    task_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class Plan:
    dispatch: tuple[str, ...] = ()
    deferred: tuple[Deferred, ...] = ()
    #: Self-blocking tasks. They do not become work: they become a question for
    #: the human.
    in_cycle: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.dispatch


def plan(
    candidates: list[Candidate],
    graph: DependencyGraph,
    completed: set[str],
    running_now: dict[str, frozenset[str]],
    limits: Limits = Limits(),
    dispatched_today: int = 0,
    names: dict[str, str] | None = None,
) -> Plan:
    """`running_now` maps task_id -> resources it already holds.

    `names` translates an internal id into the key a human recognises. A
    deferral reason quoting an opaque id forces the reader to go and query the
    database -- and the reason exists precisely to avoid that.

    Decision order: priority, then key. Stable ordering matters more than it
    looks -- without it a tie makes the same tick pick different tasks on each
    run, and the engine keeps going back and forth without finishing anything.
    """
    key_of = (names or {})
    locked = graph.in_cycle()
    unblocked = graph.unblocked(completed)

    # Resources already taken by whoever is running. A live worker owns them.
    taken: set[str] = set()
    for resources in running_now.values():
        taken |= set(resources)

    free_slots = max(0, limits.max_workers - len(running_now))
    daily_budget = max(0, limits.max_dispatches_per_day - dispatched_today)

    dispatch: list[str] = []
    deferred: list[Deferred] = []

    for c in sorted(candidates, key=lambda x: (x.priority, x.key or x.task_id)):
        if c.task_id in locked:
            continue  # reported in `in_cycle`, never dispatched
        if c.task_id in running_now:
            continue
        if c.task_id not in unblocked:
            pending = sorted(key_of.get(p, p) for p in graph.parents(c.task_id) - completed)
            deferred.append(Deferred(c.task_id, f"depends on {', '.join(pending) or 'work not completed'}"))
            continue

        collision = c.resources & taken
        if collision:
            deferred.append(Deferred(c.task_id, f"resource busy: {', '.join(sorted(collision))}"))
            continue
        if not free_slots:
            deferred.append(Deferred(c.task_id, "no free slot"))
            continue
        if not daily_budget:
            deferred.append(Deferred(c.task_id, "daily dispatch cap reached"))
            continue

        dispatch.append(c.task_id)
        # Reserve right here: two candidates from the SAME plan cannot go out
        # together if they share a resource. Forgetting this is the classic way
        # to dispatch two workers onto the same migration on the first parallel
        # tick.
        taken |= c.resources
        free_slots -= 1
        daily_budget -= 1

    return Plan(
        dispatch=tuple(dispatch),
        deferred=tuple(deferred),
        in_cycle=tuple(sorted(locked)),
    )
