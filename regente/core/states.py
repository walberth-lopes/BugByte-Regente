# -*- coding: utf-8 -*-
"""A maquina de estados da unidade de trabalho.

Duas regras que valem para o arquivo inteiro:

1. **A tabela e explicita.** Nao existe "qualquer estado vai para qualquer
   estado". Transicao fora da tabela levanta `TransicaoInvalida` -- isso e o que
   transforma um bug de orquestracao em erro alto, em vez de virar uma task
   perdida num estado que ninguem sabe interpretar.

2. **`WAITING_HUMAN` guarda de onde veio.** Ele nao e um destino: e uma pausa. A
   task volta para o estado de origem quando o humano decide, e por isso a
   transicao de saida e validada contra `retomaveis()`, nao contra uma lista
   fixa. Sem isso, uma aprovacao de deploy devolveria a task para o comeco.
"""

from __future__ import annotations

from enum import Enum

from .errors import TransicaoInvalida


class TaskState(str, Enum):
    DISCOVERED = "DISCOVERED"
    ANALYZING = "ANALYZING"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    PR_CREATED = "PR_CREATED"
    CI_RUNNING = "CI_RUNNING"
    AI_REVIEW = "AI_REVIEW"
    WAITING_HUMAN = "WAITING_HUMAN"
    APPROVED = "APPROVED"
    MERGING = "MERGING"
    DEPLOYING = "DEPLOYING"
    QA_STAGING = "QA_STAGING"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


S = TaskState

#: Estados dos quais nada mais sai. Trabalho aqui nao volta a ser agendado.
TERMINAIS: frozenset[TaskState] = frozenset({S.DONE, S.CANCELLED})

#: Estados em que existe um worker vivo (ou deveria existir). Sao os que a
#: recuperacao pos-crash precisa varrer.
ATIVOS: frozenset[TaskState] = frozenset({
    S.ASSIGNED, S.IMPLEMENTING, S.TESTING, S.CI_RUNNING,
    S.AI_REVIEW, S.MERGING, S.DEPLOYING,
})

#: Saidas de emergencia disponiveis a partir de qualquer estado nao-terminal.
#: `WAITING_HUMAN` esta aqui porque escalar e sempre legitimo -- o motor nunca
#: fica sem a opcao de parar e perguntar.
_ESCAPES: frozenset[TaskState] = frozenset({
    S.BLOCKED, S.FAILED, S.CANCELLED, S.WAITING_HUMAN,
})

#: Devolucao a fila: o worker sumiu e o trabalho volta a ser agendavel.
#:
#: Sem esta transicao, a task recuperada de um crash fica num estado ativo que
#: nenhum tick agenda -- viva no papel e parada de verdade. E o pior modo de
#: falha possivel para um motor que promete retomar sozinho, porque nada acusa:
#: nao ha erro, nao ha fila, so uma task que nunca mais anda.
_DEVOLVEM_A_FILA: frozenset[TaskState] = ATIVOS

#: Transicoes de progresso. As saidas de emergencia sao somadas depois.
_AVANCOS: dict[TaskState, frozenset[TaskState]] = {
    S.DISCOVERED:    frozenset({S.ANALYZING}),
    S.ANALYZING:     frozenset({S.READY}),
    S.READY:         frozenset({S.ASSIGNED}),
    # ASSIGNED volta a READY quando o worker morre antes de comecar: a task
    # perde o dono e volta para a fila, sem passar por FAILED.
    S.ASSIGNED:      frozenset({S.IMPLEMENTING, S.READY}),
    S.IMPLEMENTING:  frozenset({S.TESTING}),
    # TESTING volta a IMPLEMENTING no ciclo normal de correcao: teste vermelho
    # nao e falha do motor, e trabalho.
    S.TESTING:       frozenset({S.PR_CREATED, S.IMPLEMENTING}),
    S.PR_CREATED:    frozenset({S.CI_RUNNING, S.AI_REVIEW}),
    S.CI_RUNNING:    frozenset({S.AI_REVIEW, S.IMPLEMENTING}),
    S.AI_REVIEW:     frozenset({S.APPROVED, S.IMPLEMENTING}),
    S.APPROVED:      frozenset({S.MERGING}),
    # MERGING pode ir direto a DONE em projeto sem deploy governado pelo motor.
    S.MERGING:       frozenset({S.DEPLOYING, S.DONE}),
    S.DEPLOYING:     frozenset({S.QA_STAGING, S.DONE}),
    # QA reprovada devolve a task ao codigo -- o caminho mais caro e o mais comum.
    S.QA_STAGING:    frozenset({S.DONE, S.IMPLEMENTING}),
    # Desbloquear/retentar reentra pela analise: o mundo mudou desde que parou.
    S.BLOCKED:       frozenset({S.READY, S.ANALYZING}),
    S.FAILED:        frozenset({S.READY, S.ANALYZING}),
    S.WAITING_HUMAN: frozenset(),   # saida e calculada, ver `retomaveis()`
    S.DONE:          frozenset(),
    S.CANCELLED:     frozenset(),
}


def permitidas(origem: TaskState) -> frozenset[TaskState]:
    """Todos os destinos legais a partir de `origem`."""
    if origem in TERMINAIS:
        return frozenset()
    saida = _AVANCOS[origem] | (_ESCAPES - {origem})
    if origem in _DEVOLVEM_A_FILA:
        saida |= {S.READY}
    return saida


def retomaveis(pausado_em: TaskState) -> frozenset[TaskState]:
    """Destinos legais ao sair de WAITING_HUMAN, dado o estado em que pausou.

    O humano pode: mandar seguir (o proprio estado de origem), mandar refazer
    (o que aquele estado ja alcancava) ou encerrar. Ele nao pode teletransportar
    a task para um estado que ela nao alcancaria sozinha -- aprovar um deploy nao
    e o mesmo que declarar a task pronta.
    """
    if pausado_em in TERMINAIS:
        return frozenset()
    return frozenset({pausado_em}) | _AVANCOS[pausado_em] | (_ESCAPES - {S.WAITING_HUMAN})


def pode(origem: TaskState, destino: TaskState, pausado_em: TaskState | None = None) -> bool:
    if origem is S.WAITING_HUMAN:
        if pausado_em is None:
            # Sem memoria de onde pausou, so restam as saidas que nao dependem
            # dela. Devolver a task ao fluxo exigiria adivinhar.
            return destino in (_ESCAPES - {S.WAITING_HUMAN})
        return destino in retomaveis(pausado_em)
    return destino in permitidas(origem)


def exige(origem: TaskState, destino: TaskState, pausado_em: TaskState | None = None) -> None:
    """Valida ou levanta. Unico ponto por onde uma transicao entra no motor."""
    if not pode(origem, destino, pausado_em):
        contexto = f" (pausada em {pausado_em.value})" if pausado_em else ""
        raise TransicaoInvalida(
            f"{origem.value} -> {destino.value} nao e uma transicao valida{contexto}"
        )


def e_terminal(estado: TaskState) -> bool:
    return estado in TERMINAIS


def e_ativo(estado: TaskState) -> bool:
    return estado in ATIVOS
