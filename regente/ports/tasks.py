# -*- coding: utf-8 -*-
"""TaskProvider: where the work comes from."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import Capability, Port


class ExternalStatus(str, Enum):
    """Where the work stands, as understood by whoever issued it.

    It exists because raw `external_status` is free text, and the engine needs
    ONE decision out of it: is this work available, is someone already on it, or
    is it over? Without that the Core would have to know every provider's status
    names -- which is exactly the coupling the port prevents.

    The vocabulary is the smallest one that answers that question. It is not a
    translation of any tool's flow: it is the position in the life cycle, which
    every work system has.

    Member and value are kept identical. Old databases are migrated by
    `_v4_to_v5` in the SQLite store; the Jira and YAML status names these map
    FROM are untouched, since those belong to the provider.
    """
    NOT_STARTED = "NOT_STARTED"
    IN_ANALYSIS = "IN_ANALYSIS"
    IN_PROGRESS = "IN_PROGRESS"
    IN_REVIEW = "IN_REVIEW"
    IN_VALIDATION = "IN_VALIDATION"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    #: A status the adapter could not map. **Never** coerced into the most
    #: convenient neighbour: a new status on the board means somebody changed the
    #: process, and the engine has to say so instead of guessing.
    UNKNOWN = "UNKNOWN"

    @property
    def available(self) -> bool:
        """Work the engine could pick up."""
        return self in (ExternalStatus.NOT_STARTED, ExternalStatus.IN_ANALYSIS)

    @property
    def in_progress(self) -> bool:
        """Somebody (human or not) is already on this."""
        return self in (ExternalStatus.IN_PROGRESS, ExternalStatus.IN_REVIEW,
                        ExternalStatus.IN_VALIDATION)

    @property
    def finished(self) -> bool:
        return self in (ExternalStatus.COMPLETED, ExternalStatus.CANCELLED)


#: Link types the engine understands. Translating the provider's name into one
#: of these is the adapter's job.
#:
#: `blocks` is the ONLY one that becomes a dependency edge. `parent` is
#: hierarchy -- a subtask does not wait for its parent to finish, it is part of
#: what the parent is; and `related` is context, not order. Treating the three
#: as equal locks up an entire board, because hierarchy and relatedness are far
#: more common than real blocking.
BLOCKS = "blocks"
PARENT = "parent"
CHILD = "child"
RELATED = "related"
DUPLICATES = "duplicates"

#: The ones that create execution order. Any other is information, not a constraint.
BLOCKING_TYPES: frozenset[str] = frozenset({BLOCKS})


@dataclass(frozen=True, slots=True)
class TaskRef:
    """A link the provider declares between two units of work.

    `kind` is the engine's vocabulary -- never the provider's name for the link.
    """
    key: str
    kind: str = RELATED

    @property
    def blocking(self) -> bool:
        return self.kind in BLOCKING_TYPES


@dataclass(frozen=True, slots=True)
class ExternalTask:
    """A task as the provider describes it. UNTRUSTED data.

    Title, description and comments are text written by third parties. No engine
    prompt may treat them as instructions: an attempt at manipulation becomes a
    finding, never an order.
    """
    key: str
    title: str
    status: ExternalStatus = ExternalStatus.UNKNOWN
    #: The raw status, as the provider wrote it. Preserved for diagnosis: when
    #: `status` comes back UNKNOWN, this field is what says what showed up.
    external_status: str = ""
    description: str = ""
    url: str | None = None
    priority: int = 100
    project: str = ""
    assignee: str | None = None
    #: Dependencies and hierarchy as declared at the provider.
    links: tuple[TaskRef, ...] = ()
    #: Resource keys the task touches exclusively. Empty is common and honest: a
    #: task provider rarely knows which files will be touched. What enriches this
    #: is the analysis, not the adapter.
    resources: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    #: True when the record came from a LISTING, with trimmed fields.
    #:
    #: Listing and fetching detail are operations of very different cost: a real
    #: board returns hundreds of KB if every item carries a full description.
    #: Without this field, "empty description" and "description not asked for"
    #: become indistinguishable -- and the engine would accuse the whole board of
    #: being badly written when what was incomplete was the request itself.
    partial: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def anomalies(self) -> tuple[str, ...]:
        """What arrived crooked from the provider. Reported, never silently fixed."""
        findings = []
        if self.status is ExternalStatus.UNKNOWN:
            findings.append(f"unmapped status: {self.external_status!r}")
        if not self.title.strip():
            findings.append("no title")
        if not self.partial and not self.description.strip():
            findings.append("no description")
        return tuple(findings)


@dataclass(frozen=True, slots=True)
class Comment:
    author: str
    text: str
    created_at: str = ""
    id: str = ""


class TaskProvider(Port):
    capability = Capability.TASKS

    @abstractmethod
    def list_tasks(self, filters: dict[str, Any] | None = None) -> list[ExternalTask]:
        """Work visible now. An error rises as AdapterError, never an empty list."""

    @abstractmethod
    def get_task(self, key: str) -> ExternalTask: ...

    def get_comments(self, key: str) -> list[Comment]:
        return []

    # ---- writing ---------------------------------------------------------
    # Separated on purpose: a read-only adapter can exist without implementing
    # any of them, and the Policy Engine is still what authorises the call.

    def update_task(self, key: str, fields: dict[str, Any]) -> None:
        raise NotImplementedError

    def transition_task(self, key: str, destination: str) -> None:
        raise NotImplementedError

    def add_comment(self, key: str, text: str) -> None:
        raise NotImplementedError

    def add_label(self, key: str, label: str) -> None:
        raise NotImplementedError
