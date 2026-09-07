# -*- coding: utf-8 -*-
"""`TESTING` is no longer a dead end: push, pull request, CI, and every refusal.

These are contract tests. The remote is a fake, and it is a deliberately
**obedient** fake: it does whatever it is told, so every refusal below comes
from the engine rather than from a fake declining to cooperate. Nothing here is
evidence that the real path works -- see `MILESTONE-11-CANDIDATES.md` for why the
real path is blocked and by what.

The centre of the file is idempotency. A process can die between a remote
mutation and the row that records it, and the two disagree afterwards: the local
state says nothing happened, the remote says something did. Assuming success
loses work; assuming failure duplicates it. The engine asks the remote instead,
and these tests are what keep it asking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from regente.core.model import RunState, Workspace, now
from regente.core.policy import AutonomyLevel, PolicyEngine
from regente.core.states import TaskState
from regente.engine import ci as ci_module
from regente.engine.pipeline import DeliveryStage, Step
from regente.engine.remote import RemoteDelivery, RemoteIdentity, Refused
from regente.engine.store_sqlite import SqliteStore
from regente.ports import AdapterError
from regente.ports.agent import TestResult, Verdict
from regente.ports.delivery import Check, PipelineStatus
from regente.ports.repository import PullRequest, RepoRef, build_marker
from regente.ports.workspace import WorkArea

SHA = "a" * 40
OTHER_SHA = "b" * 40
REPO = RepoRef(provider="github", key="acme/worker")
WORKSPACE = "wks_1"
BRANCH = "regente/k-1"

RULES = [{"name": "deliver", "effect": "ALLOW",
          "match": {"action": ["repo.push*", "repo.pr.create"]}}]


def identity(**kw) -> RemoteIdentity:
    base = dict(workspace_id=WORKSPACE, workspace_name="ws", organization="org",
                client="cli", task_key="K-1", run_id="run_9", repo=REPO,
                branch=BRANCH, commit_sha=SHA)
    return RemoteIdentity(**{**base, **kw})


def area(**kw) -> WorkArea:
    base = dict(id="K-1", path="/tmp/area", branch=BRANCH, repo=REPO.key)
    return WorkArea(**{**base, **kw})


@dataclass
class FakeAreas:
    head_sha: str = SHA
    target: str | None = "https://github.com/acme/worker.git"
    pushes: list = field(default_factory=list)

    def head(self, a):
        return self.head_sha

    def push_target(self, a):
        return self.target

    def push(self, a, expected_sha, branch=None):
        self.pushes.append((branch, expected_sha))
        return self.head_sha


def pull_request(number=7, head=SHA, body="", branch=BRANCH) -> PullRequest:
    return PullRequest(number=number, repo=REPO, title="t",
                       url=f"https://github.com/acme/worker/pull/{number}",
                       state="OPEN", head_sha=head, branch=branch, base="main",
                       author="regente", data={"body": body})


@dataclass
class FakeRepos:
    """An obedient remote. Every refusal in this file comes from the engine."""
    branch_sha: str | None = None
    existing: PullRequest | None = None
    created: PullRequest | None = None
    creations: list = field(default_factory=list)
    reads: list = field(default_factory=list)
    fail_branch_read: Exception | None = None

    def remote_branch_sha(self, repo, branch):
        self.reads.append(branch)
        if self.fail_branch_read:
            raise self.fail_branch_read
        return self.branch_sha

    def find_pull_request_for_branch(self, repo, branch):
        return self.existing

    def create_pull_request(self, repo, branch, base, title, body, marker):
        self.creations.append((repo, branch, base, marker))
        made = self.created or pull_request(body=f"{body}\n{marker}")
        self.existing = made          # a real remote would now return it
        self.branch_sha = made.head_sha
        return made


@dataclass
class FakeCI:
    status: PipelineStatus | None = None
    error: Exception | None = None
    asked: list = field(default_factory=list)

    def get_status(self, repo, reference):
        self.asked.append(reference)
        if self.error:
            raise self.error
        return self.status or PipelineStatus(reference=reference)


@pytest.fixture
def store(tmp_path) -> SqliteStore:
    s = SqliteStore(tmp_path / "p.db")
    s.migrate()
    s.save_workspace(Workspace(id=WORKSPACE, client_id="cli", name="ws",
                               max_autonomy=AutonomyLevel.L3))
    yield s
    s.close()


def a_task(store: SqliteStore, key: str = "K-1",
           state: TaskState = TaskState.TESTING) -> str:
    """A task already at TESTING, the state this milestone unblocks."""
    from regente.core import ids
    from regente.core.model import ExternalRef, Task

    task = Task(id=ids.new_id(ids.TASK), workspace_id=WORKSPACE,
                project_id="prj", title="contended work", state=TaskState.READY,
                externo=ExternalRef(provider="filesystem", key=key))
    store.save_task(task)
    for step in (TaskState.ASSIGNED, TaskState.IMPLEMENTING, TaskState.TESTING):
        store.transition(task.id, step, actor="test", reason="setup",
                         workspace_id=WORKSPACE)
    return task.id


def stage(store, areas=None, repos=None, cicd=None, rules=None,
          autonomy=AutonomyLevel.L2) -> tuple[DeliveryStage, FakeRepos]:
    repos = repos if repos is not None else FakeRepos()
    delivery = RemoteDelivery(
        store=store, areas=areas or FakeAreas(), repos_write=repos, cicd=cicd,
        policy=PolicyEngine.from_config(RULES if rules is None else rules),
        autonomy=autonomy)
    return DeliveryStage(store=store, delivery=delivery,
                         workspace_id=WORKSPACE), repos


# ---------------------------------------------------------------------------
# The road exists
# ---------------------------------------------------------------------------

def test_testing_is_no_longer_a_dead_end(store):
    """The point of the milestone, in one test.

    A validated change at TESTING now reaches PR_CREATED and CI_RUNNING instead
    of parking in a state the scheduler skips for ever.
    """
    task_id = a_task(store)
    ci = FakeCI(status=PipelineStatus(
        reference=SHA, checks=(Check(name="ci", conclusion="SUCCESS",
                                     state="COMPLETED"),)))
    s, repos = stage(store, cicd=ci)

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW,
                        task_id, title="fix the thing")

    assert outcome.step is Step.CI_OBSERVED
    assert outcome.pull_request == 7
    assert outcome.ci.result is TestResult.PASSED
    assert outcome.task_state is TaskState.CI_RUNNING
    assert store.task(task_id, WORKSPACE).state is TaskState.CI_RUNNING


def test_the_whole_chain_is_persisted_with_identity(store):
    """task -> run -> commit -> push -> PR -> CI, each link answerable."""
    task_id = a_task(store)
    ci = FakeCI(status=PipelineStatus(
        reference=SHA, checks=(Check(name="ci", conclusion="SUCCESS",
                                     state="COMPLETED"),)))
    s, _ = stage(store, cicd=ci)
    s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id, title="t")

    row = store.deliveries(WORKSPACE, "K-1")[0]
    assert row["workspace_id"] == WORKSPACE
    assert row["task_key"] == "K-1"
    assert row["run_id"] == "run_9"
    assert row["repo_provider"] == "github"
    assert row["repo_key"] == REPO.key
    assert row["branch"] == BRANCH
    assert row["commit_sha"] == SHA
    assert row["pushed_at"] and row["push_target"]
    assert row["pr_number"] == 7 and row["pr_head_sha"] == SHA
    assert row["ci_state"] == "CONCLUDED" and row["ci_result"] == "PASSED"


# ---------------------------------------------------------------------------
# Idempotency: the same operation twice must not mutate twice
# ---------------------------------------------------------------------------

def test_a_push_that_already_landed_is_recorded_not_repeated(store):
    """The process died after pushing and before recording it.

    The local row says nothing was pushed; the remote branch is already at this
    run's commit. Pushing again would be a blind second mutation.
    """
    task_id = a_task(store)
    areas = FakeAreas()
    repos = FakeRepos(branch_sha=SHA)          # the remote already has it
    s, _ = stage(store, areas=areas, repos=repos, cicd=FakeCI())

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")

    assert areas.pushes == [], "the engine pushed a commit that was already there"
    assert "push" in outcome.resumed
    assert store.deliveries(WORKSPACE, "K-1")[0]["pushed_at"]


def test_a_pull_request_this_run_already_opened_is_adopted_not_duplicated(store):
    task_id = a_task(store)
    mine = build_marker(WORKSPACE, "K-1", "run_9")
    repos = FakeRepos(branch_sha=SHA,
                      existing=pull_request(number=42, head=SHA, body=mine))
    s, _ = stage(store, repos=repos, cicd=FakeCI())

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")

    assert repos.creations == [], "a second pull request was opened"
    assert outcome.pull_request == 42
    assert store.deliveries(WORKSPACE, "K-1")[0]["pr_number"] == 42


def test_running_the_whole_stage_twice_changes_nothing(store):
    """The strongest form: run it, run it again, compare the world."""
    task_id = a_task(store)
    areas = FakeAreas()
    ci = FakeCI(status=PipelineStatus(
        reference=SHA, checks=(Check(name="ci", conclusion="SUCCESS",
                                     state="COMPLETED"),)))
    s, repos = stage(store, areas=areas, cicd=ci)

    first = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                      title="t")
    second = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                       title="t")

    assert first.pull_request == second.pull_request
    assert len(areas.pushes) == 1, "the second run pushed again"
    assert len(repos.creations) == 1, "the second run opened another PR"
    assert len(store.deliveries(WORKSPACE, "K-1")) == 1, (
        "a resumed delivery opened a second ledger row")
    assert "push" in second.resumed


def test_a_resume_reuses_the_delivery_row(store):
    """One row per (task, run, commit), or the ledger cannot be read back."""
    task_id = a_task(store)
    s, _ = stage(store, cicd=FakeCI())
    s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id, title="t")
    row_id = store.deliveries(WORKSPACE, "K-1")[0]["id"]

    s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id, title="t")
    rows = store.deliveries(WORKSPACE, "K-1")
    assert len(rows) == 1 and rows[0]["id"] == row_id


def test_a_remote_that_cannot_be_read_stops_the_delivery(store):
    """Not knowing is not permission to push again.

    The dangerous reading of a failed read is "probably nothing is there".
    """
    task_id = a_task(store)
    areas = FakeAreas()
    repos = FakeRepos(fail_branch_read=AdapterError("502 from the provider"))
    s, _ = stage(store, areas=areas, repos=repos)

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")

    assert outcome.step is Step.REFUSED
    assert "Not knowing is not permission" in outcome.reason
    assert areas.pushes == []
    assert store.task(task_id, WORKSPACE).state is TaskState.TESTING


def test_an_adapter_that_cannot_read_a_branch_is_refused(store):
    """An adapter without the read cannot support an idempotent push."""
    task_id = a_task(store)

    class WriteOnly(FakeRepos):
        remote_branch_sha = None      # the capability is simply absent

    areas = FakeAreas()
    s, _ = stage(store, areas=areas, repos=WriteOnly())
    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")

    assert outcome.step is Step.REFUSED
    assert "cannot read a branch" in outcome.reason
    assert areas.pushes == []


# ---------------------------------------------------------------------------
# Mandatory negative paths
# ---------------------------------------------------------------------------

def test_a_verdict_short_of_validation_never_reaches_a_remote(store):
    for verdict in (Verdict.REGRESSED, Verdict.BLOCKED, Verdict.NO_CHANGE,
                    Verdict.NEEDS_HUMAN, Verdict.FAILED):
        task_id = a_task(store, key=f"K-{verdict.value}")
        areas = FakeAreas()
        s, repos = stage(store, areas=areas)
        outcome = s.advance(identity(task_key=f"K-{verdict.value}"), area(),
                            verdict, task_id, title="t")

        assert outcome.step is Step.REFUSED, verdict
        assert areas.pushes == [] and repos.creations == []
        assert store.task(task_id, WORKSPACE).state is TaskState.TESTING


def test_a_branch_that_moved_since_validation_is_refused(store):
    task_id = a_task(store)
    areas = FakeAreas(head_sha=OTHER_SHA)     # someone committed meanwhile
    s, repos = stage(store, areas=areas)

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")

    assert outcome.step is Step.REFUSED
    assert "branch moved" in outcome.reason
    assert areas.pushes == [] and repos.creations == []


def test_a_pull_request_opened_at_another_head_is_refused(store):
    task_id = a_task(store)
    repos = FakeRepos(created=pull_request(head=OTHER_SHA))
    s, _ = stage(store, repos=repos)

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")

    assert outcome.step is Step.PUSHED
    assert "binding cannot be trusted" in outcome.reason
    assert store.task(task_id, WORKSPACE).state is TaskState.TESTING, (
        "the task advanced on a pull request the engine could not vouch for")


def test_somebody_elses_pull_request_is_never_adopted(store):
    task_id = a_task(store)
    repos = FakeRepos(branch_sha=SHA,
                      existing=pull_request(number=3, body="opened by a person"))
    s, _ = stage(store, repos=repos)

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")

    assert outcome.step is Step.PUSHED
    assert "no run marker" in outcome.reason
    assert repos.creations == []


def test_a_pull_request_from_another_tenant_is_never_adopted(store):
    task_id = a_task(store)
    theirs = build_marker("wks_OTHER", "K-1", "run_9")
    repos = FakeRepos(branch_sha=SHA,
                      existing=pull_request(number=5, head=SHA, body=theirs))
    s, _ = stage(store, repos=repos)

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")
    assert outcome.step is Step.PUSHED
    assert "belongs to workspace" in outcome.reason


def test_an_area_holding_another_branch_is_refused(store):
    task_id = a_task(store)
    areas = FakeAreas()
    s, _ = stage(store, areas=areas)

    outcome = s.advance(identity(), area(branch="someone-else"),
                        Verdict.READY_FOR_REVIEW, task_id, title="t")

    assert outcome.step is Step.REFUSED
    assert "identity drift" in outcome.reason
    assert areas.pushes == []


def test_an_area_holding_another_repository_is_refused(store):
    task_id = a_task(store)
    areas = FakeAreas()
    s, _ = stage(store, areas=areas)

    outcome = s.advance(identity(), area(repo="acme/other"),
                        Verdict.READY_FOR_REVIEW, task_id, title="t")
    assert outcome.step is Step.REFUSED
    assert "identity drift" in outcome.reason


def test_policy_denial_stops_the_delivery_before_any_mutation(store):
    task_id = a_task(store)
    areas = FakeAreas()
    s, repos = stage(store, areas=areas,
                     rules=[{"name": "no", "effect": "DENY",
                             "match": {"action": "repo.push*"}}])

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")
    assert outcome.step is Step.REFUSED
    assert "DENY" in outcome.reason
    assert areas.pushes == [] and repos.creations == []


def test_an_autonomy_ceiling_below_the_action_stops_it(store):
    task_id = a_task(store)
    areas = FakeAreas()
    s, _ = stage(store, areas=areas, autonomy=AutonomyLevel.L1)

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")
    assert outcome.step is Step.REFUSED
    assert "HUMAN_APPROVAL" in outcome.reason
    assert areas.pushes == []


# ---------------------------------------------------------------------------
# CI: a result, or an honest absence of one
# ---------------------------------------------------------------------------

def test_a_pull_request_existing_is_not_evidence_that_ci_passed(store):
    task_id = a_task(store)
    ci = FakeCI(status=PipelineStatus(
        reference=SHA, checks=(Check(name="ci", conclusion="", state="IN_PROGRESS"),)))
    s, _ = stage(store, cicd=ci)

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")
    assert outcome.ci.state is ci_module.CIState.PENDING
    assert outcome.ci.result is None
    assert not outcome.ci.allows_progress


def test_ci_unavailable_is_never_success(store):
    task_id = a_task(store)
    ci = FakeCI(error=AdapterError("the provider is down"))
    s, _ = stage(store, cicd=ci)

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")
    assert outcome.ci.state is ci_module.CIState.UNAVAILABLE
    assert outcome.ci.result is None
    assert not outcome.ci.allows_progress


def test_no_checks_is_recorded_and_proves_nothing(store):
    task_id = a_task(store)
    ci = FakeCI(status=PipelineStatus(reference=SHA, confirmed_no_checks=True))
    s, _ = stage(store, cicd=ci)

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")
    assert outcome.ci.state is ci_module.CIState.NO_CHECKS
    assert outcome.ci.result is None
    assert "nothing was verified" in outcome.ci.reason


def test_ci_is_asked_about_the_commit_never_the_pull_request(store):
    """A pull request's head moves; a SHA cannot drift."""
    task_id = a_task(store)
    ci = FakeCI()
    s, _ = stage(store, cicd=ci)
    s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id, title="t")
    assert ci.asked == [SHA]


