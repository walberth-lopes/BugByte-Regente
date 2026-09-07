# -*- coding: utf-8 -*-
"""A maquina de estados da unidade de trabalho.

Duas regras que valem para o arquivo inteiro:

1. **A tabela e explicita.** Nao existe "qualquer estado vai para qualquer
   estado". Transicao fora da tabela levanta `TransicaoInvalida` -- isso e o que
   transforma um bug de orquestracao em error alto, em vez de virar uma task
   perdida num estado que ninguem sabe interpretar.

2. **`WAITING_HUMAN` guarda de onde veio.** Ele nao e um destino: e uma pausa. A
   task volta para o estado de origem quando o humano decide, e por isso a
   transicao de saida e validada contra `retomaveis()`, nao contra uma lista
   fixa. Sem isso, uma aprovacao de deploy devolveria a task para o comeco.
"""

from __future__ import annotations

from enum import Enum

from .errors import InvalidTransition


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

#: O que cada estado SIGNIFICA, em uma frase. Mora aqui porque e vocabulario da
#: maquina de estados, nao da tela: uma segunda copia numa UI vira, na primeira
#: divergencia, duas verdades sobre o mesmo estado -- e a que o operador le e a
#: errada.
MEANING: dict[TaskState, str] = {
    S.DISCOVERED: "vista na origem; ainda nao analisada",
    S.ANALYZING: "o motor esta decidindo se e alvo, e qual",
    S.READY: "elegivel para despacho; esperando um slot",
    S.ASSIGNED: "reservada por um worker; area ainda nao aberta",
    S.IMPLEMENTING: "um agente esta trabalhando dentro de uma area isolada",
    S.TESTING: "a mudanca existe e esta sendo verificada pelo motor",
    S.PR_CREATED: "a mudanca esta num pull request, aguardando",
    S.CI_RUNNING: "os checks do commit estao sendo observados",
    S.AI_REVIEW: "aguardando parecer de revisao",
    S.WAITING_HUMAN: "parada por decisao de uma pessoa",
    S.APPROVED: "decidida por uma pessoa; liberada para seguir",
    S.MERGING: "sendo integrada",
    S.DEPLOYING: "sendo publicada",
    S.QA_STAGING: "em verificacao apos publicacao",
    S.DONE: "encerrada com trabalho entregue",
    S.BLOCKED: "impedida por algo fora do trabalho em si",
    S.FAILED: "a tentativa falhou; pode ser retomada",
    S.CANCELLED: "encerrada sem entrega, por decisao",
}


def describe(state: TaskState) -> str:
    """Uma frase sobre o estado, ou o silencio admitido.

    Devolver o proprio nome seria pior que devolver vazio: a tela mostraria
    "TESTING: TESTING" e ninguem notaria que a descricao nunca foi escrita.
    """
    return MEANING.get(state, "")


#: Estados dos quais nada mais sai. Trabalho aqui nao volta a ser agendado.
TERMINAL: frozenset[TaskState] = frozenset({S.DONE, S.CANCELLED})

#: Estados em que existe um worker vivo (ou deveria existir). Sao os que a
#: recuperacao pos-crash precisa varrer.
ACTIVE: frozenset[TaskState] = frozenset({
    S.ASSIGNED, S.IMPLEMENTING, S.TESTING, S.CI_RUNNING,
    S.AI_REVIEW, S.MERGING, S.DEPLOYING,
})

#: Estados ativos em que o motor espera por um sistema DE FORA -- CI, revisao
#: humana -- e nao por um worker seu.
#:
#: A diferenca importa para recuperacao. Um estado ativo comum implica um run
#: vivo, e a ausencia dele significa que a task ficou orfa. Estes nao: nenhum
#: processo esta dentro deles por definicao, e exigir um run ativo aqui faria a
#: recuperacao devolver a fila uma task que apenas aguarda o CI responder.
#: O que precisa existir para estes e um registro de entrega -- sem ele nao ha
#: a que voltar, e ai sim a task esta perdida.
AWAITING_EXTERNAL: frozenset[TaskState] = frozenset({
    S.PR_CREATED, S.CI_RUNNING, S.AI_REVIEW,
})

#: Estados ativos com um worker do motor dentro. Sempre implicam um run vivo.
#: A ausencia de um run aqui e uma task orfa; em AWAITING_EXTERNAL nao e.
OWNED_ACTIVE: frozenset[TaskState] = ACTIVE - AWAITING_EXTERNAL

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
#: nao ha error, nao ha fila, so uma task que nunca mais anda.
_DEVOLVEM_A_FILA: frozenset[TaskState] = ACTIVE

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


