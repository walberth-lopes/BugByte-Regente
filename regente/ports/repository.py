# -*- coding: utf-8 -*-
"""RepositoryProvider: where the code lives.

Two decisions hold up this port:

**1. A name is not an identity.** A repository identifies itself by
`(provider, key)`, and the engine still scopes that by workspace before using it
as a lock or state key. A bare name fails in three ways already observed in the
real environment: the local directory may not match the remote repository; two
clients may have repositories of the same name; and the same repository may be
seen by two different providers at once.

**2. Power and permission are separate things.** `capabilities` says what the
adapter CAN do; the Policy Engine says what it MAY do. An adapter mounted for
reading only declares few capabilities and the policy is never even consulted; a
full adapter declares many and the policy is still what blocks. Mixing the two
notions produces the worst case: an `if can_write` scattered through the code,
which nobody can audit in one place.

Writing exists here only as a CONTRACT. The signatures are declared so that the
future design is visible and open to criticism now; no implementation in this
milestone executes them.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import Capability, Port


class RepoCapability(str, Enum):
    """What an adapter can do with a repository.

    Declared by the adapter, consulted by the engine before proposing work.
    Proposing an action the adapter does not implement wastes a whole cycle --
    and, worse, escalates to a human for a reason the engine could have foreseen
    on its own.
    """
    READ_METADATA = "read_metadata"
    READ_FILES = "read_files"
    READ_HISTORY = "read_history"
    READ_BRANCHES = "read_branches"
    READ_PULL_REQUESTS = "read_pull_requests"
    CLONE = "clone"
    # The write ones exist in the vocabulary so that the policy and the UI can
    # reason about them before any implementation exists.
    CREATE_BRANCH = "create_branch"
    COMMIT = "commit"
    PUSH = "push"
    OPEN_PR = "open_pr"
    REVIEW = "review"
    MERGE = "merge"


READ_CAPS: frozenset[RepoCapability] = frozenset({
    RepoCapability.READ_METADATA, RepoCapability.READ_FILES,
    RepoCapability.READ_HISTORY, RepoCapability.READ_BRANCHES,
    RepoCapability.READ_PULL_REQUESTS, RepoCapability.CLONE,
})

WRITE_CAPS: frozenset[RepoCapability] = frozenset(RepoCapability) - READ_CAPS


@dataclass(frozen=True, slots=True)
class RepoRef:
    """A repository's identity AT THE PROVIDER.

    It deliberately carries no workspace: the adapter should not need to know the
    tenancy to answer what it knows. What composes the full identity is the
    engine, with `scoped_to()` -- and it is that composed form, never the bare
    key, that becomes a lock, a resource or a row of state.
    """
    provider: str
    key: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.key}"

    def scoped_to(self, workspace_id: str) -> str:
        """The identity the engine uses. Unique within one deployment's universe."""
        return f"{workspace_id}/{self.provider}/{self.key}"

    def resource(self, workspace_id: str) -> str:
        """Mutual-exclusion key for the scheduler and for the lease."""
        return f"repo:{self.scoped_to(workspace_id)}"


@dataclass(frozen=True, slots=True)
class RepoInfo:
    """A repository as the provider describes it."""
    ref: RepoRef
    name: str
    #: The REAL integration branch, read from the provider.
    #:
    #: Never assume 'main'. Deriving a work branch from the wrong base produces a
    #: PR full of conflicts nobody asked for, and the error only shows up after
    #: the push -- by which point it has already cost the whole job.
    base_branch: str = ""
    #: Where to clone from. May be a remote URL or a local path.
    clone_origin: str | None = None
    #: Where a human sees this repository.
    url: str | None = None
    archived: bool = False
    private: bool | None = None
    capabilities: frozenset[RepoCapability] = field(default_factory=frozenset)
    #: True when the record came from a LISTING, with trimmed fields -- the same
    #: distinction that holds for tasks: "did not come" is not "is empty".
    partial: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def can(self, c: RepoCapability) -> bool:
        return c in self.capabilities

    @property
    def anomalies(self) -> tuple[str, ...]:
        findings = []
        if not self.partial and not self.base_branch:
            findings.append("no base branch -- deriving work from here is a guess")
        if not self.name.strip():
            findings.append("no readable name")
        if self.archived:
            findings.append("archived: accepts no new work")
        return tuple(findings)

    @property
    def usable(self) -> bool:
        """Can we work here? Archived and base-less both mean no."""
        return bool(self.base_branch) and not self.archived