def test_a_red_ci_never_produces_approval_merge_or_done(store):
    """CI PASS does not imply APPROVED; CI FAIL certainly does not."""
    task_id = a_task(store)
    ci = FakeCI(status=PipelineStatus(
        reference=SHA, checks=(Check(name="ci", conclusion="FAILURE",
                                     state="COMPLETED"),)))
    s, _ = stage(store, cicd=ci)

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")
    assert outcome.ci.result is TestResult.UNKNOWN     # red with no baseline
    assert store.task(task_id, WORKSPACE).state is TaskState.CI_RUNNING
    for forbidden in (TaskState.APPROVED, TaskState.MERGING, TaskState.DONE):
        assert store.task(task_id, WORKSPACE).state is not forbidden


def test_a_green_ci_does_not_by_itself_approve_or_merge(store):
    """The counter-proof the milestone asks for, stated as a test."""
    task_id = a_task(store)
    ci = FakeCI(status=PipelineStatus(
        reference=SHA, checks=(Check(name="ci", conclusion="SUCCESS",
                                     state="COMPLETED"),)))
    s, _ = stage(store, cicd=ci)

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")

    assert outcome.ci.result is TestResult.PASSED
    assert store.task(task_id, WORKSPACE).state is TaskState.CI_RUNNING, (
        "green CI advanced the task on its own; passing checks are evidence "
        "about the checks, not a decision about the change")


