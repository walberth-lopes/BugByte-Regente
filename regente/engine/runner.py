# -*- coding: utf-8 -*-
"""Running one mission end to end, in isolation.

    select -> resolve target -> isolate -> baseline -> agent loop -> verdict -> persist

This module owns the two invariants that this milestone adds, and it owns them
*here* rather than in the loop or the agent because they must hold before any
work starts -- an invariant checked after the fact is an audit, not a guard:

**No agent may start work on a task without a resolved execution target backed by
sufficient evidence.** Writing code into a repository chosen by guess is the one
failure that produces no error at all: everything succeeds, in the wrong place.

**No agent may take a task the origin says a person or another worker is already
handling.** An agent on top of a person is the worst outcome this milestone could
ship, and it is silent from both sides.

Both are enforced by `MissionPlanner` during selection AND re-checked here before
dispatch, because selection and dispatch are separated in time, and the world
moves in between.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ..core import ids
from ..core.model import Event, Run, RunState, now
from ..ports.agent import Budget, CodingAgent, Permissions, Verdict
from ..ports.repository import RepositoryProvider
from ..ports.store import Store
from ..ports.workspace import WorkspaceProvider
from . import coder, context, metrics, mission, testing
from .discovery import Source
from .target import Confidence


class InvariantViolated(RuntimeError):
    """A guard that must never fail, failed. Never caught to keep going."""


@dataclass(frozen=True, slots=True)
class MissionOutcome:
    briefing: mission.Briefing | None
    verdict: Verdict | None
    loop: coder.LoopResult | None
    measurements: metrics.MissionMetrics | None
    workspace_path: str = ""
    refusal: str = ""

    @property
    def refused(self) -> bool:
        """No mission was selected at all.

        Deliberately not `verdict is None`: a dry selection produces a briefing
        and no verdict, and reading that as a refusal would report "nothing is
        safe to do" every time someone merely asked what the engine would do.
        """
        return self.briefing is None


@dataclass(slots=True)
class MissionRunner:
    store: Store
    workspace_id: str
    workspace_name: str
    repos: RepositoryProvider
    areas: WorkspaceProvider
    agent: CodingAgent
    planner: mission.MissionPlanner
    budget: Budget
    permissions: Permissions
    agent_name: str = "coder"
    test_timeout: int = 900
    lease_seconds: int = 1800

    # ------------------------------------------------------------------
    def run(self, selection: mission.Selection) -> MissionOutcome:
        if selection.mission is None:
            return MissionOutcome(None, None, None, None,
                                  refusal=selection.render())

        m = selection.mission
        started_at = datetime.now(timezone.utc)
        clock = time.monotonic()

        # --- invariant 1: a target, with evidence ---------------------
        self._require_target(m)
        # --- invariant 2: nobody else is inside -----------------------
        self._require_no_concurrent_work(m)

        resolution_s = time.monotonic() - clock

        run = Run(id=ids.new_id(ids.RUN), task_id=m.task.key,
                  workspace_id=self.workspace_id, agent=self.agent_name,
                  state=RunState.RUNNING)

        # The lease is taken BEFORE the clone: if another worker holds the
        # repository, the expensive part must never happen.
        lease = self.store.acquire_lease(m.resource, run.id, self.workspace_id,
                                         self.lease_seconds)
        if lease is None:
            return MissionOutcome(None, None, None, None,
                                  refusal=f"REFUSED: {m.resource} is held by another worker")

        try:
            area = self.areas.prepare(m.task.key, repo=m.repo.ref.key,
                                      branch=m.branch, base=m.repo.base_branch)
            run.workspace_path, run.branch = area.path, area.branch
            self.store.save_run(run)

            test_command = testing.detect_command(area.path)
            baseline_started = time.monotonic()
            baseline = coder.measure_baseline(test_command, area.path, self.test_timeout)
            del baseline_started

            ctx = context.build(m.task, m.repo, m.discovery, area.path,
                                constraints=mission.FORBIDDEN_MUTATIONS)
            briefing = mission.briefing_for(
                m, workspace_path=area.path, agent=self.agent.name,
                budget=self.budget, permissions=self.permissions,
                baseline_command=tuple(test_command) if test_command else None,
                timeout_seconds=self.budget.max_seconds)

            self._record("mission_started", m.task.key, run.id, briefing.render()[:400],
                         target=m.repo.ref.key,
                         confidence=m.discovery.confidence.value,
                         branch=area.branch, workspace=area.path)

            loop = coder.ValidationLoop(
                agent=self.agent, budget=self.budget, permissions=self.permissions,
                test_command=test_command, test_timeout=self.test_timeout,
                agent_name=self.agent_name)
            result = loop.execute(run_id=run.id, task_key=m.task.key, path=area.path,
                                  branch=area.branch, context=ctx, repo=m.repo.ref,
                                  baseline=baseline)

            run.state = (RunState.SUCCEEDED if not result.stopped_early
                         else RunState.FAILED)
            run.ended_at = now()
            run.reason = result.reason
            run.cost_usd, run.tokens = result.total_cost_usd, result.total_tokens
            run.tool_calls, run.iterations = result.total_tool_calls, len(result.attempts)
            self.store.save_run(run)

            self._persist_target(m, result)

            measured = metrics.build(
                task_key=m.task.key, repository=m.repo.ref.key,
                verdict=result.verdict, discovery=m.discovery, loop_result=result,
                resolution_s=resolution_s, total_s=time.monotonic() - clock,
                agent=self.agent.name, started_at=started_at,
                escalations=1 if result.verdict is Verdict.NEEDS_HUMAN else 0)

            # The metrics go in under one key rather than splatted: they carry
            # their own `task_key`, and splatting would collide with the
            # event's -- silently, and only for missions that reached the end.
            self._record("mission_finished", m.task.key, run.id,
                         f"{result.verdict.value}: {result.reason}"[:300],
                         metrics=measured.as_dict())
            return MissionOutcome(briefing, result.verdict, result, measured, area.path)
        finally:
            self.store.release_lease(m.resource, run.id, self.workspace_id)

    # ------------------------------------------------------------------
    def _require_target(self, m: mission.Mission) -> None:
        """INVARIANT: no work without a target backed by evidence."""
        d = m.discovery
        if not d.repo or d.repo != m.repo.ref.key:
            raise InvariantViolated(
                f"{m.task.key}: mission repository '{m.repo.ref.key}' does not match "
                f"the resolved target '{d.repo}'")
        if d.confidence not in (Confidence.DECLARED, Confidence.OBSERVED):
            raise InvariantViolated(
                f"{m.task.key}: target confidence is {d.confidence.value}; work may "
                f"only start on DECLARED or OBSERVED")
        if not d.evidence:
            raise InvariantViolated(
                f"{m.task.key}: the target carries no evidence. A repository chosen "
                f"without evidence fails silently -- everything succeeds, elsewhere")

    def _require_no_concurrent_work(self, m: mission.Mission) -> None:
        """INVARIANT: never take work the origin says someone else is doing."""
        if not m.task.status.available:
            raise InvariantViolated(
                f"{m.task.key}: the origin reports "
                f"'{m.task.external_status or m.task.status.value}' -- an agent on top "
                f"of a person is the worst outcome available")

    def _persist_target(self, m: mission.Mission, result: coder.LoopResult) -> None:
        """Write the discovery down, and promote only on real confirmation.

        A discovery becomes VALIDATED when an execution actually produced work in
        that repository -- not when the engine merely believed it would. Believing
        is what produced the discovery in the first place; confirming has to cost
        something more.
        """
        d = m.discovery
        self.store.record_target(
            workspace_id=self.workspace_id, task_key=m.task.key,
            provider=m.repo.ref.provider, repo_key=m.repo.ref.key,
            source=d.source.value, confidence=d.confidence.value,
            strength=d.strength,
            evidence=[f"{e.source}: {e.detail}" for e in d.evidence],
            alternatives=[f"{r.repo}: {r.reason}" for r in d.alternatives_considered])

        confirmed = bool(result.changed_files) and result.verdict in (
            Verdict.RESOLVED, Verdict.READY_FOR_REVIEW)
        if confirmed and d.source is Source.DISCOVERED:
            self.store.promote_target(self.workspace_id, m.task.key,
                                      m.repo.ref.provider, m.repo.ref.key)

    def _record(self, kind: str, task_key: str, run_id: str, summary: str,
                **data) -> None:
        self.store.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id=self.workspace_id, kind=kind,
            task_id=task_key, run_id=run_id, summary=summary, data=data))
