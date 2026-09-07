# -*- coding: utf-8 -*-
"""Store: the state that outlives the process.

The engine never relies on an agent's conversation context to know where the
work stopped. Everything that matters is here, and the consequence is direct:
killing the process in the middle of a dispatch is a supported operation, not an
accident.

`transition()` and `acquire_lease()` are the two points that have to be atomic.
Without atomicity in the transition, two concurrent ticks dispatch the same task;
without atomicity in the lease, two workers write to the same repository.
"""

from __future__ import annotations

from abc import abstractmethod
from datetime import datetime

from ..core.model import (ActionRecord, Approval, Dependency, Event, Lease, Project,
                          Repository, Run, Task, Workspace)
from ..core.states import TaskState
from . import Capability, Port


class Store(Port):
    capability = Capability.STORE

    # ---- schema and tenancy ---------------------------------------------
    @abstractmethod
    def migrate(self) -> None: ...

    @abstractmethod
    def save_workspace(self, w: Workspace) -> None: ...

    @abstractmethod
    def workspace(self, workspace_id: str) -> Workspace | None: ...

    @abstractmethod
    def workspaces(self) -> list[Workspace]: ...

    @abstractmethod
    def save_project(self, p: Project) -> None: ...

    @abstractmethod
    def projects(self, workspace_id: str) -> list[Project]: ...

    @abstractmethod
    def save_repository(self, r: Repository) -> None: ...

    # ---- work ------------------------------------------------------------
    @abstractmethod
    def save_task(self, t: Task) -> None: ...

    @abstractmethod
    def task(self, task_id: str) -> Task | None: ...

    @abstractmethod
    def task_by_key(self, workspace_id: str, provider: str, key: str) -> Task | None: ...

    @abstractmethod
    def tasks(self, workspace_id: str, states: list[TaskState] | None = None) -> list[Task]: ...

    @abstractmethod
    def transition(self, task_id: str, destination: TaskState, actor: str,
                    reason: str = "", data: dict | None = None) -> Task:
        """Validates the transition, writes and emits an event -- all in one transaction."""

    @abstractmethod
    def link_dependency(self, d: Dependency) -> None: ...

    @abstractmethod
    def dependencies(self, workspace_id: str) -> list[Dependency]: ...

    # ---- execution -------------------------------------------------------
    @abstractmethod
    def save_run(self, r: Run) -> None: ...

    @abstractmethod
    def run(self, run_id: str) -> Run | None: ...

    @abstractmethod
    def active_runs(self, workspace_id: str) -> list[Run]: ...

    @abstractmethod
    def task_runs(self, task_id: str) -> list[Run]: ...

    # ---- trail -----------------------------------------------------------
    @abstractmethod
    def record_event(self, e: Event) -> None: ...

    @abstractmethod
    def events(self, workspace_id: str, task_id: str | None = None,
                limit: int = 100) -> list[Event]: ...

    @abstractmethod
    def record_action(self, a: ActionRecord) -> None: ...

    @abstractmethod
    def actions(self, workspace_id: str, limit: int = 100) -> list[ActionRecord]: ...

    # ---- escalation ------------------------------------------------------
    @abstractmethod
    def open_approval(self, a: Approval) -> None: ...

    @abstractmethod
    def open_approvals(self, workspace_id: str) -> list[Approval]: ...

    @abstractmethod
    def approval(self, approval_id: str) -> Approval | None: ...

    @abstractmethod
    def decide_approval(self, approval_id: str, choice: str, by: str,
                        note: str = "") -> Approval: ...

    # ---- locks -----------------------------------------------------------
    @abstractmethod
    def acquire_lease(self, resource: str, owner: str, workspace_id: str,
                      seconds: int) -> Lease | None:
        """Returns None when a live lease belongs to another owner. Never waits.

        The lock is per (workspace, resource). A resource of the same name in two
        clients is two resources -- one client never holds up the other's queue.
        """

    @abstractmethod
    def renew_lease(self, resource: str, owner: str, seconds: int,
                     workspace_id: str | None = None) -> bool: ...

    @abstractmethod
    def release_lease(self, resource: str, owner: str,
                    workspace_id: str | None = None) -> None: ...

    @abstractmethod
    def expired_leases(self, workspace_id: str, now: datetime | None = None) -> list[Lease]: ...

    # ---- entregas --------------------------------------------------------
    @abstractmethod
    def open_delivery(self, workspace_id: str, task_key: str, run_id: str,
                      provider: str, repo_key: str, branch: str,
                      commit_sha: str) -> str:
        """Abre o registro ANTES de qualquer mutacao remota e devolve o id."""

    @abstractmethod
    def record_push(self, delivery_id: str, target: str) -> None: ...

    @abstractmethod
    def record_pull_request(self, delivery_id: str, number: int, url: str,
                            head_sha: str) -> None: ...

    @abstractmethod
    def record_ci(self, delivery_id: str, state: str, result: str | None,
                  reason: str, checks: list[dict]) -> None: ...

    @abstractmethod
    def deliveries(self, workspace_id: str,
                   task_key: str | None = None) -> list[dict]: ...

    @abstractmethod
    def delivery_for_pr(self, workspace_id: str, provider: str, repo_key: str,
                        number: int) -> dict | None: ...

    # ---- counters --------------------------------------------------------
    @abstractmethod
    def dispatch_count(self, workspace_id: str, day: str) -> int: ...

    @abstractmethod
    def mark_dispatch(self, workspace_id: str, day: str) -> None: ...
