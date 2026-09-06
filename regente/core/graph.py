# -*- coding: utf-8 -*-
"""Grafo de dependencias entre unidades de trabalho. Puro, sem I/O.

Ciclo nao e tratado como erro fatal do motor: e uma condicao do *backlog* que o
motor precisa reportar ao humano. Um ciclo no Jira trava o time inteiro se
ninguem o enxerga -- entao `ciclos()` existe para virar item da fila NEEDS ME,
nao apenas excecao.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass(slots=True)
class DependencyGraph:
    #: task -> tarefas das quais ela depende
    _pais: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    #: task -> tarefas que dependem dela
    _filhos: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    _nos: set[str] = field(default_factory=set)

    def adiciona(self, task_id: str) -> None:
        self._nos.add(task_id)
        self._pais.setdefault(task_id, set())
        self._filhos.setdefault(task_id, set())

    def liga(self, task_id: str, depende_de: str) -> None:
        """`task_id` so pode comecar depois que `depende_de` terminar."""
        self.adiciona(task_id)
        self.adiciona(depende_de)
        self._pais[task_id].add(depende_de)
        self._filhos[depende_de].add(task_id)

    @property
    def nos(self) -> frozenset[str]:
        return frozenset(self._nos)

    def pais(self, task_id: str) -> frozenset[str]:
        return frozenset(self._pais.get(task_id, ()))

    def filhos(self, task_id: str) -> frozenset[str]:
        return frozenset(self._filhos.get(task_id, ()))

    def desbloqueadas(self, concluidas: set[str]) -> frozenset[str]:
        """Nos cujos pais estao todos concluidos.

        Dependencia para fora do grafo (a task-mae que o adapter nao trouxe)
        conta como NAO concluida. Assumir o contrario faria o motor comecar
        trabalho cujo pre-requisito ninguem verificou.
        """
        prontas = set()
        for no in self._nos:
            if no in concluidas:
                continue
            if all(p in concluidas for p in self._pais[no]):
                prontas.add(no)
        return frozenset(prontas)

    def ciclos(self) -> list[list[str]]:
        """Componentes que se autobloqueiam, para reportar -- nao para explodir."""
        cor: dict[str, int] = {n: 0 for n in self._nos}   # 0 novo, 1 na pilha, 2 fechado
        pilha: list[str] = []
        achados: list[list[str]] = []

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
                    achados.append(list(corte))
            pilha.pop()
            cor[no] = 2

        for no in sorted(self._nos):
            if cor[no] == 0:
                visita(no)
        return achados

    def em_ciclo(self) -> frozenset[str]:
        return frozenset(n for c in self.ciclos() for n in c)

    def camadas(self, concluidas: set[str] | None = None) -> list[list[str]]:
        """Ordem topologica agrupada: tudo numa camada pode rodar junto.

        Serve para visualizacao e para prova de que o paralelismo existe. O que
        de fato roda junto e decidido pelo scheduler, que ainda aplica conflito
        de recurso e limite de slots.
        """
        feitas = set(concluidas or ())
        restantes = {n for n in self._nos if n not in feitas}
        travadas = self.em_ciclo()
        saida: list[list[str]] = []
        while restantes:
            camada = sorted(
                n for n in restantes
                if n not in travadas and all(p in feitas for p in self._pais[n])
            )
            if not camada:
                break
            saida.append(camada)
            feitas.update(camada)
            restantes -= set(camada)
        return saida
