# -*- coding: utf-8 -*-
"""Scheduler: escolhe o que roda now. Funcao pura, testavel sem banco.

O objetivo declarado do motor e **trabalho util concluido por unidade de tempo**,
nao volume de analise. O scheduler e onde isso vira codigo: ele maximiza
paralelismo, mas nunca ao preco de dois workers se atropelando.

Nao paralelizar as cegas. Antes de abrir dois slots, o plano verifica:

- **dependencia**: os pais concluiram?
- **conflito de recurso**: os dois tocam o mesmo arquivo, migration, modulo,
  API ou repositorio em exclusividade?
- **ciclo**: o par se autobloqueia e ninguem percebeu?
- **limite**: ha slot, e o teto diario de despacho foi respeitado?

Conflito e declarado, nao adivinhado. Uma task diz quais chaves de recurso toca
(`repo:acme/api`, `migration:acme/api`, `file:src/auth.py`) e o scheduler trata
qualquer interseccao como exclusao mutua. Detectar conflito semantico de verdade
exige ler o codigo -- isso e trabalho de um agente de analise, que alimenta esta
lista. O scheduler nunca decide sozinho que dois diffs "provavelmente" convivem.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .graph import DependencyGraph


@dataclass(frozen=True, slots=True)
class Candidate:
    """O que o scheduler precisa saber de uma task. Nada alem disso."""
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
    #: Tasks que se autobloqueiam. Nao viram trabalho: viram pergunta ao humano.
    in_cycle: tuple[str, ...] = ()

    @property
    def vazio(self) -> bool:
        return not self.dispatch


def plan(
    candidates: list[Candidate],
    grafo: DependencyGraph,
    completed: set[str],
    running_now: dict[str, frozenset[str]],
    limits: Limits = Limits(),
    dispatched_today: int = 0,
    nomes: dict[str, str] | None = None,
) -> Plan:
    """`em_execucao` mapeia task_id -> recursos que ela ja segurou.

    `nomes` traduz id interno para a chave que um humano reconhece. Um motivo de
    adiamento que cita id opaco obriga quem le a ir consultar o banco -- e o
    motivo existe justamente para evitar isso.

    Ordem de decisao: prioridade, depois chave. Ordenacao estavel importa mais do
    que parece -- sem ela, um empate faz o mesmo tick escolher tasks diferentes a
    cada execucao, e o motor fica indo e voltando sem terminar nada.
    """
    chave_de = (nomes or {})
    locked = grafo.in_cycle()
    unblocked = grafo.unblocked(completed)

    # Recursos ja ocupados por quem esta rodando. Um worker vivo tem posse.
    taken: set[str] = set()
    for resources in running_now.values():
        taken |= set(resources)

    free_slots = max(0, limits.max_workers - len(running_now))
    daily_budget = max(0, limits.max_dispatches_per_day - dispatched_today)

    dispatch: list[str] = []
    deferred: list[Deferred] = []

    for c in sorted(candidates, key=lambda x: (x.priority, x.key or x.task_id)):
        if c.task_id in locked:
            continue  # reportada em `em_ciclo`, nunca despachada
        if c.task_id in running_now:
            continue
        if c.task_id not in unblocked:
            pending = sorted(chave_de.get(p, p) for p in grafo.parents(c.task_id) - completed)
            deferred.append(Deferred(c.task_id, f"depende de {', '.join(pending) or 'trabalho nao concluido'}"))
            continue

        collision = c.resources & taken
        if collision:
            deferred.append(Deferred(c.task_id, f"recurso ocupado: {', '.join(sorted(collision))}"))
            continue
        if not free_slots:
            deferred.append(Deferred(c.task_id, "sem slot livre"))
            continue
        if not daily_budget:
            deferred.append(Deferred(c.task_id, "teto diario de despachos atingido"))
            continue

        dispatch.append(c.task_id)
        # Reserva ja aqui: duas candidatas do MESMO plano nao podem sair juntas
        # se compartilham recurso. Esquecer isto e o jeito classico de despachar
        # dois workers para a mesma migration no primeiro tick paralelo.
        taken |= c.resources
        free_slots -= 1
        daily_budget -= 1

    return Plan(
        dispatch=tuple(dispatch),
        deferred=tuple(deferred),
        in_cycle=tuple(sorted(locked)),
    )
