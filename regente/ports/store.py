# -*- coding: utf-8 -*-
"""Store: o estado que sobrevive ao processo.

O motor nunca depende do contexto de conversa de um agente para saber onde o
trabalho parou. Tudo o que importa esta aqui, e a consequencia e direta: matar o
processo no meio de um despacho e uma operacao suportada, nao um acidente.

`transiciona()` e `adquire_lease()` sao os dois pontos que precisam ser atomicos.
Sem atomicidade na transicao, dois ticks concorrentes despacham a mesma task; sem
atomicidade no lease, dois workers escrevem no mesmo repositorio.
"""

from __future__ import annotations

from abc import abstractmethod
from datetime import datetime

from ..core.model import (ActionRecord, Approval, Dependency, Event, Lease, Project,
                          Repository, Run, Task, Workspace)
from ..core.states import TaskState
from . import Capability, Port


class Store(Port):
    capability = Capability.STORE

    # ---- esquema e tenancy ----------------------------------------------
    @abstractmethod
    def migra(self) -> None: ...

    @abstractmethod
    def salva_workspace(self, w: Workspace) -> None: ...

    @abstractmethod
    def workspace(self, workspace_id: str) -> Workspace | None: ...

    @abstractmethod
    def workspaces(self) -> list[Workspace]: ...

    @abstractmethod
    def salva_project(self, p: Project) -> None: ...

    @abstractmethod
    def projects(self, workspace_id: str) -> list[Project]: ...

    @abstractmethod
    def salva_repository(self, r: Repository) -> None: ...

    # ---- trabalho --------------------------------------------------------
    @abstractmethod
    def salva_task(self, t: Task) -> None: ...

    @abstractmethod
    def task(self, task_id: str) -> Task | None: ...

    @abstractmethod
    def task_por_chave(self, workspace_id: str, provider: str, key: str) -> Task | None: ...

    @abstractmethod
    def tasks(self, workspace_id: str, estados: list[TaskState] | None = None) -> list[Task]: ...

    @abstractmethod
    def transiciona(self, task_id: str, destino: TaskState, ator: str,
                    motivo: str = "", dados: dict | None = None) -> Task:
        """Valida a transicao, grava e emite evento -- tudo na mesma transacao."""

    @abstractmethod
    def liga_dependencia(self, d: Dependency) -> None: ...

    @abstractmethod
    def dependencias(self, workspace_id: str) -> list[Dependency]: ...

    # ---- execucao --------------------------------------------------------
    @abstractmethod
    def salva_run(self, r: Run) -> None: ...

    @abstractmethod
    def run(self, run_id: str) -> Run | None: ...

    @abstractmethod
    def runs_ativos(self, workspace_id: str) -> list[Run]: ...

    @abstractmethod
    def runs_da_task(self, task_id: str) -> list[Run]: ...

    # ---- trilha ----------------------------------------------------------
    @abstractmethod
    def anota(self, e: Event) -> None: ...

    @abstractmethod
    def eventos(self, workspace_id: str, task_id: str | None = None,
                limite: int = 100) -> list[Event]: ...

    @abstractmethod
    def registra_acao(self, a: ActionRecord) -> None: ...

    @abstractmethod
    def acoes(self, workspace_id: str, limite: int = 100) -> list[ActionRecord]: ...

    # ---- escalonamento ---------------------------------------------------
    @abstractmethod
    def abre_approval(self, a: Approval) -> None: ...

    @abstractmethod
    def approvals_abertos(self, workspace_id: str) -> list[Approval]: ...

    @abstractmethod
    def approval(self, approval_id: str) -> Approval | None: ...

    @abstractmethod
    def decide_approval(self, approval_id: str, escolha: str, por: str,
                        nota: str = "") -> Approval: ...

    # ---- travas ----------------------------------------------------------
    @abstractmethod
    def adquire_lease(self, recurso: str, dono: str, workspace_id: str,
                      segundos: int) -> Lease | None:
        """Devolve None quando ha lease vivo de outro dono. Nunca espera.

        A trava e por (workspace, recurso). Recurso homonimo em dois clientes
        sao dois recursos -- um cliente nunca segura a fila do outro.
        """

    @abstractmethod
    def renova_lease(self, recurso: str, dono: str, segundos: int,
                     workspace_id: str | None = None) -> bool: ...

    @abstractmethod
    def solta_lease(self, recurso: str, dono: str,
                    workspace_id: str | None = None) -> None: ...

    @abstractmethod
    def leases_vencidos(self, workspace_id: str, agora: datetime | None = None) -> list[Lease]: ...

    # ---- contadores ------------------------------------------------------
    @abstractmethod
    def conta_despachos(self, workspace_id: str, dia: str) -> int: ...

    @abstractmethod
    def marca_despacho(self, workspace_id: str, dia: str) -> None: ...
