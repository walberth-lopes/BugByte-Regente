# -*- coding: utf-8 -*-
"""The domain. No type here knows what Jira, GitHub, GCloud or SQLite are.

Two structural choices that hold up the rest of the engine:

**The multi-tenancy spine is in every object that persists.** Organization ->
Client -> Workspace -> Project -> Repository is not a decorative hierarchy: every
Task, Run and Event carries a `workspace_id`. Bolting tenancy on later means
migrating every table and reviewing every query -- and the forgotten query is
precisely the one that leaks client A's data to client B.

**`ExternalRef` separates the engine's id from the provider's id.** The task
exists in the engine even if the provider changes tool; and the same task can be
seen by two different providers (the Jira issue, the GitHub PR) without
duplication.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone
from typing import Any

from .policy import AutonomyLevel
from .risk import RiskLevel
from .states import TaskState


def now() -> datetime:
    """UTC, always. Local time only ever appears at the presentation surface."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Tenancy spine
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Organization:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Client:
    id: str
    organization_id: str
    name: str


@dataclass(frozen=True, slots=True)
class Workspace:
    """The unit of configuration: a set of adapters, policies and limits.

    It is the workspace -- not the project -- that carries credentials and
    autonomy, because the workspace is where the boundary between clients has to
    be inviolable.
    """
    id: str
    client_id: str
    name: str
    max_autonomy: AutonomyLevel = AutonomyLevel.L2
    root: str | None = None


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    workspace_id: str
    name: str
    default_environment: str = "staging"
    max_autonomy: AutonomyLevel | None = None  # None = inherits from the workspace


@dataclass(frozen=True, slots=True)
class Repository:
    id: str
    project_id: str
    name: str
    base_branch: str = "main"
    url: str | None = None


# --------------------------------------------------------------------------
# Work
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ExternalRef:
    """Where the task came from, in the vocabulary of whoever issued it."""
    provider: str          # adapter name, e.g. "filesystem", "jira"
    key: str               # key at the provider, e.g. "FAXINA-183"
    url: str | None = None


@dataclass(slots=True)
class Task:
    id: str
    workspace_id: str
    project_id: str
    title: str
    state: TaskState = TaskState.DISCOVERED
    external: ExternalRef | None = None
    description: str = ""
    priority: int = 100                       # lower runs first
    risk: RiskLevel | None = None
    #: The state the task was in when it paused for a human decision.
    paused_at: TaskState | None = None
    #: Resource keys this task touches exclusively. The scheduler uses this to
    #: NOT parallelise two workers over the same migration or the same file.
    #: E.g. "repo:acme/api", "migration:acme/api", "file:src/auth.py".
    resources: tuple[str, ...] = ()
    attempts: int = 0
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """How the task appears to a human."""
        return self.external.key if self.external else self.id


@dataclass(frozen=True, slots=True)
class Dependency:
    task_id: str
    depends_on: str
    kind: str = "blocks"     # blocks | subtask | conflict
    reason: str = ""


class RunState(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"   # the worker died; the lease expired
    ABORTED = "ABORTED"           # the engine stopped on purpose (loop, budget)


@dataclass(slots=True)
class Run:
    """One attempt at executing a task by an agent.

    A Task has long-term state; a Run has attempt state. Separating the two is
    what allows retrying without losing the history -- and what makes "the
    worker died" different from "the task failed".
    """
    id: str
    task_id: str
    workspace_id: str
    agent: str
    state: RunState = RunState.RUNNING
    worker: str | None = None
    workspace_path: str | None = None
    branch: str | None = None
    started_at: datetime = field(default_factory=now)
    ended_at: datetime | None = None
    reason: str = ""
    cost_usd: float = 0.0
    tokens: int = 0
    tool_calls: int = 0
    iterations: int = 0
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Event:
    """Append-only record. Source of truth for the timeline and the audit trail.

    The engine never deletes or edits an event. State is a convenient
    projection; the event is what happened.
    """
    id: str
    workspace_id: str
    kind: str
    ts: datetime = field(default_factory=now)
    task_id: str | None = None
    run_id: str | None = None
    actor: str = "engine"
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class ApprovalState(str, Enum):
    OPEN = "OPEN"
    DECIDED = "DECIDED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class Option:
    id: str
    label: str
    effect: str = ""


@dataclass(slots=True)
class Approval:
    """An item in the NEEDS ME queue.

    The fields are those of the required briefing: what happened, why it
    matters, what the agent tried, options, recommendation, risk. A giant log
    does not belong here -- it stays in the events, on demand.
    """
    id: str
    workspace_id: str
    task_id: str
    what_happened: str
    why_it_matters: str
    what_was_tried: tuple[str, ...] = ()
    options: tuple[Option, ...] = ()
    recommendation: str | None = None      # Option.id
    risk: RiskLevel = RiskLevel.MEDIUM
    state: ApprovalState = ApprovalState.OPEN
    run_id: str | None = None
    created_at: datetime = field(default_factory=now)
    decided_at: datetime | None = None
    decided_by: str | None = None
    choice: str | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """Every tool request, with the policy verdict. Nothing is left out.

    What was DENIED is recorded too: a refusal is the most valuable record the
    engine produces, because it is what proves the gate is alive.
    """
    id: str
    workspace_id: str
    agent: str
    action: str
    resource: str
    effect: str                # ALLOW | DENY | HUMAN_APPROVAL
    risk: str
    ts: datetime = field(default_factory=now)
    task_id: str | None = None
    run_id: str | None = None
    rule: str | None = None
    reason: str = ""
    result: str = ""
    duration_ms: int = 0
    cost_usd: float = 0.0
    tokens: int = 0


@dataclass(frozen=True, slots=True)
class Lease:
    """Cooperative lock with a heartbeat.

    There is no lock without expiry in this engine: the worker that dies without
    releasing the lock is the normal case, not the exceptional one. An expired
    lease is the signal recovery uses to return the task to the queue.
    """
    resource: str
    owner: str
    expires_at: datetime
    workspace_id: str
    renewed_at: datetime = field(default_factory=now)
