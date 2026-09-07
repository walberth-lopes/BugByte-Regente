# -*- coding: utf-8 -*-
"""WorkspaceProvider: onde o worker vive, e o que ele pode fazer la dentro."""

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

    def push_target(self, area: WorkArea) -> str | None:
        """Where a push from this area would go. `None` means nowhere.

        The engine must be able to ASK before it writes. A provider that cannot
        answer this cannot be trusted with a push, and `None` is a safe answer:
        it says a push is impossible, not that it is unconstrained.
        """
        return None

    def push(self, area: WorkArea, expected_sha: str,
             branch: str | None = None) -> str:
        """Publish the area's work branch to its push target. Returns the SHA.

        Lives here, and not on `RepositoryProvider`, for the same reason `commit`
        does: a push is a git operation FROM THE ISOLATED AREA, and only this
        provider knows how that area was materialised, where it points and
        whether it is clean. A repository port would have to learn about local
        checkouts to do this, which is precisely the coupling the ports exist to
        prevent -- and a provider that writes commits through an API has no
        concept of "push" at all.

        `expected_sha` is required, not optional. Pushing "whatever is on the
        branch now" publishes work nobody verified: between validation and push
        the branch may have moved, and the engine would be vouching for a commit
        it never saw. Implementations MUST re-read HEAD and refuse on mismatch.

        Implementations MUST also refuse:
          - an integration branch as the target,
          - a push target that is a local path,
          - any form of force push.
        """
        raise NotImplementedError

    def is_dirty(self, area: WorkArea) -> bool:
        """Are there uncommitted changes? Distinguishes 'nothing to commit' from
        'the commit silently did nothing'."""
        raise NotImplementedError


# The runner contract used to live here as a SECOND `AgentRunner`, with its own
# `RunRequest`/`RunResult`. Two ports claimed `Capability.RUNNER` and were not
# interchangeable: adapters registered under one name implemented `.run(RunRequest)`
# and adapters under another implemented `.execute(ExecutionRequest)`, so a
# configuration that picked the wrong one raised `AttributeError` deep in a
# mission instead of failing at composition.
#
# One capability, one port. The single contract lives in `ports/agent.py`.
