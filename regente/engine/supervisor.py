# -*- coding: utf-8 -*-
"""Supervisor: orcamentos, deteccao de nao-progresso e escada de recuperacao.

Falha e esperada. O que nao pode ser tolerado e **falhar sem sair do lugar**: o
agente que reescreve o mesmo arquivo, colhe o mesmo error e tenta de novo consome
orcamento inteiro sem produzir nada, e o motor precisa cortar isso sozinho.

A escada de recuperacao e finita de proposito:

    falhou -> retenta (backoff) -> troca de estrategia -> escala ao humano

Cada degrau precisa ser *diferente* do anterior. Retentar identico depois de um
error deterministico e so gastar dinheiro mais devagar -- por isso a troca de
estrategia (outro agente, outro modelo) vem antes da segunda desistencia, e nao
depois da quinta tentativa igual.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..core.model import Run, RunState, Task, now
from ..core.states import ACTIVE, TaskState


@dataclass(frozen=True, slots=True)
class Budget:
    max_iterations: int = 24
    max_tool_calls: int = 120
    max_cost_usd: float = 5.0
    max_seconds: int = 2700
    #: Tentativas por task antes de escalar. Tres degraus: original, retentativa,
    #: estrategia alternativa.
    max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class StopVerdict:
    """O que o supervisor manda fazer. Vocabulario fechado, nao texto livre."""
    stop: bool
    reason: str = ""
    #: 'seguir' | 'retentar' | 'trocar_estrategia' | 'escalar' | 'abortar'
    next_action: str = "seguir"


def over_budget(run: Run, orc: Budget, when: datetime | None = None) -> StopVerdict:
    ts = when or now()
    if run.iterations > orc.max_iterations:
        return StopVerdict(True, f"{run.iterations} iteracoes (teto {orc.max_iterations})", "escalar")
    if run.tool_calls > orc.max_tool_calls:
        return StopVerdict(True, f"{run.tool_calls} chamadas de tool (teto {orc.max_tool_calls})", "escalar")
    if run.cost_usd > orc.max_cost_usd:
        return StopVerdict(True, f"US$ {run.cost_usd:.2f} gastos (teto {orc.max_cost_usd:.2f})", "escalar")
    elapsed = (ts - run.started_at).total_seconds()
    if elapsed > orc.max_seconds:
        return StopVerdict(True, f"{int(elapsed)}s decorridos (teto {orc.max_seconds}s)", "trocar_estrategia")
    return StopVerdict(False)


def _signature(text: str) -> str:
    """Reduz uma mensagem a uma marca comparavel.

    Sem isso, 'timeout apos 30.2s' e 'timeout apos 31.7s' parecem erros
    diferentes e o detector de repeticao nunca dispara.
    """
    limpo = "".join(c for c in text.lower() if not c.isdigit())
    return hashlib.sha1(" ".join(limpo.split()).encode("utf-8")).hexdigest()[:12]


@dataclass(slots=True)
class LoopDetector:
    """Guarda marcas do que ja aconteceu neste run e acusa repeticao.

    Quatro padroes, todos com o mesmo significado -- o estado nao anda:
    mesmo error, mesmo arquivo, mesmo teste, mesma decisao.
    """
    limit: int = 3
    _marcas: dict[str, int] = field(default_factory=dict)

    def register(self, kind: str, detail: str) -> int:
        key = f"{kind}:{_signature(detail)}"
        self._marcas[key] = self._marcas.get(key, 0) + 1
        return self._marcas[key]

    def repeated(self, kind: str, detail: str) -> StopVerdict:
        n = self.register(kind, detail)
        if n >= self.limit:
            return StopVerdict(True, f"{kind} repetido {n}x sem progresso: {detail[:120]}",
                            "trocar_estrategia")
        return StopVerdict(False)


def no_progress(task: Task, runs: list[Run], window: int = 3) -> StopVerdict:
    """Acusa a task que consumiu varios runs e nao mudou de estado.

    Comparar estado entre runs -- e nao "o agente escreveu arquivos?" -- e o que
    diferencia trabalho de agitacao.
    """
    finished_runs = [r for r in runs if r.state is not RunState.RUNNING][-window:]
    if len(finished_runs) < window:
        return StopVerdict(False)
    if all(r.state in (RunState.FAILED, RunState.ABORTED) for r in finished_runs):
        return StopVerdict(True, f"{window} execucoes seguidas sem sair de {task.state.value}", "escalar")
    return StopVerdict(False)


def next_recovery_step(task: Task, orc: Budget) -> str:
    """Escada de recuperacao, baseada em quantas vezes a task ja falhou."""
    if task.attempts <= 0:
        return "retentar"
    if task.attempts < orc.max_attempts - 1:
        return "trocar_estrategia"
    return "escalar"


def backoff_delay(attempts: int, base_segundos: int = 60, teto_segundos: int = 1800) -> timedelta:
    """Exponencial com teto. O teto existe para que uma task nao suma por horas."""
    return timedelta(seconds=min(teto_segundos, base_segundos * (2 ** max(0, attempts))))


def resume_state(state: TaskState) -> TaskState:
    """Para onde vai a task cujo worker morreu.

    Sempre READY -- e READY e o unico estado do qual o scheduler despacha. Manter
    a task no estado ativo para "preservar o progresso" nao preserva nada: o
    progresso vive na area de trabalho e na branch, nao no rotulo do estado, e a
    task fica viva no papel e parada de verdade.

    O que preserva o trabalho parcial e a area ser enderecada pela task, e nao
    pelo run: a proxima tentativa reabre a mesma arvore, com os commits WIP.
    """
    if state in ACTIVE:
        return TaskState.READY
    return state