# ---------------------------------------------------------------------------
# Tenancy holds through the pipeline
# ---------------------------------------------------------------------------

def test_two_tenants_deliver_the_same_names_without_crossing(tmp_path):
    """Same task key, same repository, same branch, different everything else."""
    other = "wks_2"
    s = SqliteStore(tmp_path / "two.db")
    s.migrate()
    for workspace in (WORKSPACE, other):
        s.save_workspace(Workspace(id=workspace, client_id="c", name="ws",
                                   max_autonomy=AutonomyLevel.L3))
    try:
        a_id = a_task(s)

        from regente.core import ids
        from regente.core.model import ExternalRef, Task
        b_task = Task(id=ids.new_id(ids.TASK), workspace_id=other,
                      project_id="prj", title="theirs", state=TaskState.READY,
                      externo=ExternalRef(provider="filesystem", key="K-1"))
        s.save_task(b_task)
        for step in (TaskState.ASSIGNED, TaskState.IMPLEMENTING,
                     TaskState.TESTING):
            s.transition(b_task.id, step, actor="test", reason="setup",
                         workspace_id=other)

        ci_a = FakeCI(status=PipelineStatus(reference=SHA))
        ci_b = FakeCI(status=PipelineStatus(reference=OTHER_SHA))
        stage_a, repos_a = stage(s, cicd=ci_a)
        delivery_b = RemoteDelivery(
            store=s, areas=FakeAreas(head_sha=OTHER_SHA),
            repos_write=FakeRepos(created=pull_request(number=7, head=OTHER_SHA)),
            cicd=ci_b, policy=PolicyEngine.from_config(RULES),
            autonomy=AutonomyLevel.L2)
        stage_b = DeliveryStage(store=s, delivery=delivery_b,
                                workspace_id=other)

        stage_a.advance(identity(), area(), Verdict.READY_FOR_REVIEW, a_id,
                        title="a")
        stage_b.advance(identity(workspace_id=other, commit_sha=OTHER_SHA),
                        area(), Verdict.READY_FOR_REVIEW, b_task.id, title="b")

        rows_a = s.deliveries(WORKSPACE, "K-1")
        rows_b = s.deliveries(other, "K-1")
        assert len(rows_a) == 1 and len(rows_b) == 1
        assert rows_a[0]["id"] != rows_b[0]["id"]
        assert rows_a[0]["commit_sha"] == SHA
        assert rows_b[0]["commit_sha"] == OTHER_SHA

        # The same pull request NUMBER in both, and they stay distinct rows.
        assert rows_a[0]["pr_number"] == rows_b[0]["pr_number"] == 7
        assert s.delivery_for_pr(WORKSPACE, "github", REPO.key, 7)["id"] \
            == rows_a[0]["id"]
        assert s.delivery_for_pr(other, "github", REPO.key, 7)["id"] \
            == rows_b[0]["id"]

        # CI for A's SHA is never asked about B's.
        assert ci_a.asked == [SHA] and ci_b.asked == [OTHER_SHA]
    finally:
        s.close()


