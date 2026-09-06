# -*- coding: utf-8 -*-
"""WorkspaceProvider e AgentRunner: onde o worker vive e como ele e executado."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from . import Capability, Port


@dataclass(frozen=True, slots=True)
class WorkArea:
    """A area isolada de um worker.

    Um worker nunca escreve na area de outro. O isolamento e do provedor --
    worktree, container, VM -- e o motor so precisa do caminho e do identificador
    para conseguir limpar depois de um crash.
    """
    id: str
    path: str
    branch: str | None = None
    repo: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class WorkspaceProvider(Port):
    capability = Capability.WORKSPACE

    @abstractmethod
    def prepare(self, key: str, repo: str | None = None,
                branch: str | None = None, base: str | None = None) -> WorkArea:
        """`chave` identifica a UNIDADE DE TRABALHO, nao a tentativa.

        Chamar duas vezes com a mesma chave devolve a mesma area, com o que ja
        estava la. E isso que faz uma retomada apos crash reencontrar os commits
        WIP em vez de recomecar do zero -- endereca-la pelo run jogaria fora
        exatamente o trabalho que a recuperacao existe para salvar.
        """

    @abstractmethod
    def discard(self, area: WorkArea) -> None:
        """Solta a area. Precisa ser seguro chamar em area ja perdida.

        Limpeza acontece depois de crash, quando o processo que criou a area nao
        existe mais -- entao 'ja nao esta la' e success, nao error.
        """

    def list_areas(self) -> list[WorkArea]:
        return []

    # ---- writing history inside the area --------------------------------

    def commit(self, area: WorkArea, message: str,
               author: tuple[str, str] | None = None) -> str:
        """Record the area's current state on its own branch. Returns the SHA.

        Lives on THIS port and not on the repository port because the area is
        this provider's artefact -- it is the only component that knows how the
        area was materialised, and therefore the only one that can write to it
        without guessing.

        Implementations must refuse to commit onto the base branch. The engine
        already builds a work branch, so landing on the base can only mean
        something went wrong upstream, and a commit is the wrong place to
        discover it.
        """
        raise NotImplementedError

    def head(self, area: WorkArea) -> str:
        """Current commit of the area. Used to prove what a run produced."""
        raise NotImplementedError

    def is_dirty(self, area: WorkArea) -> bool:
        """Are there uncommitted changes? Distinguishes 'nothing to commit' from
        'the commit silently did nothing'."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class RunRequest:
    """O que o motor entrega a um worker.

    `contexto` ja vem montado e reduzido: o motor coleta o necessario e nada
    alem. Despejar o projeto inteiro aqui e o que torna um agente caro, lento e
    impreciso ao mesmo tempo.
    """
    run_id: str
    task_id: str
    agent: str
    goal: str
    area: WorkArea
    contexto: dict[str, Any] = field(default_factory=dict)
    #: Acoes que este worker pode sequer tentar. O Policy Engine ainda decide
    #: cada chamada; esta lista so evita oferecer ao agente o que ele nunca
    #: poderia usar.
    tools: tuple[str, ...] = ()
    limit_iterations: int = 24
    limit_tool_calls: int = 120
    limit_cost_usd: float = 5.0
    limit_seconds: int = 2700


@dataclass(frozen=True, slots=True)
class RunResult:
    ok: bool
    summary: str
    #: Como o worker terminou: 'concluido', 'timebox', 'sem_progresso',
    #: 'orcamento', 'error', 'precisa_humano'. O motor decide o proximo passo a
    #: partir daqui -- por isso e vocabulario fechado, nao texto livre.
    outcome: str = "concluido"
    artifacts: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    tokens: int = 0
    tool_calls: int = 0
    iterations: int = 0
    #: Pergunta ao humano, quando `desfecho == 'precisa_humano'`.
    question: dict[str, Any] | None = None


class AgentRunner(Port):
    """Executa um agente. A implementacao decide o substrato.

    Esta port e o que impede o motor de virar refem de um harness. Um runner
    pode ser um harness agentico ja pronto, um laco proprio sobre LLMProvider, ou
    um script deterministico. O Orchestrator nao muda em nenhum dos casos.
    """
    capability = Capability.RUNNER

    @abstractmethod
    def run(self, request: RunRequest) -> RunResult: ...

    def cancel(self, run_id: str) -> None:
        return None
