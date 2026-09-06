# -*- coding: utf-8 -*-
"""NotificationProvider, SecretProvider e LLMProvider."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from . import Capability, Port


class NotificationProvider(Port):
    capability = Capability.NOTIFICATION

    @abstractmethod
    def notify(self, title: str, body: str, urgency: str = "normal",
               link: str | None = None) -> None:
        """Chamar isto e caro em atencao humana. O motor limita por tick."""


class SecretProvider(Port):
    capability = Capability.SECRETS

    @abstractmethod
    def resolve(self, reference: str) -> str:
        """Resolve uma REFERENCIA a segredo.

        O valor devolvido nunca entra em prompt, evento, log ou estado. A porta
        existe para que o adapter possa usar o segredo dentro de uma operacao
        fechada -- o agente pede a operacao, nao a credencial.
        """

    def available(self, reference: str) -> bool:
        try:
            self.resolve(reference)
            return True
        except Exception:
            return False


@dataclass(frozen=True, slots=True)
class Message:
    role: str      # system | user | assistant
    content: str


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    model: str = ""
    tokens_entrada: int = 0
    tokens_saida: int = 0
    cost_usd: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Qual modelo usar. Resolvido por perfil, nunca fixado em codigo de agente.

    Perfis existem porque a escolha varia por tipo de agente, complexidade,
    risco, custo, organizacao e projeto -- seis eixos que nao cabem numa
    constante. O agente pede 'triagem' ou 'codigo'; a configuracao decide o
    modelo e o fornecedor.
    """
    profile: str
    #: Vazio de proposito: a porta nao tem fornecedor preferido.
    #: Quem escolhe e a configuracao do cliente.
    provider: str = ""
    model: str = ""
    max_tokens: int = 4096
    temperature: float = 0.0


class LLMProvider(Port):
    capability = Capability.LLM

    @abstractmethod
    def complete(self, mensagens: list[Message], spec: ModelSpec) -> Completion: ...

    def custo_estimado(self, tokens_entrada: int, tokens_saida: int,
                       spec: ModelSpec) -> float:
        return 0.0