def test_a_delivery_row_answers_every_identity_question(store):
    task_id = a_task(store)
    ci = FakeCI(status=PipelineStatus(reference=SHA, confirmed_no_checks=True))
    s, _ = stage(store, cicd=ci)
    s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id, title="t")

    row = store.deliveries(WORKSPACE, "K-1")[0]
    for question, value in (
            ("which tenant", row["workspace_id"]),
            ("which task", row["task_key"]),
            ("which run", row["run_id"]),
            ("which repository", row["repo_key"]),
            ("which branch", row["branch"]),
            ("which SHA", row["commit_sha"]),
            ("which pull request", row["pr_number"]),
            ("which CI state", row["ci_state"])):
        assert value not in (None, ""), f"the ledger cannot answer: {question}"


# ---------------------------------------------------------------------------
# The task can move under us
# ---------------------------------------------------------------------------

def test_a_task_moved_off_the_road_mid_cycle_is_not_dragged_back(store):
    """Somebody escalated the task while the delivery was pushing.

    The pull request is real and stays recorded. The task belongs to whoever
    moved it, and the delivery does not overwrite that decision.
    """
    task_id = a_task(store)

    class MovesTheTask(FakeRepos):
        def create_pull_request(self, *a, **kw):
            store.transition(task_id, TaskState.BLOCKED, actor="a human",
                             reason="stop, I want to look at this",
                             workspace_id=WORKSPACE)
            return super().create_pull_request(*a, **kw)

    s, _ = stage(store, repos=MovesTheTask(), cicd=FakeCI())
    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")

    assert outcome.step is Step.PULL_REQUEST
    assert "left the delivery road" in outcome.reason
    assert outcome.pull_request == 7, "the delivery hid a pull request it opened"
    assert store.deliveries(WORKSPACE, "K-1")[0]["pr_number"] == 7
    assert store.task(task_id, WORKSPACE).state is TaskState.BLOCKED


