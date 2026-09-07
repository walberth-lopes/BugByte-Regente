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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..core import ids
from ..core.model import Event, Run, RunState, now
from ..core.policy import (Action, AutonomyLevel, Effect, PolicyContext,
                           PolicyEngine)
from ..ports.agent import AgentRunner, Budget, Mission, Permissions, Verdict
from ..ports.repository import RepositoryProvider
from ..ports import AdapterError
from ..ports.store import Store
from ..ports.workspace import WorkspaceProvider
from . import (coder, context, metrics, mission, observation, pipeline,
               remote, testing, validation)
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
    #: SHA written by the ENGINE, never by the agent. Empty when no commit
    #: happened -- and the reason why is always stated.
    commit_sha: str = ""
    commit_reason: str = ""
    promoted: bool = False
    promotion_reason: str = ""
    #: How far the change got towards a reviewer. `None` when no delivery stage
    #: is configured at all -- which is a different thing from a delivery that
    #: was attempted and refused, and the two must not read alike.
    delivery: pipeline.DeliveryOutcome | None = None

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
    agent: AgentRunner
    planner: mission.MissionPlanner
    budget: Budget
    permissions: Permissions
    policy: PolicyEngine | None = None
    autonomy: AutonomyLevel = AutonomyLevel.L2
    organization: str = "*"
    client: str = "*"
    environment: str = "staging"
    agent_name: str = "coder"
    test_timeout: int = 900
    lease_seconds: int = 1800
    #: Identity written into the commit. Distinct from any human on purpose:
    #: history must say who actually wrote it.
    commit_author: tuple[str, str] = ("Regente", "regente@localhost.invalid")
    #: Directories the agent must never touch, watched for the whole run. The
    #: source clones belong here: a write into one of them would contaminate
    #: every future area cut from it, and it leaves no trace in the isolated
    #: area's own git status.
    watched_sources: tuple[str, ...] = ()
    #: Vendor-named authority paths, from composition.
    authority_paths: tuple[str, ...] = ()
    #: Vendor-named instruction filenames, from composition.
    instruction_files: tuple[str, ...] = ()
    #: The road out of `TESTING`. Optional on purpose: a workspace with no
    #: remote configured still runs missions, and the engine says so rather
    #: than failing at the end of a successful piece of work.
    delivery: pipeline.DeliveryStage | None = None
    #: Which origin the mission's task key belongs to. A key alone does not
    #: identify a task -- two providers can both call something `PROJ-1` -- so
    #: the lookup that finds the row to move is given both, never one.
    task_provider: str = ""

    def _watched_sources(self) -> tuple[str, ...]:
        return self.watched_sources

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

        run = self._new_run(m)

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

            ctx = context.build(
                m.task, m.repo, m.discovery, area.path,
                constraints=mission.FORBIDDEN_MUTATIONS,
                validation_commands=((tuple(test_command),) if test_command else ()),
                instruction_files=self.instruction_files)

            # Captured BEFORE the agent runs. A sentinel taken afterwards would
            # report "nothing moved" about a world nobody looked at, which is
            # the most reassuring possible way to miss an escape.
            sentinel = observation.sentinel_for(
                Mission(workspace_id=self.workspace_id,
                        workspace_name=self.workspace_name,
                        task_key=m.task.key, run_id=run.id,
                        allowed_root=area.path),
                area_root=Path(area.path).parent,
                sources=self._watched_sources(),
                authority_paths=self.authority_paths)
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
                agent_name=self.agent_name, sentinel=sentinel,
                forbidden_actions=mission.FORBIDDEN_MUTATIONS,
                authority_paths=self.authority_paths)
            result = loop.execute(
                run_id=run.id, task_key=m.task.key,
                workspace_id=self.workspace_id, workspace_name=self.workspace_name,
                path=area.path, branch=area.branch, context=ctx, repo=m.repo.ref,
                baseline=baseline)

            if result.violations:
                # Recorded as its own event, not folded into the mission
                # summary. A boundary breach is what someone searches the
                # timeline for, and it must be findable without reading prose.
                self._record(
                    "authority_violation", m.task.key, run.id,
                    "; ".join(f"{v.kind} {v.path}".strip()
                              for v in result.violations)[:300],
                    violations=[{"kind": v.kind, "detail": v.detail,
                                 "path": v.path} for v in result.violations],
                    repository=m.repo.ref.key, branch=area.branch)
            if result.discrepancies:
                self._record(
                    "claim_discrepancy", m.task.key, run.id,
                    f"{len(result.discrepancies)} gap(s) between what the agent "
                    f"claimed and what the engine observed",
                    discrepancies=[{"kind": d.kind, "detail": d.detail}
                                   for d in result.discrepancies])

            run.state = (RunState.SUCCEEDED if not result.stopped_early
                         else RunState.FAILED)
            run.ended_at = now()
            run.reason = result.reason
            run.cost_usd, run.tokens = result.total_cost_usd, result.total_tokens
            run.tool_calls, run.iterations = result.total_tool_calls, len(result.attempts)
            self.store.save_run(run)

            promoted, promotion_reason = self._persist_target(m, result)
            commit_sha, commit_reason = self._maybe_commit(m, area, result)
            if commit_sha:
                self._record("commit_written", m.task.key, run.id,
                             f"{commit_sha[:12]} on {area.branch}",
                             sha=commit_sha, branch=area.branch,
                             repository=m.repo.ref.key,
                             workspace_id=self.workspace_id, reason=commit_reason)

            delivered = self._maybe_deliver(m, area, run, result, commit_sha)

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
            return MissionOutcome(briefing, result.verdict, result, measured,
                                  area.path, commit_sha=commit_sha,
                                  commit_reason=commit_reason, promoted=promoted,
                                  promotion_reason=promotion_reason,
                                  delivery=delivered)
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

    def _persist_target(self, m: mission.Mission,
                        result: coder.LoopResult) -> tuple[bool, str]:
        """Write the discovery down; promote only under the independent rule.

        Recording always happens -- the evidence is worth keeping whatever the
        outcome. Promotion is a separate decision, and it deliberately does not
        ask whether the agent succeeded: an agent can write into any repository
        it is handed, so its success says nothing about whether the target was
        right. See `validation.may_promote_target`.
        """
        d = m.discovery
        self.store.record_target(
            workspace_id=self.workspace_id, task_key=m.task.key,
            provider=m.repo.ref.provider, repo_key=m.repo.ref.key,
            source=d.source.value, confidence=d.confidence.value,
            strength=d.strength,
            evidence=[f"{e.source}: {e.detail}" for e in d.evidence],
            alternatives=[f"{r.repo}: {r.reason}" for r in d.alternatives_considered])

        judgement = validation.may_promote_target(d, result)
        if not judgement.allowed:
            return False, judgement.reason
        promoted = self.store.promote_target(
            self.workspace_id, m.task.key, m.repo.ref.provider, m.repo.ref.key)
        return promoted, judgement.reason

    def _maybe_commit(self, m: mission.Mission, area, result: coder.LoopResult
                      ) -> tuple[str, str]:
        """Commit, if and only if the ENGINE and POLICY both say so.

            Agent  -> Outcome
            Engine -> Validation
            Policy -> Permission
            Engine -> Commit

        The agent's opinion never enters this chain. It may have reported
        "ready to commit"; that is an observation, and observations do not
        authorise writes.
        """
        judgement = validation.may_commit(result.verdict, result)
        if not judgement.allowed:
            return "", f"engine declined: {judgement.reason}"

        decision = self.policy.decide(PolicyContext(
            action=Action(kind=validation.COMMIT_ACTION, resource=m.repo.ref.key,
                          environment=self.environment),
            organization=self.organization, client=self.client,
            workspace=self.workspace_name, project=m.task.project or "*",
            agent=self.agent_name, risk=m.risk.level.name,
            autonomy=self.autonomy))
        if decision.effect != Effect.ALLOW:
            return "", f"policy {decision.effect}: {decision.reason}"

        try:
            sha = self.areas.commit(
                area, message=f"{m.task.key}: {m.task.title}"[:72],
                author=self.commit_author)
        except AdapterError as e:
            return "", f"commit refused by the workspace: {e}"
        return sha, judgement.reason

    def _new_run(self, m: mission.Mission) -> Run:
        """A identidade do run, decidida num lugar so.

        `task_id` guarda um ID INTERNO ou nada. Este caminho gravava ali a chave
        do fornecedor, e `task_runs` casa por id -- entao o run ficava invisivel
        a partir da sua propria task, sem erro e sem log. Vazio quando nao ha
        linha de task e a verdade: a missao avulsa monta o trabalho a partir do
        provedor, e nem sempre existe linha a que apontar.
        """
        stored = (self.store.task_by_key(self.workspace_id, self.task_provider,
                                         m.task.key)
                  if self.task_provider else None)
        return Run(id=ids.new_id(ids.RUN), task_id=stored.id if stored else "",
                   task_key=m.task.key, workspace_id=self.workspace_id,
                   agent=self.agent_name, state=RunState.RUNNING)

    def _maybe_deliver(self, m: mission.Mission, area, run: Run,
                       result: coder.LoopResult, commit_sha: str
                       ) -> "pipeline.DeliveryOutcome | None":
        """Carry the committed change towards a reviewer, or say why not.

        Three preconditions, each of which is a refusal to guess:

        * **a stage** -- without a configured remote there is nowhere to
          deliver, and inventing one would be worse than stopping.
        * **a commit the engine wrote** -- `commit_sha` is empty whenever the
          engine or the policy declined. Delivering anyway would push a change
          the engine itself refused to record.
        * **a task the store can name** -- the mission carries the origin's key;
          the delivery moves a row. Without the row there is no state to move,
          and moving the wrong one is worse than moving none.

        Everything after that -- policy, identity, SHA, idempotency -- is asked
        again inside the stage, per step. Reaching this line authorises nothing.
        """
        if self.delivery is None or not commit_sha:
            return None
        task = self.store.task_by_key(self.workspace_id, self.task_provider,
                                      m.task.key)
        if task is None:
            self._record("delivery_skipped", m.task.key, run.id,
                         "the task is not in this workspace's store; the "
                         "delivery has no state to move")
            return None

        identity = remote.RemoteIdentity(
            workspace_id=self.workspace_id, workspace_name=self.workspace_name,
            organization=self.organization, client=self.client,
            task_key=m.task.key, run_id=run.id, repo=m.repo.ref,
            branch=area.branch or "", commit_sha=commit_sha)

        outcome = self.delivery.advance(
            identity, area, result.verdict, task.id,
            title=f"{m.task.key}: {m.task.title}"[:72],
            body=self._pull_request_body(m, result, commit_sha),
            risk=m.risk.level.name)

        self._record("delivery", m.task.key, run.id,
                     f"{outcome.step.value}: {outcome.reason}"[:300],
                     step=outcome.step.value, sha=commit_sha,
                     pull_request=outcome.pull_request,
                     resumed=list(outcome.resumed),
                     ci=outcome.ci.state.value if outcome.ci else None,
                     workspace_id=self.workspace_id)
        return outcome

    def _pull_request_body(self, m: mission.Mission, result: coder.LoopResult,
                           commit_sha: str) -> str:
        """What a reviewer needs, written so it cannot be mistaken for approval.

        Says what was done, what was measured, and by whom. It states the limits
        of its own evidence in the text, because a pull request body is read by
        a person deciding whether to merge, and a confident summary of an
        unverified change is how a machine borrows authority it does not have.
        """
        tests = result.test_verdict
        if tests is None:
            verified = "no test verdict was produced"
        else:
            verified = f"{tests.result.value} ({tests.command or 'no command'})"

        lines = [
            f"**{m.task.key}** -- {m.task.title}",
            "",
            f"- repository: `{m.repo.ref.key}` (target {m.discovery.confidence.value})",
            f"- engine verdict: `{result.verdict.value}`",
            f"- tests: {verified}",
            f"- files changed: {len(result.changed_files)}",
            f"- commit written by the engine: `{commit_sha[:12]}`",
            "",
            "Written by an automated agent under an engine that validated the "
            "change and wrote the commit itself. The agent did not decide to "
            "open this; it does not decide whether it merges. Review it as you "
            "would any other proposal.",
        ]
        return "\n".join(lines)

    def _record(self, kind: str, task_key: str, run_id: str, summary: str,
                **data) -> None:
        self.store.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id=self.workspace_id, kind=kind,
            task_id=task_key, run_id=run_id, summary=summary, data=data))
