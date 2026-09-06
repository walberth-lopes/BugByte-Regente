# -*- coding: utf-8 -*-
"""Supervisor: orcamentos, deteccao de nao-progresso e escada de recuperacao.

Falha e esperada. O que nao pode ser tolerado e **falhar sem sair do lugar**: o
agente que reescreve o mesmo arquivo, colhe o mesmo erro e tenta de novo consome
orcamento inteiro sem produzir nada, e o motor precisa cortar isso sozinho.

A escada de recuperacao e finita de proposito:

    falhou -> retenta (backoff) -> troca de estrategia -> escala ao humano

Cada degrau precisa ser *diferente* do anterior. Retentar identico depois de um
erro deterministico e so gastar dinheiro mais devagar -- por isso a troca de
estrategia (outro agente, outro modelo) vem antes da segunda desistencia, e nao
depois da quinta tentativa igual.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..core.model import Run, RunState, Task, agora
from ..core.states import ATIVOS, TaskState


@dataclass(frozen=True, slots=True)
class Orcamento:
    max_iteracoes: int = 24
    max_tool_calls: int = 120
    max_custo_usd: float = 5.0
    max_segundos: int = 2700
    #: Tentativas por task antes de escalar. Tres degraus: original, retentativa,
    #: estrategia alternativa.
    max_tentativas: int = 3


@dataclass(frozen=True, slots=True)
class Veredito:
    """O que o supervisor manda fazer. Vocabulario fechado, nao texto livre."""
    parar: bool
    motivo: str = ""
    #: 'seguir' | 'retentar' | 'trocar_estrategia' | 'escalar' | 'abortar'
    proximo: str = "seguir"


def estoura_orcamento(run: Run, orc: Orcamento, quando: datetime | None = None) -> Veredito:
    ts = quando or agora()
    if run.iteracoes > orc.max_iteracoes:
        return Veredito(True, f"{run.iteracoes} iteracoes (teto {orc.max_iteracoes})", "escalar")
    if run.chamadas_tool > orc.max_tool_calls:
        return Veredito(True, f"{run.chamadas_tool} chamadas de tool (teto {orc.max_tool_calls})", "escalar")
    if run.custo_usd > orc.max_custo_usd:
        return Veredito(True, f"US$ {run.custo_usd:.2f} gastos (teto {orc.max_custo_usd:.2f})", "escalar")
    decorrido = (ts - run.iniciado_em).total_seconds()
    if decorrido > orc.max_segundos:
        return Veredito(True, f"{int(decorrido)}s decorridos (teto {orc.max_segundos}s)", "trocar_estrategia")
    return Veredito(False)


def _assinatura(texto: str) -> str:
    """Reduz uma mensagem a uma marca comparavel.

    Sem isso, 'timeout apos 30.2s' e 'timeout apos 31.7s' parecem erros
    diferentes e o detector de repeticao nunca dispara.
    """
    limpo = "".join(c for c in texto.lower() if not c.isdigit())
    return hashlib.sha1(" ".join(limpo.split()).encode("utf-8")).hexdigest()[:12]


@dataclass(slots=True)
class DetectorDeLoop:
    """Guarda marcas do que ja aconteceu neste run e acusa repeticao.

    Quatro padroes, todos com o mesmo significado -- o estado nao anda:
    mesmo erro, mesmo arquivo, mesmo teste, mesma decisao.
    """
    limite: int = 3
    _marcas: dict[str, int] = field(default_factory=dict)

    def registra(self, tipo: str, detalhe: str) -> int:
        chave = f"{tipo}:{_assinatura(detalhe)}"
        self._marcas[chave] = self._marcas.get(chave, 0) + 1
        return self._marcas[chave]

    def repetiu(self, tipo: str, detalhe: str) -> Veredito:
        n = self.registra(tipo, detalhe)
        if n >= self.limite:
            return Veredito(True, f"{tipo} repetido {n}x sem progresso: {detalhe[:120]}",
                            "trocar_estrategia")
        return Veredito(False)


def sem_progresso(task: Task, runs: list[Run], janela: int = 3) -> Veredito:
    """Acusa a task que consumiu varios runs e nao mudou de estado.

    Comparar estado entre runs -- e nao "o agente escreveu arquivos?" -- e o que
    diferencia trabalho de agitacao.
    """
    encerrados = [r for r in runs if r.estado is not RunState.RUNNING][-janela:]
    if len(encerrados) < janela:
        return Veredito(False)
    if all(r.estado in (RunState.FAILED, RunState.ABORTED) for r in encerrados):
        return Veredito(True, f"{janela} execucoes seguidas sem sair de {task.estado.value}", "escalar")
    return Veredito(False)


def proximo_degrau(task: Task, orc: Orcamento) -> str:
    """Escada de recuperacao, baseada em quantas vezes a task ja falhou."""
    if task.tentativas <= 0:
        return "retentar"
    if task.tentativas < orc.max_tentativas - 1:
        return "trocar_estrategia"
    return "escalar"


def espera_backoff(tentativas: int, base_segundos: int = 60, teto_segundos: int = 1800) -> timedelta:
    """Exponencial com teto. O teto existe para que uma task nao suma por horas."""
    return timedelta(seconds=min(teto_segundos, base_segundos * (2 ** max(0, tentativas))))


def estado_de_retomada(estado: TaskState) -> TaskState:
    """Para onde vai a task cujo worker morreu.

    Sempre READY -- e READY e o unico estado do qual o scheduler despacha. Manter
    a task no estado ativo para "preservar o progresso" nao preserva nada: o
    progresso vive na area de trabalho e na branch, nao no rotulo do estado, e a
    task fica viva no papel e parada de verdade.

    O que preserva o trabalho parcial e a area ser enderecada pela task, e nao
    pelo run: a proxima tentativa reabre a mesma arvore, com os commits WIP.
    """
    if estado in ATIVOS:
        return TaskState.READY
    return estado