def test_a_task_that_cannot_be_read_is_never_moved(store):
    task_id = a_task(store)
    s, _ = stage(store)
    s.workspace_id = "wks_OTHER"        # the tenant the delivery believes it is

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")
    assert outcome.step is Step.PULL_REQUEST
    assert "not readable in this workspace" in outcome.reason
    assert store.task(task_id, WORKSPACE).state is TaskState.TESTING


def test_a_restart_resumes_from_the_remote_not_from_optimism(tmp_path):
    """The process died between the push and the row that records it.

    A fresh store, a fresh stage: everything the first attempt learned in
    memory is gone. What it must not do is assume either way.
    """
    db = tmp_path / "restart.db"
    first = SqliteStore(db)
    first.migrate()
    first.save_workspace(Workspace(id=WORKSPACE, client_id="c", name="ws",
                                   max_autonomy=AutonomyLevel.L3))
    task_id = a_task(first)
    first.close()

    # The world as the dead process left it: pushed, nothing recorded.
    remote = FakeRepos(branch_sha=SHA)
    areas = FakeAreas()

    second = SqliteStore(db)
    try:
        s, _ = stage(second, areas=areas, repos=remote, cicd=FakeCI())
        outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW,
                            task_id, title="t")

        assert areas.pushes == [], "the restarted process pushed again"
        assert "push" in outcome.resumed
        assert outcome.pull_request == 7
        assert second.deliveries(WORKSPACE, "K-1")[0]["pushed_at"]
    finally:
        second.close()


