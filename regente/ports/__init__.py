# -*- coding: utf-8 -*-
"""Ports: the capabilities the Core Engine knows about.

The rule that defines the whole architecture: **these interfaces are oriented
towards capability, never towards a tool.** `TaskProvider.transition()` exists
because every work system has states; `jira_transition_id` appears nowhere in
here. A provider name in this folder is a bug, and the boundary test fails.

An adapter failure never becomes an absence. Every port raises `AdapterError`
when the operation cannot be carried out -- returning an empty list to say "I
could not ask" is forbidden. Confusing the two has already cost tables with
millions of rows being declared non-existent.
"""

from __future__ import annotations

from enum import Enum

from ..core.errors import RegenteError


class AdapterError(RegenteError):
    """The operation could not be carried out. It does NOT mean 'does not exist'."""


class ReadOnlyRefused(AdapterError):
    """An adapter mounted in read-only mode refused a write."""


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
    """Common base. Every adapter identifies itself and declares what it can do."""

    capability: Capability
    #: Adapter name in the registry, e.g. 'jira', 'github', 'filesystem'.
    name: str = "unknown"

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name}

    def verify(self) -> None:
        """Proves the adapter genuinely works. Used by `regente doctor`.

        It exists so that a credential error shows up in the diagnosis, and not
        in the middle of a dispatch.
        """
        return None
