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
        """Calling this is expensive in human attention. The engine caps it per tick."""


class SecretProvider(Port):
    capability = Capability.SECRETS

    @abstractmethod
    def resolve(self, reference: str) -> str:
        """Resolves a secret REFERENCE.

        The returned value never enters a prompt, event, log or state. The port
        exists so the adapter can use the secret inside a closed operation --
        the agent asks for the operation, not for the credential.
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
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Which model to use. Resolved by profile, never hard-coded in agent code.

    Profiles exist because the choice varies by agent type, complexity, risk,
    cost, organisation and project -- six axes that do not fit in a constant.
    The agent asks for 'triage' or 'code'; the configuration decides the model
    and the provider.
    """
    profile: str
    #: Empty on purpose: the port has no preferred provider.
    #: The client's configuration is what chooses.
    provider: str = ""
    model: str = ""
    max_tokens: int = 4096
    temperature: float = 0.0


class LLMProvider(Port):
    capability = Capability.LLM

    @abstractmethod
    def complete(self, messages: list[Message], spec: ModelSpec) -> Completion: ...

    def estimated_cost(self, input_tokens: int, output_tokens: int,
                       spec: ModelSpec) -> float:
        return 0.0