# ---------------------------------------------------------------------------
# The tick comes back: CI_RUNNING is not the next dead end
# ---------------------------------------------------------------------------
#
# Moving the road's end from TESTING to CI_RUNNING would have been no progress
# at all. These are about the engine returning to a delivery it is waiting on,
# and about where it stops -- which is the moment a person is needed, whatever
# colour the checks came back.

from regente.core.model import Workspace as _Workspace     # noqa: E402


def watched(tmp_path, store, ci=None, deliveries=True):
    """An orchestrator whose only job here is to watch a delivery in flight."""
    from regente.adapters.notify.console import Console
    from regente.adapters.tasks.filesystem import FilesystemTasks
    from regente.adapters.workspace.local import IsolatedDirectory
    from regente.adapters.runner.scripted import ScriptedAgent
    from regente.core.risk import RiskEngine
    from regente.core.scheduling import Limits
    from regente.engine.gate import Gate
    from regente.engine.orchestrator import Orchestrator

    tasks_dir = tmp_path / "board"
    tasks_dir.mkdir(exist_ok=True)
    risk = RiskEngine()
    delivery = RemoteDelivery(
        store=store, areas=FakeAreas(), repos_write=FakeRepos(), cicd=ci,
        policy=PolicyEngine.from_config(RULES), autonomy=AutonomyLevel.L2)
    return Orchestrator(
        store=store, workspace=store.workspace(WORKSPACE),
        tasks_provider=FilesystemTasks(tasks_dir),
        area_provider=IsolatedDirectory(tmp_path / "areas"),
        runner=ScriptedAgent(script={}),
        gate=Gate(store=store, policy=PolicyEngine.from_config([]), risk=risk),
        risk=risk, limits=Limits(max_workers=1),
        notificador=Console(journal=tmp_path / "journal.log"),
        lease_seconds=900, delivery=delivery if deliveries else None)


def delivered(store, ci_state="PENDING") -> str:
    """A task at CI_RUNNING with a real delivery row behind it."""
    task_id = a_task(store)
    row = store.open_delivery(WORKSPACE, "K-1", "run_9", "github", REPO.key,
                              BRANCH, SHA)
    store.record_push(row, "https://github.com/acme/worker.git")
    store.record_pull_request(row, 7, "https://github.com/acme/worker/pull/7",
                              SHA)
    for step in (TaskState.PR_CREATED, TaskState.CI_RUNNING):
        store.transition(task_id, step, actor="test", reason="setup",
                         workspace_id=WORKSPACE)
    return task_id


def test_a_delivery_waiting_on_ci_is_read_again_next_tick(tmp_path, store):
    task_id = delivered(store)
    ci = FakeCI(status=PipelineStatus(
        reference=SHA, checks=(Check(name="ci", conclusion="", state="QUEUED"),)))
    orq = watched(tmp_path, store, ci)

    report = orq.tick()

    assert ci.asked == [SHA], "the tick did not come back to the delivery"
    assert "K-1" in report.ci_observed
    assert store.task(task_id, WORKSPACE).state is TaskState.CI_RUNNING
    assert store.deliveries(WORKSPACE, "K-1")[0]["ci_observations"] == 1


def test_a_conclusive_ci_reaches_a_person_and_never_a_merge(tmp_path, store):
    """Where the engine stops. Green does not mean approved, merged or done."""
    task_id = delivered(store)
    ci = FakeCI(status=PipelineStatus(
        reference=SHA, checks=(Check(name="ci", conclusion="SUCCESS",
                                     state="COMPLETED"),)))
    orq = watched(tmp_path, store, ci)
    orq.tick()

    after = store.task(task_id, WORKSPACE)
    assert after.state is TaskState.WAITING_HUMAN
    assert after.paused_at is TaskState.CI_RUNNING
    for forbidden in (TaskState.MERGING, TaskState.DEPLOYING, TaskState.DONE,
                      TaskState.APPROVED):
        assert after.state is not forbidden

    approval = store.open_approvals(WORKSPACE)[0]
    assert "pull request #7" in approval.why_it_matters
    assert "PASSED" in approval.why_it_matters