@dataclass(frozen=True, slots=True)
class Branch:
    name: str
    sha: str = ""
    #: True when this is the repository's integration branch.
    e_base: bool = False
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    additions: int = 0
    deletions: int = 0
    status: str = "modified"


@dataclass(frozen=True, slots=True)
class PullRequest:
    numero: int
    repo: RepoRef
    title: str
    url: str
    state: str = "OPEN"
    #: The exact head SHA. Without it there is no telling a current review from
    #: a stale one -- and a stale review describes one piece of code while being
    #: displayed stuck to another.
    head_sha: str = ""
    branch: str = ""
    base: str = ""
    rascunho: bool = False
    autor: str = ""
    additions: int = 0
    deletions: int = 0
    files: tuple[FileChange, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Review:
    autor: str
    veredito: str          # APPROVED | CHANGES_REQUESTED | COMMENTED
    commit_sha: str = ""
    body: str = ""
    id: str = ""


class RepositoryProvider(Port):
    capability = Capability.REPOSITORY

    #: What this adapter, as mounted, can do.
    capabilities: frozenset[RepoCapability] = READ_CAPS

    # ---- discovery and reading -------------------------------------------

    @abstractmethod
    def list_repositories(self, filtro: dict[str, Any] | None = None) -> list[RepoInfo]:
        """Visible repositories. An error rises as AdapterError, never an empty list."""

    @abstractmethod
    def get_repository(self, key: str) -> RepoInfo:
        """Full detail of a repository, by the provider's key."""

    def list_branches(self, key: str, filtro: dict[str, Any] | None = None) -> list[Branch]:
        return []

    def read_file(self, key: str, path: str, ref: str | None = None) -> str:
        raise NotImplementedError

    def list_pull_requests(self, filtro: dict[str, Any] | None = None) -> list[PullRequest]:
        return []

    def get_pull_request(self, key: str, numero: int) -> PullRequest:
        raise NotImplementedError

    def list_reviews(self, key: str, numero: int) -> list[Review]:
        return []

    # ---- writing: contract declared, nothing implemented -----------------
    #
    # The signatures exist so that the future design is visible and open to
    # criticism now. Each one has the shape that prevents an already-known
    # defect -- which is why it is worth writing them before, and not after, the
    # defect happens.

    def create_branch(self, key: str, name: str, a_partir_de: str) -> Branch:
        """`a_partir_de` is mandatory: deriving from the implicit base is the
        short path to a PR born out of stale code."""
        raise NotImplementedError

    def create_commit(self, key: str, branch: str, message: str,
                      files: dict[str, str]) -> str:
        raise NotImplementedError

    def push(self, key: str, branch: str, esperado_sha: str | None = None) -> None:
        """`esperado_sha` allows refusing the push if the branch moved underfoot."""
        raise NotImplementedError

    def create_pull_request(self, key: str, branch: str, base: str,
                            title: str, body: str) -> PullRequest:
        raise NotImplementedError

    def submit_review(self, key: str, numero: int, head_sha: str,
                      body: str, veredito: str) -> Review:
        """`head_sha` is mandatory in the signature so that no adapter can
        publish 'against whatever head exists now'. The adapter must re-read the
        head and abort if it changed: a review that is born stale is worse than
        no review at all."""
        raise NotImplementedError

    def merge_pull_request(self, key: str, numero: int, metodo: str = "squash",
                           esperado_sha: str | None = None) -> None:
        raise NotImplementedError
