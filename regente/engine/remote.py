# -*- coding: utf-8 -*-
"""Crossing the boundary: push, pull request, CI observation.

    commit -> [policy + identity + SHA] -> push
           -> [policy + identity + SHA] -> pull request
           -> observe CI -> classify -> persist

**Every remote mutation revalidates from scratch.** Not once at the start of the
flow: once per mutation. Validation and mutation are separated in time, and the
world moves in between -- a branch can gain a commit, a person can open a pull
request, a policy can be edited. A guard that ran three steps ago is a fact about
the past.

The three things revalidated before each write are always the same:

  - **policy**, because authority is not inherited from the previous step;
  - **identity**, because the workspace, repository, task and run must still be
    the ones the mutation was computed for;
  - **the SHA**, because publishing a commit the engine did not verify means
    vouching for work it never saw.

The agent appears nowhere in this file. It has no capability to push or to open a
pull request, and it does not acquire one by having produced the commit -- the
same rule that already governs committing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..core.policy import (Action, AutonomyLevel, Effect, PolicyContext,
                           PolicyEngine)
from ..ports import AdapterError
from ..ports.delivery import CICDProvider, PipelineStatus
from ..ports.repository import (PullRequest, RepoRef, build_marker,
                                read_marker)
from ..ports.store import Store
from ..ports.workspace import WorkArea, WorkspaceProvider
from . import ci as ci_module

#: Policy actions for the two remote mutations. Distinct strings, because they
#: are distinct authorities: a workspace may be allowed to publish a branch and
#: not to open a pull request, and one action name could not express that.
PUSH_ACTION = "repo.push"
PR_ACTION = "repo.pr.create"


class Refused(RuntimeError):
    """A remote mutation was not performed, and the reason is stated.

    An exception rather than a return value: every caller must handle it, and a
    refusal that can be ignored by forgetting to check a boolean is a refusal
    that will eventually be ignored.
    """


@dataclass(frozen=True, slots=True)
class RemoteIdentity:
    """Who is acting, on what. Rebuilt and rechecked before every mutation."""
    workspace_id: str
    workspace_name: str
    organization: str
    client: str
    task_key: str
    run_id: str
    repo: RepoRef
    branch: str
    commit_sha: str

    def marker(self) -> str:
        return build_marker(self.workspace_id, self.task_key, self.run_id)


@dataclass(frozen=True, slots=True)
class PushOutcome:
    sha: str
    target: str


@dataclass(slots=True)
class RemoteDelivery:
    """Owns every write that leaves the machine."""

    store: Store
    areas: WorkspaceProvider
    #: Pull-request capable adapter. Optional and separate from `areas` because
    #: publishing a branch and opening a pull request are distinct authorities:
    #: a workspace may hold the first and not the second, and a single required
    #: dependency could not express that.
    repos_write: object | None
    cicd: CICDProvider | None
    policy: PolicyEngine
    autonomy: AutonomyLevel
    environment: str = "staging"
    agent_name: str = "coder"
    wait_policy: ci_module.WaitPolicy = field(
        default_factory=ci_module.WaitPolicy)

    # ------------------------------------------------------------------
    def _authorise(self, action: str, identity: RemoteIdentity, risk: str) -> None:
        decision = self.policy.decide(PolicyContext(
            action=Action(kind=action, resource=identity.repo.key,
                          environment=self.environment),
            organization=identity.organization, client=identity.client,
            workspace=identity.workspace_name, project="*",
            agent=self.agent_name, risk=risk, autonomy=self.autonomy))
        if decision.effect != Effect.ALLOW:
            raise Refused(f"{action} {decision.effect}: {decision.reason}")

    def _confirm_identity(self, area: WorkArea, identity: RemoteIdentity) -> None:
        """The area must still be the one this mutation was computed for."""
        if area.repo != identity.repo.key:
            raise Refused(
                f"identity drift: the area holds '{area.repo}' and the mutation "
                f"targets '{identity.repo.key}'")
        if area.branch != identity.branch:
            raise Refused(
                f"identity drift: the area is on '{area.branch}' and the mutation "
                f"targets '{identity.branch}'")

    def _confirm_sha(self, area: WorkArea, identity: RemoteIdentity) -> str:
        actual = self.areas.head(area)
        if actual != identity.commit_sha:
            raise Refused(
                f"the branch moved: expected {identity.commit_sha[:12]}, found "
                f"{actual[:12]}. Publishing now would vouch for a commit that "
                f"was never validated")
        return actual

    # ---- mutation 1: push ------------------------------------------------
    def push(self, area: WorkArea, identity: RemoteIdentity, delivery_id: str,
             risk: str = "LOW") -> PushOutcome:
        self._authorise(PUSH_ACTION, identity, risk)
        self._confirm_identity(area, identity)
        self._confirm_sha(area, identity)

        target = self.areas.push_target(area)
        if not target:
            raise Refused("the area has no push target; a push is impossible")

        try:
            sha = self.areas.push(area, expected_sha=identity.commit_sha,
                                  branch=identity.branch)
        except AdapterError as e:
            raise Refused(f"push refused by the workspace: {e}") from e

        self.store.record_push(delivery_id, target)
        return PushOutcome(sha=sha, target=target)

    # ---- mutation 2: pull request ---------------------------------------
    def open_pull_request(self, area: WorkArea, identity: RemoteIdentity,
                          delivery_id: str, base: str, title: str, body: str,
                          risk: str = "LOW") -> PullRequest:
        self._authorise(PR_ACTION, identity, risk)
        self._confirm_identity(area, identity)
        self._confirm_sha(area, identity)

        if self.repos_write is None:
            raise Refused(
                "no pull-request provider is configured for this workspace; "
                "opening one is impossible, not implicit")

        existing = self.repos_write.find_pull_request_for_branch(
            identity.repo.key, identity.branch)
        if existing is not None:
            self._refuse_or_adopt(existing, identity)
            return existing

        created = self.repos_write.create_pull_request(
            repo=identity.repo.key, branch=identity.branch, base=base,
            title=title, body=body, marker=identity.marker())

        # Read back rather than trust. The PR is only this run's if its head is
        # the commit the engine pushed -- the number alone binds nothing.
        if created.head_sha != identity.commit_sha:
            raise Refused(
                f"pull request #{created.number} opened at {created.head_sha[:12]} "
                f"but the run's commit is {identity.commit_sha[:12]}; the binding "
                f"cannot be trusted")

        self.store.record_pull_request(delivery_id, created.number, created.url,
                                       created.head_sha)
        return created

    def _refuse_or_adopt(self, existing: PullRequest,
                         identity: RemoteIdentity) -> None:
        """A pull request already targets this branch. Whose is it?

        Adoption requires BOTH proofs: the marker naming this workspace, task and
        run, and the head matching the commit this run produced. Either alone is
        not enough -- a marker can be copied into a body, and a head can coincide
        after a rebase. Anything else is somebody's work, and the engine has no
        way to know their intent.
        """
        marker = read_marker(existing.data.get("body", ""))

        if marker is None:
            raise Refused(
                f"pull request #{existing.number} already targets "
                f"'{identity.branch}' and carries no run marker. It is not this "
                f"engine's to touch")
        if (marker["workspace_id"] != identity.workspace_id
                or marker["task_key"] != identity.task_key):
            raise Refused(
                f"pull request #{existing.number} belongs to workspace "
                f"'{marker['workspace_id']}' / task '{marker['task_key']}', not to "
                f"'{identity.workspace_id}' / '{identity.task_key}'")
        if existing.head_sha != identity.commit_sha:
            raise Refused(
                f"pull request #{existing.number} carries this run's marker but its "
                f"head is {existing.head_sha[:12]}, not {identity.commit_sha[:12]}; "
                f"something moved it and adoption would be a guess")
        if marker["run_id"] != identity.run_id:
            raise Refused(
                f"pull request #{existing.number} belongs to run "
                f"'{marker['run_id']}', not '{identity.run_id}'")

    # ---- observation: never a mutation ----------------------------------
    def observe_ci(self, identity: RemoteIdentity, delivery_id: str,
                   base_sha: str | None = None) -> ci_module.CIObservation:
        """Read the checks for THIS commit. Never triggers, cancels or re-runs.

        Asks about the SHA rather than the pull request: a PR's head moves, and
        asking about a PR answers about whatever is on it now -- which may not be
        the commit the engine validated.
        """
        if self.cicd is None:
            observation = ci_module.unavailable(
                "no CI provider is configured for this workspace")
            self._persist_ci(delivery_id, observation)
            return observation

        try:
            status = self.cicd.get_status(identity.repo.key, identity.commit_sha)
        except AdapterError as e:
            # Unavailability is not a CI result. It is recorded as its own state
            # so a later reader can tell "nothing ran" from "nobody asked".
            observation = ci_module.unavailable(str(e)[:300])
            self._persist_ci(delivery_id, observation)
            return observation

        baseline: PipelineStatus | None = None
        if base_sha:
            try:
                baseline = self.cicd.get_status(identity.repo.key, base_sha)
            except AdapterError:
                # No baseline means no regression claim. Losing the baseline
                # degrades the verdict to UNKNOWN; it never upgrades it.
                baseline = None

        observation = ci_module.classify(status, baseline)
        self._persist_ci(delivery_id, observation)
        return observation

    def _persist_ci(self, delivery_id: str,
                    observation: ci_module.CIObservation) -> None:
        self.store.record_ci(
            delivery_id,
            state=observation.state.value,
            result=observation.result.value if observation.result else None,
            reason=observation.reason,
            checks=[{"name": n, "status": "green"} for n in observation.green]
                   + [{"name": n, "status": "red"} for n in observation.red]
                   + [{"name": n, "status": "running"} for n in observation.running])
