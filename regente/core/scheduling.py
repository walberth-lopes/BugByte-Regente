# -*- coding: utf-8 -*-
"""Scheduler: escolhe o que roda agora. Funcao pura, testavel sem banco.

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
class Candidata:
    """O que o scheduler precisa saber de uma task. Nada alem disso."""
    task_id: str
    prioridade: int = 100
    recursos: frozenset[str] = frozenset()
    chave: str = ""


@dataclass(frozen=True, slots=True)
class Limites:
    max_workers: int = 2
    max_despachos_dia: int = 8


@dataclass(frozen=True, slots=True)
class Adiada:
    task_id: str
    motivo: str


@dataclass(frozen=True, slots=True)
class Plano:
    despachar: tuple[str, ...] = ()
    adiadas: tuple[Adiada, ...] = ()
    #: Tasks que se autobloqueiam. Nao viram trabalho: viram pergunta ao humano.
    em_ciclo: tuple[str, ...] = ()

    @property
    def vazio(self) -> bool:
        return not self.despachar


def planeja(
    candidatas: list[Candidata],
    grafo: DependencyGraph,
    concluidas: set[str],
    em_execucao: dict[str, frozenset[str]],
    limites: Limites = Limites(),
    despachos_hoje: int = 0,
    nomes: dict[str, str] | None = None,
) -> Plano:
    """`em_execucao` mapeia task_id -> recursos que ela ja segurou.

    `nomes` traduz id interno para a chave que um humano reconhece. Um motivo de
    adiamento que cita id opaco obriga quem le a ir consultar o banco -- e o
    motivo existe justamente para evitar isso.

    Ordem de decisao: prioridade, depois chave. Ordenacao estavel importa mais do
    que parece -- sem ela, um empate faz o mesmo tick escolher tasks diferentes a
    cada execucao, e o motor fica indo e voltando sem terminar nada.
    """
    chave_de = (nomes or {})
    travadas = grafo.em_ciclo()
    desbloqueadas = grafo.desbloqueadas(concluidas)

    # Recursos ja ocupados por quem esta rodando. Um worker vivo tem posse.
    ocupados: set[str] = set()
    for recursos in em_execucao.values():
        ocupados |= set(recursos)

    livres = max(0, limites.max_workers - len(em_execucao))
    saldo_dia = max(0, limites.max_despachos_dia - despachos_hoje)

    despachar: list[str] = []
    adiadas: list[Adiada] = []

    for c in sorted(candidatas, key=lambda x: (x.prioridade, x.chave or x.task_id)):
        if c.task_id in travadas:
            continue  # reportada em `em_ciclo`, nunca despachada
        if c.task_id in em_execucao:
            continue
        if c.task_id not in desbloqueadas:
            pendentes = sorted(chave_de.get(p, p) for p in grafo.pais(c.task_id) - concluidas)
            adiadas.append(Adiada(c.task_id, f"depende de {', '.join(pendentes) or 'trabalho nao concluido'}"))
            continue

        colisao = c.recursos & ocupados
        if colisao:
            adiadas.append(Adiada(c.task_id, f"recurso ocupado: {', '.join(sorted(colisao))}"))
            continue
        if not livres:
            adiadas.append(Adiada(c.task_id, "sem slot livre"))
            continue
        if not saldo_dia:
            adiadas.append(Adiada(c.task_id, "teto diario de despachos atingido"))
            continue

        despachar.append(c.task_id)
        # Reserva ja aqui: duas candidatas do MESMO plano nao podem sair juntas
        # se compartilham recurso. Esquecer isto e o jeito classico de despachar
        # dois workers para a mesma migration no primeiro tick paralelo.
        ocupados |= c.recursos
        livres -= 1
        saldo_dia -= 1

    return Plano(
        despachar=tuple(despachar),
        adiadas=tuple(adiadas),
        em_ciclo=tuple(sorted(travadas)),
    )
