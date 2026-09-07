# -*- coding: utf-8 -*-
"""Orchestrator: the operational brain. One tick from end to end.

    recover -> discover -> analyse -> plan -> dispatch -> collect

Each phase is independent and idempotent. That is not elegance: it is what makes
the engine survive interruption. Killing the process between two phases leaves
the state consistent, and the next tick carries on from where it stopped -- the
recurring tick is itself the retry mechanism, with no retry code inside the flow.

**Baseline on the first pass.** The first discovery of a workspace records the
backlog and dispatches nothing. Without that, switching the engine on against a
board with dozens of open tasks sets off a storm of workers -- and the owner's
first contact with the product becomes an incident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..core import ids
from ..core.graph import DependencyGraph
from ..core.model import (Dependency, Event, ExternalRef, Run, RunState, Task, Workspace,
                          now)
from ..core.policy import AutonomyLevel
from ..core.risk import RiskEngine, RiskLevel
from ..core.scheduling import Candidate, Limits, Plan, plan
from ..core.states import TaskState
from ..ports import AdapterError
from ..ports.support import NotificationProvider
from ..ports.tasks import ExternalTask, ExternalStatus, TaskProvider
from ..ports.workspace import AgentRunner, RunRequest, WorkspaceProvider
from ..ports.store import Store
from . import escalation, supervisor
from .gate import Scope, Gate


def _sanitize(key: str) -> str:
    """Task key -> safe directory name.

    A provider key accepts things a path does not (slash, colon, space). Without
    sanitising, a task's area vanishes somewhere in an unexpected tree -- or,
    worse, escapes the root.
    """
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in key)
    return cleaned.strip("-.") or "no-key"


@dataclass(slots=True)
class TickReport:
    """What the tick did. Deliberately short: this is what the owner reads."""
    workspace: str
    discovered: int = 0
    analyzed: int = 0
    dispatched: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    recovered: tuple[str, ...] = ()
    escalated: tuple[str, ...] = ()
    deferred: tuple[tuple[str, str], ...] = ()
    cycles: tuple[str, ...] = ()
    baseline: bool = False
    errors: tuple[str, ...] = ()
    #: What arrived crooked from the source. Reported, never silently fixed.
    anomalies: tuple[str, ...] = ()
    #: Tasks whose status at the source changed since the last pass.
    changes: tuple[tuple[str, str, str], ...] = ()
    #: Were blocked by the source and have come back to the queue.
    unblocked_tasks: tuple[str, ...] = ()

    def summary(self) -> str:
        if self.baseline:
            return f"{self.workspace}: baseline with {self.discovered} tasks; nothing dispatched"
        parts = []
        if self.recovered:
            parts.append(f"{len(self.recovered)} recovered")
        if self.discovered:
            parts.append(f"{self.discovered} new")
        if self.dispatched:
            parts.append(f"{len(self.dispatched)} dispatched")
        if self.completed:
            parts.append(f"{len(self.completed)} completed")
        if self.changes:
            parts.append(f"{len(self.changes)} changed at the source")
        if self.unblocked_tasks:
            parts.append(f"{len(self.unblocked_tasks)} released")
        if self.escalated:
            parts.append(f"{len(self.escalated)} need you")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return f"{self.workspace}: " + (", ".join(parts) if parts else "nothing to do")


@dataclass(slots=True)
class Orchestrator:
    store: Store
    workspace: Workspace
    tasks_provider: TaskProvider
    area_provider: WorkspaceProvider
    runner: AgentRunner
    gate: Gate
    risk: RiskEngine
    limits: Limits = field(default_factory=Limits)
    budget: supervisor.Budget = field(default_factory=supervisor.Budget)
    notificador: NotificationProvider | None = None
    project_id: str = "prj_default"
    #: Lifetime of a lease in seconds. The worker renews it; if it dies, the
    #: lease expires and recovery returns the task to the queue.
    lease_seconds: int = 900

    # ------------------------------------------------------------------
    def tick(self) -> TickReport:
        rel = TickReport(workspace=self.workspace.name)
        self._record("tick_start", summary="tick started")
        try:
            self._recover(rel)
            first_pass = self._discover(rel)
            if first_pass:
                rel.baseline = True
                self._record("baseline", summary=f"{rel.discovered} tasks recorded without dispatch")
                return rel
            self._analyze(rel)
            self._dispatch(rel)
        except AdapterError as e:
            # An adapter failure never becomes "there was no work". The tick ends
            # with a declared error and the next one tries again.
            rel.errors += (f"adapter: {e}",)
            self._record("error", summary=str(e)[:300])
        self._record("tick_end", summary=rel.summary())
        return rel

    # ---- 1. recovery ----------------------------------------------------
    def _recover(self, rel: TickReport) -> None:
        """Returns to the queue the work of workers that died.

        The proof that a worker died is the expired lease, not the absence of a
        process: the engine may be running on another machine, and 'I cannot see
        the process' is a test that only works by accident.
        """
        expired = {l.owner: l for l in self.store.expired_leases(self.workspace.id)}
        for run in self.store.active_runs(self.workspace.id):
            if run.id not in expired:
                continue
            task = self.store.task(run.task_id)
            run.state = RunState.INTERRUPTED
            run.ended_at = now()
            run.reason = "lease expired: the worker did not renew"
            self.store.save_run(run)
            self.store.release_lease(run.id, run.id, self.workspace.id)
            for resource in (task.resources if task else ()):
                self.store.release_lease(resource, run.id, self.workspace.id)

            if task is None:
                continue
            destination = supervisor.resume_state(task.state)
            if destination is not task.state:
                self.store.transition(task.id, destination, actor="supervisor",
                                       reason="worker interrupted")
            rel.recovered += (task.key,)
            self._record("recovered", task_id=task.id, run_id=run.id,
                        summary=f"worker dead; task comes back as {destination.value}")

    # ---- 2. discovery ---------------------------------------------------
    def _discover(self, rel: TickReport) -> bool:
        """Returns True when this was the first pass (baseline)."""
        had_any = bool(self.store.tasks(self.workspace.id))
        external_items = self.tasks_provider.list_tasks()

        keys: dict[str, str] = {}   # external key -> task_id
        for e in external_items:
            if e.status.finished:
                # Work finished at the source does not become work here.
                continue
            task = self.store.task_by_key(self.workspace.id, self.tasks_provider.name, e.key)
            if task is None:
                task = self._create_task(e)
                rel.discovered += 1
                self._record("discovered", task_id=task.id,
                            summary=f"{e.key}: {e.title}"[:200],
                            status=e.status.value, anomalies=list(e.anomalies))
            else:
                self._refresh(task, e, rel)
            keys[e.key] = task.id
            if e.anomalies:
                rel.anomalies += tuple(f"{e.key}: {a}" for a in e.anomalies)

        # Links can only be wired once every task exists: the blocker may show
        # up after the blocked one in the same list.
        #
        # **Only a BLOCKING link becomes an edge.** Hierarchy and relatedness are
        # information, not execution order: a subtask does not wait for its parent
        # to finish, it is part of what the parent is. Treating the three as equal
        # locks the whole board -- and on a real board hierarchy and relatedness
        # are far more common than actual blocking.
        for e in external_items:
            for v in e.links:
                if not v.blocking:
                    continue
                target = keys.get(v.key)
                if target and target != keys[e.key]:
                    self.store.link_dependency(Dependency(
                        task_id=keys[e.key], depends_on=target, kind=v.kind,
                        reason=f"declared by {self.tasks_provider.name}"))
        return not had_any

    def _refresh(self, task: Task, e: ExternalTask, rel: TickReport) -> None:
        """Re-reads what changed at the source. The engine does NOT inherit its state.

        The source rules over what is its own -- title, priority, who is on the
        task. The engine's state belongs to the engine: if somebody moved the
        issue on the board, that changes the *relevance* of the work, not the
        step at which the worker stopped.

        The `data` keys below (normalised_status, raw_status, labels) are
        persisted JSON; old databases are migrated by `_v4_to_v5` in the store.
        """
        before = task.data.get("normalised_status")
        current_status = e.status.value
        task.title = e.title
        task.priority = e.priority
        task.data.update({"normalised_status": current_status,
                           "raw_status": e.external_status,
                           "labels": list(e.labels)})
        task.updated_at = now()
        self.store.save_task(task)
        if before and before != current_status:
            rel.changes += ((task.key, before, current_status),)
            self._record("changed_at_source", task_id=task.id,
                        summary=f"{before} -> {current_status} ({e.external_status})",
                        de=before, to_state=current_status)

    def _create_task(self, e: ExternalTask) -> Task:
        t = Task(
            id=ids.new_id(ids.TASK), workspace_id=self.workspace.id,
            project_id=e.project or self.project_id, title=e.title,
            state=TaskState.DISCOVERED,
            external=ExternalRef(provider=self.tasks_provider.name, key=e.key, url=e.url),
            description=e.description, priority=e.priority,
            resources=tuple(e.resources),
            data={**dict(e.data), "normalised_status": e.status.value,
                   "raw_status": e.external_status,
                   "labels": list(e.labels)})
        self.store.save_task(t)
        return t

    # ---- 3. analysis ----------------------------------------------------
    def _analyze(self, rel: TickReport) -> None:
        """DISCOVERED -> ANALYZING -> READY, computing risk and resources.

        The analysis in this milestone is deterministic: risk from declared
        signals and resources by convention. A PlannerAgent slots in here later,
        at the same point -- it enriches `resources` and `dependencies`, and the
        rest of the engine does not change.
        """
        # Work the source says is blocked by third parties can come back to the
        # queue when the source changes its mind. This is the only way back: a
        # task blocked by FAILURE is not unblocked by an external status.
        for t in self.store.tasks(self.workspace.id, [TaskState.BLOCKED]):
            if t.data.get("blocked_by") != "source":
                continue
            if self._status_of(t).available:
                t.data.pop("blocked_by", None)
                self.store.save_task(t)
                self.store.transition(t.id, TaskState.READY, actor="planner",
                                       reason="the source released the work")
                rel.unblocked_tasks += (t.key,)

        for t in self.store.tasks(self.workspace.id, [TaskState.DISCOVERED]):
            self.store.transition(t.id, TaskState.ANALYZING, actor="planner",
                                   reason="initial analysis")
            assessment = self.risk.assess({
                "action": "task.analyze",
                "environment": "local",
                "paths": list(t.resources) + [t.title],
                "category": "task",
            })
            t = self.store.task(t.id)
            t.risk = assessment.level
            if not t.resources:
                # With no better information, the task holds the whole project.
                # Erring on the side of not parallelising is cheap; erring the
                # other way produces two workers in the same file.
                t.resources = (f"project:{t.project_id}",)
            # **The source decides whether the work is available.**
            #
            # Without this guard the engine dispatches a worker onto a task that
            # already has people on it -- measured against the real board: two
            # issues in CODING were dispatched on the first tick. An agent on top
            # of a person is the worst defect this milestone could have let
            # through, and no test with invented data would have found it.
            status = self._status_of(t)
            if not status.available:
                t.data["blocked_by"] = "source"
                self.store.save_task(t)
                self.store.transition(
                    t.id, TaskState.BLOCKED, actor="planner",
                    reason=f"the source says {t.data.get('raw_status') or status.value}")
                rel.analyzed += 1
                continue

            self.store.save_task(t)
            self.store.transition(t.id, TaskState.READY, actor="planner",
                                   reason=f"risk {assessment.level.name}",
                                   data={"sinais": list(assessment.reasons)})
            rel.analyzed += 1

    # ---- 4/5. plan and dispatch -----------------------------------------
    def _graph(self) -> DependencyGraph:
        g = DependencyGraph()
        for t in self.store.tasks(self.workspace.id):
            g.add(t.id)
        for d in self.store.dependencies(self.workspace.id):
            g.link(d.task_id, d.depends_on)
        return g

    def plan(self) -> Plan:
        """Exposed so the UI and the tests can see the decision without running it."""
        all_tasks = self.store.tasks(self.workspace.id)
        completed = {t.id for t in all_tasks if t.state is TaskState.DONE}
        active = self.store.active_runs(self.workspace.id)
        by_id = {t.id: t for t in all_tasks}
        running_now = {
            r.task_id: frozenset(by_id[r.task_id].resources)
            for r in active if r.task_id in by_id
        }
        candidates = [
            Candidate(task_id=t.id, priority=t.priority,
                      resources=frozenset(t.resources), key=t.key)
            for t in all_tasks if t.state is TaskState.READY
        ]
        today = now().strftime("%Y-%m-%d")
        return plan(candidates, self._graph(), completed, running_now,
                       self.limits, self.store.dispatch_count(self.workspace.id, today),
                       names={t.id: t.key for t in all_tasks})

    def _dispatch(self, rel: TickReport) -> None:
        p = self.plan()
        rel.deferred = tuple((self.store.task(a.task_id).key, a.reason) for a in p.deferred)
        rel.cycles = tuple(self.store.task(i).key for i in p.in_cycle)

        if p.in_cycle:
            self._escalate_cycle(p, rel)

        for task_id in p.dispatch:
            try:
                self._run_one(task_id, rel)
            except Exception as e:   # noqa: BLE001 - one worker does not bring the tick down
                rel.errors += (f"{task_id}: {type(e).__name__}: {e}"[:200],)
                self._record("error", task_id=task_id, summary=str(e)[:300])

    def _run_one(self, task_id: str, rel: TickReport) -> None:
        task = self.store.task(task_id)
        run = Run(id=ids.new_id(ids.RUN), task_id=task.id, workspace_id=self.workspace.id,
                  agent="coder", state=RunState.RUNNING)

        # Lock BEFORE transitioning: if the lock fails, the task must not have
        # left READY -- otherwise it sits in ASSIGNED with no owner.
        held: list[str] = []
        for resource in task.resources:
            if self.store.acquire_lease(resource, run.id, self.workspace.id,
                                        self.lease_seconds) is None:
                for r in held:
                    self.store.release_lease(r, run.id, self.workspace.id)
                self._record("deferred", task_id=task.id,
                            summary=f"resource {resource} became busy between the plan and the dispatch")
                return
            held.append(resource)

        self.store.transition(task.id, TaskState.ASSIGNED, actor="orchestrator",
                               reason=f"run {run.id}")
        area = self.area_provider.prepare(_sanitize(task.key),
                                          branch=f"regente/{task.key.lower()}")
        run.workspace_path, run.branch = area.path, area.branch
        self.store.save_run(run)
        self.store.mark_dispatch(self.workspace.id, now().strftime("%Y-%m-%d"))
        self.store.transition(task.id, TaskState.IMPLEMENTING, actor=run.agent,
                               reason="worker started")
        rel.dispatched += (task.key,)
        self._record("dispatched", task_id=task.id, run_id=run.id,
                    summary=f"{run.agent} in {area.path}")

        request = RunRequest(
            run_id=run.id, task_id=task.id, agent=run.agent,
            goal=task.title, area=area,
            context={"description": task.description, "key": task.key,
                      "risk": task.risk.name if task.risk else "LOW",
                      "resources": list(task.resources)},
            limit_iterations=self.budget.max_iterations,
            limit_tool_calls=self.budget.max_tool_calls,
            limit_cost_usd=self.budget.max_cost_usd,
            limit_seconds=self.budget.max_seconds)

        try:
            result = self.runner.run(request)
        except Exception as e:   # noqa: BLE001
            result = None
            run.reason = f"{type(e).__name__}: {e}"[:300]

        self._collect(task.id, run, result, held, rel)

    # ---- 6. collection --------------------------------------------------
    def _collect(self, task_id: str, run: Run, result, held: list[str],
               rel: TickReport) -> None:
        for r in held:
            self.store.release_lease(r, run.id, self.workspace.id)
        run.ended_at = now()

        if result is None:
            self._failed(task_id, run, run.reason or "the worker raised an exception", rel)
            return

        run.cost_usd, run.tokens = result.cost_usd, result.tokens
        run.tool_calls, run.iterations = result.tool_calls, result.iterations

        if result.outcome == "NEEDS_HUMAN":
            run.state, run.reason = RunState.ABORTED, result.summary
            self.store.save_run(run)
            self.store.transition(task_id, TaskState.WAITING_HUMAN, actor=run.agent,
                                   reason=result.summary)
            self._escalate(task_id, run, result, rel)
            return

        if result.ok:
            run.state, run.reason = RunState.SUCCEEDED, result.summary
            self.store.save_run(run)
            task = self.store.task(task_id)
            self.store.transition(task.id, TaskState.TESTING, actor=run.agent,
                                   reason=result.summary)
            rel.completed += (task.key,)
            self._record("implemented", task_id=task.id, run_id=run.id,
                        summary=result.summary[:200])
            return

        self._failed(task_id, run, result.summary, rel, outcome=result.outcome)

    def _failed(self, task_id: str, run: Run, reason: str, rel: TickReport,
                outcome: str = "ERROR") -> None:
        run.state, run.reason = RunState.FAILED, reason
        self.store.save_run(run)

        task = self.store.task(task_id)
        task.attempts += 1
        self.store.save_task(task)

        # The task passes through FAILED before any recovery. Skipping that rung
        # would save one line and erase from the timeline the fact that a failure
        # happened -- which is exactly what somebody looks for when the same task
        # comes back for the third time.
        self.store.transition(task.id, TaskState.FAILED, actor=run.agent, reason=reason)

        step_name = supervisor.next_recovery_step(task, self.budget)
        if supervisor.no_progress(task, self.store.task_runs(task.id)).stop:
            step_name = "escalar"

        if step_name == "escalar":
            self.store.transition(task.id, TaskState.WAITING_HUMAN, actor="supervisor",
                                   reason=reason)
            self._escalate_failure(task, run, reason, step_name, rel)
        else:
            self.store.transition(task.id, TaskState.READY, actor="supervisor",
                                   reason=f"{step_name} after failure: {reason}"[:300])
            self._record("failed", task_id=task.id, run_id=run.id,
                        summary=f"{reason[:160]} -> {step_name}")

    # ---- escalation ------------------------------------------------------
    def _escalate(self, task_id: str, run: Run, result, rel: TickReport) -> None:
        task = self.store.task(task_id)
        p = result.question or {}
        approval = escalation.build(
            task=task,
            what_happened=p.get("what_happened", result.summary),
            why_it_matters=p.get("why_it_matters", "the agent stopped without being able to decide on its own"),
            attempts=tuple(p.get("attempts", ())),
            recommendation=p.get("recommendation", escalation.FOLLOW.id),
            risk=task.risk or RiskLevel.MEDIUM,
            run_id=run.id)
        self._publish(approval, task, rel)

    def _escalate_failure(self, task: Task, run: Run, reason: str, step_name: str,
                      rel: TickReport) -> None:
        attempts = tuple(
            f"{r.agent}: {r.reason or r.state.value}"[:160]
            for r in self.store.task_runs(task.id)[-3:])
        approval = escalation.build(
            task=task,
            what_happened=f"{task.attempts} attempts failed. Last one: {reason}"[:400],
            why_it_matters="the recovery ladder ran out; without your decision the task does not move",
            attempts=attempts,
            recommendation=escalation.INVESTIGATE.id,
            risk=task.risk or RiskLevel.MEDIUM,
            run_id=run.id)
        self._publish(approval, task, rel)

    def _escalate_cycle(self, p: Plan, rel: TickReport) -> None:
        keys = [self.store.task(i).key for i in p.in_cycle]
        task = self.store.task(p.in_cycle[0])
        if any(a.task_id == task.id for a in self.store.open_approvals(self.workspace.id)):
            return   # already asked; do not repeat it every tick
        approval = escalation.build(
            task=task,
            what_happened=f"circular dependencies between {', '.join(keys)}",
            why_it_matters="none of these tasks can start while the cycle exists",
            attempts=("built the graph from the links declared at the source",),
            recommendation=escalation.INVESTIGATE.id,
            risk=RiskLevel.MEDIUM)
        self._publish(approval, task, rel)

    def _publish(self, approval, task: Task, rel: TickReport) -> None:
        self.store.open_approval(approval)
        rel.escalated += (task.key,)
        if self.notificador:
            b = escalation.briefing(approval, task)
            self.notificador.notify(f"{task.key} needs you", b.what_happened,
                                    urgency="high" if approval.risk >= RiskLevel.HIGH else "normal")

    # ---- utilities -------------------------------------------------------
    def _record(self, kind: str, summary: str = "", task_id: str | None = None,
               run_id: str | None = None, **data: Any) -> None:
        self.store.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id=self.workspace.id, kind=kind,
            task_id=task_id, run_id=run_id, summary=summary, data=data))

    def _status_of(self, t: Task) -> ExternalStatus:
        """The status at the source, rebuilt from what was persisted.

        A provider with no notion of status returns UNKNOWN -- which is NOT
        available. Deliberately conservative: not knowing whether somebody is on
        the task has to cost a deferral, never a collision.
        """
        raw = t.data.get("normalised_status")
        try:
            return ExternalStatus(raw)
        except ValueError:
            return ExternalStatus.UNKNOWN

    def scope(self, agent: str = "engine", task_id: str | None = None,
               run_id: str | None = None, project: str = "*") -> Scope:
        return Scope(workspace_id=self.workspace.id, workspace=self.workspace.name,
                      project=project, autonomy=self.workspace.max_autonomy,
                      agent=agent, task_id=task_id, run_id=run_id)
