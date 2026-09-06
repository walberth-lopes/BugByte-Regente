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
    _pais: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    #: task -> tasks that depend on it
    _filhos: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    _nos: set[str] = field(default_factory=set)

    def add(self, task_id: str) -> None:
        self._nos.add(task_id)
        self._pais.setdefault(task_id, set())
        self._filhos.setdefault(task_id, set())

    def link(self, task_id: str, depends_on: str) -> None:
        """`task_id` may only start after `depends_on` has finished."""
        self.add(task_id)
        self.add(depends_on)
        self._pais[task_id].add(depends_on)
        self._filhos[depends_on].add(task_id)

    @property
    def nodes(self) -> frozenset[str]:
        return frozenset(self._nos)

    def parents(self, task_id: str) -> frozenset[str]:
        return frozenset(self._pais.get(task_id, ()))

    def children(self, task_id: str) -> frozenset[str]:
        return frozenset(self._filhos.get(task_id, ()))

    def unblocked(self, completed: set[str]) -> frozenset[str]:
        """Nodes whose parents have all completed.

        A dependency pointing outside the graph (the parent task the adapter did
        not bring in) counts as NOT completed. Assuming the opposite would make
        the engine start work whose prerequisite nobody verified.
        """
        ready = set()
        for no in self._nos:
            if no in completed:
                continue
            if all(p in completed for p in self._pais[no]):
                ready.add(no)
        return frozenset(ready)

    def cycles(self) -> list[list[str]]:
        """Self-blocking components, to be reported -- not to blow up."""
        cor: dict[str, int] = {n: 0 for n in self._nos}   # 0 new, 1 on the stack, 2 closed
        pilha: list[str] = []
        findings: list[list[str]] = []

        def visita(no: str) -> None:
            cor[no] = 1
            pilha.append(no)
            for prox in sorted(self._pais[no]):
                if prox not in cor:
                    continue
                if cor[prox] == 0:
                    visita(prox)
                elif cor[prox] == 1:
                    corte = pilha[pilha.index(prox):]
                    findings.append(list(corte))
            pilha.pop()
            cor[no] = 2

        for no in sorted(self._nos):
            if cor[no] == 0:
                visita(no)
        return findings

    def in_cycle(self) -> frozenset[str]:
        return frozenset(n for c in self.cycles() for n in c)

    def layers(self, completed: set[str] | None = None) -> list[list[str]]:
        """Grouped topological order: everything in a layer can run together.

        Useful for visualisation and as proof that the parallelism exists. What
        actually runs together is decided by the scheduler, which still applies
        resource conflicts and the slot limit.
        """
        feitas = set(completed or ())
        restantes = {n for n in self._nos if n not in feitas}
        locked = self.in_cycle()
        output: list[list[str]] = []
        while restantes:
            camada = sorted(
                n for n in restantes
                if n not in locked and all(p in feitas for p in self._pais[n])
            )
            if not camada:
                break
            output.append(camada)
            feitas.update(camada)
            restantes -= set(camada)
        return output
