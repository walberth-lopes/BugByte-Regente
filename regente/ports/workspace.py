# -*- coding: utf-8 -*-
"""WorkspaceProvider and AgentRunner: where the worker lives and how it is run."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from . import Capability, Port


@dataclass(frozen=True, slots=True)
class WorkArea:
    """A worker's isolated area.

    A worker never writes into another one's area. The isolation belongs to the
    provider -- worktree, container, VM -- and the engine only needs the path
    and the identifier to be able to clean up after a crash.
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
        """`key` identifies the UNIT OF WORK, not the attempt.

        Calling it twice with the same key returns the same area, with whatever
        was already there. That is what lets a resume after a crash find the WIP
        commits again instead of starting from scratch -- addressing it by run
        would throw away exactly the work recovery exists to save.
        """

    @abstractmethod
    def discard(self, area: WorkArea) -> None:
        """Releases the area. Must be safe to call on an area already lost.

        Cleanup happens after a crash, when the process that created the area no
        longer exists -- so 'it is not there any more' is success, not an error.
        """

    def list_areas(self) -> list[WorkArea]:
        return []


@dataclass(frozen=True, slots=True)
class RunRequest:
    """What the engine hands to a worker.

    `contexto` arrives already assembled and reduced: the engine collects what is
    needed and nothing more. Dumping the whole project in here is what makes an
    agent expensive, slow and imprecise all at once.
    """
    run_id: str
    task_id: str
    agent: str
    goal: str
    area: WorkArea
    contexto: dict[str, Any] = field(default_factory=dict)
    #: Actions this worker may even attempt. The Policy Engine still decides
    #: each call; this list merely avoids offering the agent what it could never
    #: use.
    tools: tuple[str, ...] = ()
    limit_iterations: int = 24
    limit_tool_calls: int = 120
    limit_cost_usd: float = 5.0
    limit_seconds: int = 2700


@dataclass(frozen=True, slots=True)
class RunResult:
    ok: bool
    summary: str
    #: How the worker finished: 'concluido', 'timebox', 'sem_progresso',
    #: 'orcamento', 'error', 'precisa_humano'. The engine decides the next step
    #: from this -- which is why it is a closed vocabulary, not free text. The
    #: values themselves stay as they are: the engine branches on them.
    outcome: str = "concluido"
    artifacts: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    tokens: int = 0
    tool_calls: int = 0
    iterations: int = 0
    #: Question for the human, when `outcome == 'precisa_humano'`.
    question: dict[str, Any] | None = None


class AgentRunner(Port):
    """Runs an agent. The implementation decides the substrate.

    This port is what keeps the engine from becoming hostage to one harness. A
    runner can be an off-the-shelf agentic harness, a loop of its own over
    LLMProvider, or a deterministic script. The Orchestrator does not change in
    any of those cases.
    """
    capability = Capability.RUNNER

    @abstractmethod
    def run(self, request: RunRequest) -> RunResult: ...

    def cancel(self, run_id: str) -> None:
        return None
