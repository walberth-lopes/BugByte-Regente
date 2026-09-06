# -*- coding: utf-8 -*-
"""CICDProvider e DeploymentProvider."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from . import Capability, Port


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    conclusion: str = ""     # SUCCESS | FAILURE | CANCELLED | ...
    state: str = ""        # QUEUED | IN_PROGRESS | COMPLETED
    url: str = ""

    @property
    def rodando(self) -> bool:
        return self.state in {"QUEUED", "IN_PROGRESS", "PENDING"}

    @property
    def verde(self) -> bool:
        return self.conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}


@dataclass(frozen=True, slots=True)
class PipelineStatus:
    id: str
    state: str
    checks: tuple[Check, ...] = ()
    url: str = ""
    #: True quando o provedor confirmou que NAO existe nenhum check.
    #: Lista vazia por falha de leitura e AdapterErro, nunca isto.
    sem_checks: bool = False

    @property
    def concluido(self) -> bool:
        return bool(self.checks) and not any(c.rodando for c in self.checks)

    @property
    def falhou(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.checks if c.conclusion and not c.verde and not c.rodando)


class CICDProvider(Port):
    capability = Capability.CICD

    @abstractmethod
    def get_status(self, repo: str, reference: str) -> PipelineStatus:
        """`referencia` e um SHA ou numero de PR, a criterio do adapter."""

    def get_logs(self, repo: str, execucao_id: str, limit: int = 200) -> list[str]:
        return []

    def run(self, repo: str, pipeline: str, parametros: dict[str, Any] | None = None) -> str:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Deployment:
    id: str
    environment: str
    version: str
    state: str = "IN_PROGRESS"
    url: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class DeploymentProvider(Port):
    capability = Capability.DEPLOYMENT

    def get_deployment(self, deployment_id: str) -> Deployment:
        raise NotImplementedError

    def deploy_staging(self, project: str, version: str) -> Deployment:
        raise NotImplementedError

    def deploy_production(self, project: str, version: str) -> Deployment:
        """Separado de staging na porta, de proposito.

        Um unico `deploy(ambiente)` faria a diferenca entre staging e producao
        virar o valor de uma string vinda do contexto -- exatamente o tipo de
        campo que um prompt injetado consegue mexer. Sao metodos distintos, com
        acoes distintas na policy e niveis de autonomia distintos.
        """
        raise NotImplementedError

    def rollback(self, project: str, environment: str,
                 to_version: str | None = None) -> Deployment:
        raise NotImplementedError
