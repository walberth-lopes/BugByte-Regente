# -*- coding: utf-8 -*-
"""CICDProvider e DeploymentProvider."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from . import Capability, Port


@dataclass(frozen=True, slots=True)
class Check:
    nome: str
    conclusao: str = ""     # SUCCESS | FAILURE | CANCELLED | ...
    estado: str = ""        # QUEUED | IN_PROGRESS | COMPLETED
    url: str = ""

    @property
    def rodando(self) -> bool:
        return self.estado in {"QUEUED", "IN_PROGRESS", "PENDING"}

    @property
    def verde(self) -> bool:
        return self.conclusao in {"SUCCESS", "NEUTRAL", "SKIPPED"}


@dataclass(frozen=True, slots=True)
class PipelineStatus:
    id: str
    estado: str
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
        return tuple(c.nome for c in self.checks if c.conclusao and not c.verde and not c.rodando)


class CICDProvider(Port):
    capability = Capability.CICD

    @abstractmethod
    def get_status(self, repo: str, referencia: str) -> PipelineStatus:
        """`referencia` e um SHA ou numero de PR, a criterio do adapter."""

    def get_logs(self, repo: str, execucao_id: str, limite: int = 200) -> list[str]:
        return []

    def run(self, repo: str, pipeline: str, parametros: dict[str, Any] | None = None) -> str:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Deployment:
    id: str
    ambiente: str
    versao: str
    estado: str = "IN_PROGRESS"
    url: str = ""
    dados: dict[str, Any] = field(default_factory=dict)


class DeploymentProvider(Port):
    capability = Capability.DEPLOYMENT

    def get_deployment(self, deployment_id: str) -> Deployment:
        raise NotImplementedError

    def deploy_staging(self, projeto: str, versao: str) -> Deployment:
        raise NotImplementedError

    def deploy_production(self, projeto: str, versao: str) -> Deployment:
        """Separado de staging na porta, de proposito.

        Um unico `deploy(ambiente)` faria a diferenca entre staging e producao
        virar o valor de uma string vinda do contexto -- exatamente o tipo de
        campo que um prompt injetado consegue mexer. Sao metodos distintos, com
        acoes distintas na policy e niveis de autonomia distintos.
        """
        raise NotImplementedError

    def rollback(self, projeto: str, ambiente: str,
                 para_versao: str | None = None) -> Deployment:
        raise NotImplementedError