def test_a_red_ci_asks_for_more_work_rather_than_declaring_failure(tmp_path, store):
    task_id = delivered(store)
    ci = FakeCI(status=PipelineStatus(
        reference=SHA, checks=(Check(name="build", conclusion="FAILURE",
                                     state="COMPLETED"),)))
    orq = watched(tmp_path, store, ci)
    orq.tick()

    approval = store.open_approvals(WORKSPACE)[0]
    assert approval.recommendation == "investigar"
    assert store.task(task_id, WORKSPACE).state is TaskState.WAITING_HUMAN


def test_ci_that_never_answers_eventually_reaches_a_person(tmp_path, store):
    """The anti-loop rule applied to waiting.

    A pipeline that never concludes is a real outcome. Polling it for ever is
    how it stays invisible, and 'still pending' on tick nine hundred is not
    more informative than it was on tick two.
    """
    task_id = delivered(store)
    ci = FakeCI(status=PipelineStatus(
        reference=SHA, checks=(Check(name="ci", conclusion="", state="QUEUED"),)))
    orq = watched(tmp_path, store, ci)
    orq.max_ci_observations = 3

    states = []
    for _ in range(5):
        orq.tick()
        states.append(store.task(task_id, WORKSPACE).state)

    assert states[0] is TaskState.CI_RUNNING
    assert TaskState.WAITING_HUMAN in states, (
        "the engine watched a pipeline that never concluded, for ever")
    approval = store.open_approvals(WORKSPACE)[0]
    assert "still" in approval.why_it_matters and "PENDING" in approval.why_it_matters


def test_ci_unavailable_forever_is_escalated_never_called_green(tmp_path, store):
    task_id = delivered(store)
    ci = FakeCI(error=AdapterError("the CI provider is unreachable"))
    orq = watched(tmp_path, store, ci)
    orq.max_ci_observations = 2

    for _ in range(3):
        orq.tick()

    after = store.task(task_id, WORKSPACE)
    assert after.state is TaskState.WAITING_HUMAN
    assert store.deliveries(WORKSPACE, "K-1")[0]["ci_state"] == "UNAVAILABLE"
    assert store.deliveries(WORKSPACE, "K-1")[0]["ci_result"] is None
    # Blocked, not sent back for more work: nothing is wrong with the change,
    # and asking an agent to fix an unreachable CI provider would be theatre.
    approval = store.open_approvals(WORKSPACE)[0]
    assert approval.recommendation == "bloquear"
    assert "unreachable" in approval.why_it_matters


def test_a_workspace_that_cannot_read_ci_says_so_instead_of_waiting(tmp_path, store):
    task_id = delivered(store)
    orq = watched(tmp_path, store, ci=None, deliveries=False)
    orq.tick()

    after = store.task(task_id, WORKSPACE)
    assert after.state is TaskState.WAITING_HUMAN
    assert "no way to read its checks" in store.open_approvals(
        WORKSPACE)[0].why_it_matters


def test_watching_ci_never_triggers_cancels_or_re_runs_anything(tmp_path, store):
    """Observation is L0. The adapter is asked one question and offered none."""
    delivered(store)
    ci = FakeCI(status=PipelineStatus(reference=SHA))
    orq = watched(tmp_path, store, ci)
    orq.tick()

    assert not hasattr(ci, "triggered")
    for forbidden in ("trigger", "rerun", "cancel", "dispatch", "approve"):
        assert not any(forbidden in name for name in dir(ci)), (
            f"the CI fake grew a '{forbidden}' surface; the engine must have "
            f"no way to reach one")


# ---------------------------------------------------------------------------
# Nothing permanent may carry a credential
# ---------------------------------------------------------------------------
#
# An HTTPS remote can hold the token inside the URL, and `git remote get-url`
# hands it back verbatim. Everything the delivery records outlives the process:
# the database goes into backups, bug reports and screenshots. Found by reading
# what the ledger stores, not by a failing test -- with a local clone, which is
# what every test used, the URL has no credential in it and the defect is
# invisible.

CREDENTIALLED = "https://x-access-token:ghp_notarealtoken000@github.com/acme/worker.git"


def test_a_credentialled_push_target_never_reaches_the_ledger(store):
    from regente.core.redaction import carries_credential

    task_id = a_task(store)
    areas = FakeAreas(target=CREDENTIALLED)
    ci = FakeCI(status=PipelineStatus(reference=SHA))
    s, _ = stage(store, areas=areas, cicd=ci)

    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")

    row = store.deliveries(WORKSPACE, "K-1")[0]
    assert not carries_credential(row["push_target"]), row["push_target"]
    assert "ghp_notarealtoken000" not in str(dict(row))
    assert not carries_credential(outcome.push_target)
    # The target is not simply dropped: a delivery whose destination is unknown
    # reads like a delivery that went nowhere.
    assert "github.com/acme/worker.git" in row["push_target"]


