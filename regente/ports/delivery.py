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
    def running(self) -> bool:
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
    #: True when the provider confirmed that NO check exists.
    #: An empty list caused by a read failure is an AdapterError, never this.
    sem_checks: bool = False

    @property
    def concluido(self) -> bool:
        return bool(self.checks) and not any(c.running for c in self.checks)

    @property
    def falhou(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.checks if c.conclusion and not c.verde and not c.running)


class CICDProvider(Port):
    capability = Capability.CICD

    @abstractmethod
    def get_status(self, repo: str, reference: str) -> PipelineStatus:
        """`reference` is a SHA or a PR number, at the adapter's discretion."""

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
        """Separated from staging in the port, on purpose.

        A single `deploy(environment)` would make the difference between staging
        and production become the value of a string coming from the context --
        exactly the kind of field an injected prompt can move. These are
        distinct methods, with distinct policy actions and distinct autonomy
        levels.
        """
        raise NotImplementedError

    def rollback(self, project: str, environment: str,
                 to_version: str | None = None) -> Deployment:
        raise NotImplementedError