#: States this engine currently has code to move a task OUT of.
#:
#: Not the same thing as `_AVANCOS`, which says which transitions are *legal*.
#: A transition can be perfectly legal and have nobody who performs it, and that
#: gap is invisible: the task sits in a busy-looking state, the scheduler skips
#: it because it appears to be in progress, and the engine reports quiet, clean
#: ticks forever.
#:
#: That is not hypothetical. A soak run found every successfully dispatched task
#: parked in `TESTING` on its fourth tick -- legal to leave, and no code in the
#: project leaving it. The pipeline beyond `TESTING` belongs to milestones that
#: do not exist yet, so the honest thing is for the engine to know where its own
#: road ends and say so, rather than drive tasks off it.
#:
#: When a later milestone adds the stage that advances a state, it adds the
#: state here. `test_states.py` checks the two sets against each other, so
#: forgetting is loud.
ENGINE_ADVANCES: frozenset[TaskState] = frozenset({
    S.DISCOVERED, S.READY, S.ASSIGNED, S.IMPLEMENTING, S.FAILED, S.BLOCKED,
    S.WAITING_HUMAN,
    # O tick le os checks de uma entrega em voo e, com resposta conclusiva ou
    # com a espera esgotada, entrega a uma pessoa. CI_RUNNING deixou de ser
    # beco sem saida quando esse observador passou a existir -- e so por isso.
    S.CI_RUNNING,
})


def engine_can_advance(state: TaskState) -> bool:
    return state in ENGINE_ADVANCES


def is_terminus(state: TaskState) -> bool:
    """An active state the engine can enter and cannot leave.

    The dangerous shape: it looks like work in progress and it is a dead end.
    """
    return state in ACTIVE and state not in ENGINE_ADVANCES


def allowed_from(source: TaskState) -> frozenset[TaskState]:
    """Todos os destinos legais a partir de `origem`."""
    if source in TERMINAL:
        return frozenset()
    output = _AVANCOS[source] | (_ESCAPES - {source})
    if source in _DEVOLVEM_A_FILA:
        output |= {S.READY}
    return output


def resumable_from(pausado_em: TaskState) -> frozenset[TaskState]:
    """Destinos legais ao sair de WAITING_HUMAN, dado o estado em que pausou.

    O humano pode: mandar seguir (o proprio estado de origem), mandar refazer
    (o que aquele estado ja alcancava), devolver a fila ou encerrar. Ele nao pode
    teletransportar a task para um estado que ela nao alcancaria sozinha --
    aprovar um deploy nao e o mesmo que declarar a task pronta.

    A devolucao a fila estava faltando aqui, e a falta era ao contrario do que a
    propria regra diz: `allowed_from` ja deixa qualquer estado ativo voltar a
    READY, entao escalar uma task REDUZIA as opcoes do humano abaixo das que o
    motor tinha sozinho. Na pratica isso fechava o unico caminho util depois de
    uma escalada -- mandar refazer -- e a decisao morria com InvalidTransition.
    Achado por uma corrida longa, nao por leitura.
    """
    if pausado_em in TERMINAL:
        return frozenset()
    saidas = (frozenset({pausado_em}) | _AVANCOS[pausado_em]
              | (_ESCAPES - {S.WAITING_HUMAN}))
    if pausado_em in _DEVOLVEM_A_FILA:
        saidas |= {S.READY}
    return saidas


def can(source: TaskState, destination: TaskState, pausado_em: TaskState | None = None) -> bool:
    if source is S.WAITING_HUMAN:
        if pausado_em is None:
            # Sem memoria de onde pausou, so restam as saidas que nao dependem
            # dela. Devolver a task ao fluxo exigiria adivinhar.
            return destination in (_ESCAPES - {S.WAITING_HUMAN})
        return destination in resumable_from(pausado_em)
    return destination in allowed_from(source)


def require(source: TaskState, destination: TaskState, pausado_em: TaskState | None = None) -> None:
    """Valida ou levanta. Unico ponto por onde uma transicao entra no motor."""
    if not can(source, destination, pausado_em):
        contexto = f" (pausada em {pausado_em.value})" if pausado_em else ""
        raise InvalidTransition(
            f"{source.value} -> {destination.value} nao e uma transicao valida{contexto}"
        )


def is_terminal(state: TaskState) -> bool:
    return state in TERMINAL


def is_active(state: TaskState) -> bool:
    return state in ACTIVE