def test_no_row_and_no_event_in_the_whole_database_carries_one(store, tmp_path):
    """The strong form: read every byte the engine wrote, not the columns I
    happened to think of."""
    from regente.core.redaction import carries_credential

    task_id = a_task(store)
    areas = FakeAreas(target=CREDENTIALLED)
    ci = FakeCI(status=PipelineStatus(reference=SHA))
    s, _ = stage(store, areas=areas, cicd=ci)
    s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id, title="t")

    leaks = []
    for (table,) in store._con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in store._con.execute(f"SELECT * FROM {table}"):
            text = " ".join(str(v) for v in tuple(row) if v is not None)
            if carries_credential(text) or "ghp_notarealtoken000" in text:
                leaks.append(f"{table}: {text[:160]}")
    assert not leaks, "a credential was written to the database:\n" + "\n".join(leaks)


def test_a_push_error_quoting_the_remote_is_redacted_too(store):
    """`git` puts the URL in its own error messages, and refusals are recorded."""
    from regente.core.redaction import carries_credential

    task_id = a_task(store)

    class Failing(FakeAreas):
        def push(self, a, expected_sha, branch=None):
            raise AdapterError(f"fatal: unable to access '{CREDENTIALLED}'")

    s, _ = stage(store, areas=Failing(target=CREDENTIALLED))
    outcome = s.advance(identity(), area(), Verdict.READY_FOR_REVIEW, task_id,
                        title="t")

    assert outcome.step is Step.REFUSED
    assert not carries_credential(outcome.reason), outcome.reason
    assert "ghp_notarealtoken000" not in outcome.reason


def test_reconcile_reports_only_a_pull_request_this_run_can_claim(store):
    """`reconcile` answers "what already happened *for this run*".

    Its own contract, tested directly rather than through `push`. A stranger's
    pull request answered here would read as "no pull request needed" to any
    caller that trusts it -- and the mutation sweep found that nothing else
    would have noticed, because today's only caller happens to re-check later.
    """
    a_task(store)
    delivery = RemoteDelivery(
        store=store, areas=FakeAreas(),
        repos_write=FakeRepos(branch_sha=SHA,
                              existing=pull_request(number=3, body="by a person")),
        cicd=None, policy=PolicyEngine.from_config(RULES),
        autonomy=AutonomyLevel.L2)

    answer = delivery.reconcile(identity())

    assert answer.pushed is True
    assert answer.pull_request is None, (
        "reconcile claimed somebody else's pull request as this run's")
    assert answer.pull_request_needed


# ---------------------------------------------------------------------------
# A real corpse: killed between mutating the remote and recording it
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_a_worker_killed_after_pushing_does_not_cause_a_second_push(tmp_path):
    """The window that cannot be reached from inside one process.

    A real child process pushes, is killed from outside before it can write the
    row, and a second process picks the work up. The local state says nothing
    happened; the remote says something did. Only one of them is right, and the
    engine is not allowed to guess which.
    """
    import json
    import subprocess
    import sys
    import time

    root = tmp_path
    harness = str(Path(__file__).with_name("delivery_kill.py"))
    seeded = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, r'{Path(__file__).parent}'); "
         f"import delivery_kill as d; "
         f"print(d.seed(__import__('pathlib').Path(r'{root}')))"],
        capture_output=True, text=True, timeout=120)
    assert seeded.returncode == 0, seeded.stderr
    (root / "task.id").write_text(seeded.stdout.strip(), encoding="utf-8")

    victim = subprocess.Popen([sys.executable, harness, "push", str(root)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 60
        while not (root / "pushed.flag").exists() and time.time() < deadline:
            if victim.poll() is not None:
                raise AssertionError(
                    "the worker exited instead of pushing:\n"
                    + victim.communicate()[1].decode(errors="replace"))
            time.sleep(0.05)
        assert (root / "pushed.flag").exists(), "the worker never pushed"
    finally:
        victim.kill()                      # no finally, no cleanup, no row
        victim.wait(timeout=60)

    remote = json.loads((root / "remote.json").read_text(encoding="utf-8"))
    assert remote["pushes"] == 1 and remote["branch_sha"] == "c" * 40
    survivor = SqliteStore(root / "regente.db")
    try:
        assert not survivor.deliveries("wks_kill", "KILL-1")[0]["pushed_at"], (
            "the dead worker somehow recorded its push; the window this test "
            "exists for was not reached")
    finally:
        survivor.close()

    resumed = subprocess.run([sys.executable, harness, "resume", str(root)],
                             capture_output=True, text=True, timeout=300)
    assert resumed.returncode == 0, resumed.stderr
    answer = json.loads(resumed.stdout.strip().splitlines()[-1])

    after = json.loads((root / "remote.json").read_text(encoding="utf-8"))
    assert after["pushes"] == 1, (
        f"the resumed process pushed a commit that was already on the remote "
        f"({after['pushes']} pushes for one commit)")
    assert after["creations"] == 1, "more than one pull request for one commit"
    assert "push" in answer["resumed"]
    assert answer["pull_request"] == 1
