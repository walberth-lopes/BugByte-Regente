# -*- coding: utf-8 -*-
"""Dependency graph between work units. Pure, no I/O.

A cycle is not treated as a fatal engine error: it is a condition of the
*backlog* that the engine has to report to a human. A cycle in Jira stalls the
whole team if nobody sees it -- so `cycles()` exists to become an item in the
NEEDS ME queue, not merely an exception.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass(slots=True)
class DependencyGraph:
    #: task -> tasks it depends on
    _parents: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    #: task -> tasks that depend on it
    _children: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    _nodes: set[str] = field(default_factory=set)

    def add(self, task_id: str) -> None:
        self._nodes.add(task_id)
        self._parents.setdefault(task_id, set())
        self._children.setdefault(task_id, set())

    def link(self, task_id: str, depends_on: str) -> None:
        """`task_id` may only start after `depends_on` has finished."""
        self.add(task_id)
        self.add(depends_on)
        self._parents[task_id].add(depends_on)
        self._children[depends_on].add(task_id)

    @property
    def nodes(self) -> frozenset[str]:
        return frozenset(self._nodes)

    def parents(self, task_id: str) -> frozenset[str]:
        return frozenset(self._parents.get(task_id, ()))

    def children(self, task_id: str) -> frozenset[str]:
        return frozenset(self._children.get(task_id, ()))

    def unblocked(self, completed: set[str]) -> frozenset[str]:
        """Nodes whose parents have all completed.

        A dependency pointing outside the graph (the parent task the adapter did
        not bring in) counts as NOT completed. Assuming the opposite would make
        the engine start work whose prerequisite nobody verified.
        """
        ready = set()
        for node in self._nodes:
            if node in completed:
                continue
            if all(p in completed for p in self._parents[node]):
                ready.add(node)
        return frozenset(ready)

    def cycles(self) -> list[list[str]]:
        """Self-blocking components, to be reported -- not to blow up."""
        color: dict[str, int] = {n: 0 for n in self._nodes}   # 0 new, 1 on the stack, 2 closed
        stack: list[str] = []
        findings: list[list[str]] = []

        def visit(node: str) -> None:
            color[node] = 1
            stack.append(node)
            for parent in sorted(self._parents[node]):
                if parent not in color:
                    continue
                if color[parent] == 0:
                    visit(parent)
                elif color[parent] == 1:
                    cut = stack[stack.index(parent):]
                    findings.append(list(cut))
            stack.pop()
            color[node] = 2

        for node in sorted(self._nodes):
            if color[node] == 0:
                visit(node)
        return findings

    def in_cycle(self) -> frozenset[str]:
        return frozenset(n for c in self.cycles() for n in c)

    def layers(self, completed: set[str] | None = None) -> list[list[str]]:
        """Grouped topological order: everything in a layer can run together.

        Useful for visualisation and as proof that the parallelism exists. What
        actually runs together is decided by the scheduler, which still applies
        resource conflicts and the slot limit.
        """
        done = set(completed or ())
        remaining = {n for n in self._nodes if n not in done}
        locked = self.in_cycle()
        output: list[list[str]] = []
        while remaining:
            layer = sorted(
                n for n in remaining
                if n not in locked and all(p in done for p in self._parents[n])
            )
            if not layer:
                break
            output.append(layer)
            done.update(layer)
            remaining -= set(layer)
        return output
