# -*- coding: utf-8 -*-
"""Portas: as capacidades que o Core Engine conhece.

Regra que define a arquitetura inteira: **estas interfaces sao orientadas a
capacidade, nunca a ferramenta.** `TaskProvider.transition()` existe porque todo
sistema de trabalho tem estados; `jira_transition_id` nao existe em lugar nenhum
daqui. Um nome de fornecedor nesta pasta e bug, e o teste de fronteira falha.

Falha de adapter nunca vira ausencia. Toda porta levanta `AdapterErro` quando a
operacao nao pode ser cumprida -- e proibido devolver lista vazia para dizer
"nao consegui perguntar". Confundir as duas coisas ja custou tabelas com milhoes
de linhas declaradas inexistentes.
"""

from __future__ import annotations

from enum import Enum

from ..core.errors import RegenteError


class AdapterError(RegenteError):
    """A operacao nao pode ser cumprida. NAO significa 'nao existe'."""


class ReadOnlyRefused(AdapterError):
    """Adapter montado em modo leitura recusou uma escrita."""


class Capability(str, Enum):
    TASKS = "tasks"
    REPOSITORY = "repository"
    CLOUD = "cloud"
    DATABASE = "database"
    CICD = "cicd"
    DEPLOYMENT = "deployment"
    NOTIFICATION = "notification"
    SECRETS = "secrets"
    LLM = "llm"
    WORKSPACE = "workspace"
    RUNNER = "runner"
    STORE = "store"


class Port:
    """Base comum. Todo adapter se identifica e declara o que sabe fazer."""

    capability: Capability
    #: Nome do adapter no registro, ex.: 'jira', 'github', 'filesystem'.
    name: str = "desconhecido"

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name}

    def verify(self) -> None:
        """Prova que o adapter funciona de verdade. Usado por `regente doctor`.

        Existe para que um error de credencial apareca no diagnostico, e nao no
        meio de um despacho.
        """
        return None
