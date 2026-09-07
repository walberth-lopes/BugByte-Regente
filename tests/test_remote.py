# -*- coding: utf-8 -*-
"""Authority and identity on every remote mutation.

These are contract tests. They prove the guards hold; they do NOT prove the
capability works against a real provider. That distinction is the point of
`CAPABILITIES.md`, and nothing here should ever be cited as evidence that the
remote flow has been exercised.

The fakes below stand in for a hosted provider. They are deliberately obedient:
they do whatever they are told, so that every refusal in a test comes from the
engine's guards rather than from a fake declining to cooperate. A fake that
refuses would prove nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from regente.adapters.repos.readonly import (cli_is_allowed_write, cli_is_read)
from regente.core.policy import AutonomyLevel, PolicyEngine
from regente.engine import ci as ci_module
from regente.engine.remote import (PR_ACTION, PUSH_ACTION, Refused,
                                   RemoteDelivery, RemoteIdentity)
from regente.engine.store_sqlite import SqliteStore
from regente.ports import AdapterError
from regente.ports.agent import TestResult
from regente.ports.delivery import Check, PipelineStatus
from regente.ports.repository import (PullRequest, RepoRef, build_marker,
                                      read_marker)
from regente.ports.workspace import WorkArea

RULES = [
    {"name": "publish", "effect": "ALLOW",
     "match": {"action": ["repo.push*", "repo.pr.create"]}},
    {"name": "never_merge", "effect": "DENY", "match": {"action": "repo.merge*"}},
]

SHA = "a" * 40
OTHER_SHA = "b" * 40
REPO = RepoRef(provider="github", key="acme/api")


def identity(**kw) -> RemoteIdentity:
    base = dict(workspace_id="wks_1", workspace_name="ws", organization="org",
                client="cli", task_key="K-1", run_id="run_9", repo=REPO,
                branch="regente/k-1", commit_sha=SHA)
    return RemoteIdentity(**{**base, **kw})


def area(**kw) -> WorkArea:
    base = dict(id="K-1", path="/tmp/area", branch="regente/k-1", repo="acme/api")
    return WorkArea(**{**base, **kw})


@dataclass
class FakeAreas:
    """An obedient workspace. Every refusal in a test comes from the engine."""
    head_sha: str = SHA
    target: str | None = "https://github.com/acme/api.git"
    pushed: list[tuple] = field(default_factory=list)
    raise_on_push: Exception | None = None

    def head(self, a):
        return self.head_sha

    def push_target(self, a):
        return self.target

    def push(self, a, expected_sha, branch=None):
        if self.raise_on_push:
            raise self.raise_on_push
        self.pushed.append((a.path, branch, expected_sha))
        return self.head_sha


def pull_request(number=7, head=SHA, body="", author="regente",
                 branch="regente/k-1") -> PullRequest:
    return PullRequest(number=number, repo=REPO, title="t",
                       url=f"https://github.com/acme/api/pull/{number}",
                       state="OPEN", head_sha=head, branch=branch, base="main",
                       author=author, data={"body": body})


@dataclass
class FakeRepos:
    existing: PullRequest | None = None
    created: PullRequest | None = None
    calls: list[tuple] = field(default_factory=list)
    #: What the remote branch points at. `None` means the branch is not there,
    #: which is the ordinary state before a first push. The engine asks this
    #: before every push so a resumed run does not push twice.
    branch_sha: str | None = None

    def remote_branch_sha(self, repo, branch):
        return self.branch_sha

    def find_pull_request_for_branch(self, repo, branch):
        return self.existing

    def create_pull_request(self, repo, branch, base, title, body, marker):
        self.calls.append(("create", repo, branch, base, marker))
        return self.created or pull_request(body=f"{body}\n{marker}")


@dataclass
class FakeCI:
    status: PipelineStatus | None = None
    error: Exception | None = None
    asked: list[str] = field(default_factory=list)

    def get_status(self, repo, reference):
        self.asked.append(reference)
        if self.error:
            raise self.error
        return self.status or PipelineStatus(reference=reference)


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "t.db")
    s.migrate()
    return s


def delivery(store, areas=None, repos=None, cicd=None, rules=None,
             autonomy=AutonomyLevel.L2) -> RemoteDelivery:
    return RemoteDelivery(
        store=store, areas=areas or FakeAreas(), repos_write=repos or FakeRepos(),
        cicd=cicd, policy=PolicyEngine.from_config(rules if rules is not None else RULES),
        autonomy=autonomy)


def new_delivery_row(store) -> str:
    return store.open_delivery("wks_1", "K-1", "run_9", "github", "acme/api",
                               "regente/k-1", SHA)


# ---------------------------------------------------------------------------
# Push: policy, identity, SHA -- revalidated, not inherited
# ---------------------------------------------------------------------------

def test_push_happens_when_everything_lines_up(store):
    areas = FakeAreas()
    d = delivery(store, areas=areas)
    row = new_delivery_row(store)
    out = d.push(area(), identity(), row)

    assert out.sha == SHA
    assert out.target.startswith("https://")
    assert areas.pushed == [("/tmp/area", "regente/k-1", SHA)]
    assert store.deliveries("wks_1")[0]["pushed_at"]


def test_push_is_refused_when_policy_denies(store):
    d = delivery(store, rules=[{"name": "no", "effect": "DENY",
                                "match": {"action": "repo.push*"}}])
    with pytest.raises(Refused, match="DENY"):
        d.push(area(), identity(), new_delivery_row(store))


def test_push_is_refused_when_the_ceiling_is_too_low(store):
    d = delivery(store, autonomy=AutonomyLevel.L1)
    with pytest.raises(Refused, match="HUMAN_APPROVAL"):
        d.push(area(), identity(), new_delivery_row(store))


def test_push_is_refused_when_the_branch_moved(store):
    """Publishing then would vouch for a commit nobody validated."""
    d = delivery(store, areas=FakeAreas(head_sha=OTHER_SHA))
    with pytest.raises(Refused, match="branch moved"):
        d.push(area(), identity(), new_delivery_row(store))


def test_push_is_refused_when_the_area_holds_another_repository(store):
    d = delivery(store)
    with pytest.raises(Refused, match="identity drift"):
        d.push(area(repo="acme/other"), identity(), new_delivery_row(store))


def test_push_is_refused_when_the_area_is_on_another_branch(store):
    d = delivery(store)
    with pytest.raises(Refused, match="identity drift"):
        d.push(area(branch="someone-else"), identity(), new_delivery_row(store))


def test_push_is_refused_without_a_push_target(store):
    """Absence of configuration means impossible, not local."""
    d = delivery(store, areas=FakeAreas(target=None))
    with pytest.raises(Refused, match="no push target"):
        d.push(area(), identity(), new_delivery_row(store))


def test_a_workspace_refusal_surfaces_as_a_refusal(store):
    d = delivery(store, areas=FakeAreas(
        raise_on_push=AdapterError("refused to push to a local path")))
    with pytest.raises(Refused, match="local path"):
        d.push(area(), identity(), new_delivery_row(store))


# ---------------------------------------------------------------------------
# Pull request: the same three guards, then identity of the result
# ---------------------------------------------------------------------------

def test_a_pull_request_carries_the_run_marker(store):
    repos = FakeRepos()
    d = delivery(store, repos=repos)
    row = new_delivery_row(store)
    pr = d.open_pull_request(area(), identity(), row, base="main",
                             title="t", body="b")

    marker = repos.calls[0][-1]
    assert read_marker(marker) == {"workspace_id": "wks_1", "task_key": "K-1",
                                   "run_id": "run_9"}
    stored = store.deliveries("wks_1")[0]
    assert stored["pr_number"] == pr.number
    assert stored["pr_head_sha"] == SHA


def test_a_pull_request_is_refused_when_policy_denies(store):
    d = delivery(store, rules=[{"name": "no", "effect": "DENY",
                                "match": {"action": "repo.pr.create"}}])
    with pytest.raises(Refused, match="DENY"):
        d.open_pull_request(area(), identity(), new_delivery_row(store),
                            base="main", title="t", body="b")


def test_a_pull_request_is_refused_when_the_branch_moved(store):
    d = delivery(store, areas=FakeAreas(head_sha=OTHER_SHA))
    with pytest.raises(Refused, match="branch moved"):
        d.open_pull_request(area(), identity(), new_delivery_row(store),
                            base="main", title="t", body="b")


def test_a_pull_request_opened_at_another_head_is_refused(store):
    """The number binds nothing. Only the head does."""
    d = delivery(store, repos=FakeRepos(created=pull_request(head=OTHER_SHA)))
    with pytest.raises(Refused, match="binding cannot be trusted"):
        d.open_pull_request(area(), identity(), new_delivery_row(store),
                            base="main", title="t", body="b")


# ---------------------------------------------------------------------------
# Somebody else's pull request
# ---------------------------------------------------------------------------

def test_an_unmarked_pull_request_is_never_adopted(store):
    """A person opened it. The engine cannot know their intent."""
    d = delivery(store, repos=FakeRepos(
        existing=pull_request(number=3, body="I started this yesterday",
                              author="a-human")))
    with pytest.raises(Refused, match="no run marker"):
        d.open_pull_request(area(), identity(), new_delivery_row(store),
                            base="main", title="t", body="b")


def test_a_pull_request_from_another_workspace_is_never_adopted(store):
    other = build_marker("wks_OTHER", "K-1", "run_9")
    d = delivery(store, repos=FakeRepos(existing=pull_request(body=other)))
    with pytest.raises(Refused, match="belongs to workspace"):
        d.open_pull_request(area(), identity(), new_delivery_row(store),
                            base="main", title="t", body="b")


def test_a_pull_request_for_another_task_is_never_adopted(store):
    other = build_marker("wks_1", "K-OTHER", "run_9")
    d = delivery(store, repos=FakeRepos(existing=pull_request(body=other)))
    with pytest.raises(Refused, match="task 'K-OTHER'"):
        d.open_pull_request(area(), identity(), new_delivery_row(store),
                            base="main", title="t", body="b")


def test_a_marker_alone_is_not_enough_when_the_head_disagrees(store):
    """Both proofs, or neither. A marker can be copied into a body."""
    mine = build_marker("wks_1", "K-1", "run_9")
    d = delivery(store, repos=FakeRepos(
        existing=pull_request(body=mine, head=OTHER_SHA)))
    with pytest.raises(Refused, match="adoption would be a guess"):
        d.open_pull_request(area(), identity(), new_delivery_row(store),
                            base="main", title="t", body="b")


def test_a_pull_request_from_another_run_is_not_adopted(store):
    other = build_marker("wks_1", "K-1", "run_OTHER")
    d = delivery(store, repos=FakeRepos(existing=pull_request(body=other)))
    with pytest.raises(Refused, match="belongs to run 'run_OTHER'"):
        d.open_pull_request(area(), identity(), new_delivery_row(store),
                            base="main", title="t", body="b")


def test_this_runs_own_pull_request_is_recognised(store):
    mine = build_marker("wks_1", "K-1", "run_9")
    existing = pull_request(body=mine, head=SHA)
    d = delivery(store, repos=FakeRepos(existing=existing))
    got = d.open_pull_request(area(), identity(), new_delivery_row(store),
                              base="main", title="t", body="b")
    assert got.number == existing.number


# ---------------------------------------------------------------------------
# Isolation: two workspaces, one repository
# ---------------------------------------------------------------------------

def test_two_workspaces_cannot_share_one_pull_request(store):
    store.open_delivery("wks_a", "K-1", "run_1", "github", "acme/api", "b", SHA)
    a = store.deliveries("wks_a")[0]["id"]
    store.record_pull_request(a, 7, "url", SHA)

    b = store.open_delivery("wks_b", "K-1", "run_2", "github", "acme/api", "b", SHA)
    # A different workspace may hold the same PR number without colliding: the
    # uniqueness is per workspace, which is what tenancy means here.
    store.record_pull_request(b, 7, "url", SHA)
    assert store.delivery_for_pr("wks_a", "github", "acme/api", 7)["run_id"] == "run_1"
    assert store.delivery_for_pr("wks_b", "github", "acme/api", 7)["run_id"] == "run_2"


def test_one_workspace_cannot_bind_two_deliveries_to_one_pull_request(store):
    import sqlite3
    a = store.open_delivery("wks_a", "K-1", "run_1", "github", "acme/api", "b", SHA)
    store.record_pull_request(a, 7, "url", SHA)
    b = store.open_delivery("wks_a", "K-2", "run_2", "github", "acme/api", "b", SHA)
    with pytest.raises(sqlite3.IntegrityError):
        store.record_pull_request(b, 7, "url", SHA)


# ---------------------------------------------------------------------------
# CI observation: three conditions that are not results
# ---------------------------------------------------------------------------

def check(name, conclusion="SUCCESS", state="COMPLETED") -> Check:
    return Check(name=name, conclusion=conclusion, state=state)


def test_pending_checks_are_not_a_result(store):
    ci = FakeCI(status=PipelineStatus(
        reference=SHA, checks=(check("ci", "", "IN_PROGRESS"),)))
    obs = delivery(store, cicd=ci).observe_ci(identity(), new_delivery_row(store))
    assert obs.state is ci_module.CIState.PENDING
    assert obs.result is None
    assert not obs.allows_progress


def test_an_unreadable_provider_is_not_a_result(store):
    ci = FakeCI(error=AdapterError("502 from the provider"))
    obs = delivery(store, cicd=ci).observe_ci(identity(), new_delivery_row(store))
    assert obs.state is ci_module.CIState.UNAVAILABLE
    assert obs.result is None
    assert not obs.allows_progress, "unavailability must never read as green"


def test_no_ci_provider_is_unavailability_not_success(store):
    obs = delivery(store, cicd=None).observe_ci(identity(), new_delivery_row(store))
    assert obs.state is ci_module.CIState.UNAVAILABLE


def test_confirmed_absence_of_checks_is_recorded_and_proves_nothing(store):
    ci = FakeCI(status=PipelineStatus(reference=SHA, confirmed_no_checks=True))
    obs = delivery(store, cicd=ci).observe_ci(identity(), new_delivery_row(store))
    assert obs.state is ci_module.CIState.NO_CHECKS
    assert obs.result is None
    assert obs.allows_progress, "there is nothing left to wait for"
    assert "nothing was verified" in obs.reason


def test_ci_is_asked_about_the_commit_not_the_pull_request(store):
    """A PR's head moves; a SHA cannot drift."""
    ci = FakeCI(status=PipelineStatus(reference=SHA, checks=(check("ci"),)))
    delivery(store, cicd=ci).observe_ci(identity(), new_delivery_row(store))
    assert ci.asked == [SHA]


def test_all_green_is_passed(store):
    ci = FakeCI(status=PipelineStatus(reference=SHA,
                                      checks=(check("ci"), check("lint"))))
    obs = delivery(store, cicd=ci).observe_ci(identity(), new_delivery_row(store))
    assert obs.result is TestResult.PASSED
    assert obs.allows_progress


def test_a_red_check_without_a_baseline_is_unknown(store):
    ci = FakeCI(status=PipelineStatus(reference=SHA,
                                      checks=(check("ci", "FAILURE"),)))
    obs = delivery(store, cicd=ci).observe_ci(identity(), new_delivery_row(store))
    assert obs.result is TestResult.UNKNOWN
    assert not obs.allows_progress


def test_the_ci_observation_is_persisted(store):
    ci = FakeCI(status=PipelineStatus(reference=SHA, checks=(check("ci"),)))
    row = new_delivery_row(store)
    delivery(store, cicd=ci).observe_ci(identity(), row)
    stored = store.deliveries("wks_1")[0]
    assert stored["ci_state"] == "CONCLUDED"
    assert stored["ci_result"] == "PASSED"
    assert stored["ci_checks"] == [{"name": "ci", "status": "green"}]


# ---------------------------------------------------------------------------
# CI classification, in isolation
# ---------------------------------------------------------------------------

def test_a_regression_needs_a_baseline_that_was_green():
    after = PipelineStatus(reference=SHA, checks=(check("ci", "FAILURE"),))
    base = PipelineStatus(reference=OTHER_SHA, checks=(check("ci", "SUCCESS"),))
    obs = ci_module.classify(after, base)
    assert obs.result is TestResult.REGRESSION
    assert obs.regressions == ("ci",)
    assert obs.condemns_the_change


def test_a_check_already_red_on_the_base_is_inherited():
    after = PipelineStatus(reference=SHA, checks=(check("ci", "FAILURE"),))
    base = PipelineStatus(reference=OTHER_SHA, checks=(check("ci", "FAILURE"),))
    obs = ci_module.classify(after, base)
    assert obs.result is TestResult.PREEXISTING_FAILURE
    assert obs.allows_progress
    assert not obs.condemns_the_change


def test_an_environment_check_failure_is_not_the_changes_fault():
    after = PipelineStatus(reference=SHA, checks=(check("setup-python", "FAILURE"),))
    obs = ci_module.classify(after)
    assert obs.result is TestResult.ENVIRONMENT_FAILURE


def test_skipped_and_neutral_count_as_green():
    after = PipelineStatus(reference=SHA, checks=(check("a", "SKIPPED"),
                                                  check("b", "NEUTRAL")))
    assert ci_module.classify(after).result is TestResult.PASSED


def test_the_wait_policy_ends_pending_and_unavailability_separately():
    policy = ci_module.WaitPolicy(max_pending_seconds=10, max_unavailable_attempts=2)
    assert "pending" in policy.exhausted(11, 0)
    assert "unreadable" in policy.exhausted(0, 2)
    assert policy.exhausted(1, 1) == ""


# ---------------------------------------------------------------------------
# The write gate: authority is never inherited from a verb
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("invocation", [
    ["pr", "merge", "1"], ["pr", "review", "1", "--approve"], ["pr", "close", "1"],
    ["pr", "edit", "1"], ["pr", "reopen", "1"], ["pr", "comment", "1"],
    ["pr", "ready", "1"], ["repo", "delete", "x"], ["repo", "edit", "x"],
    ["workflow", "run"], ["run", "rerun", "1"], ["run", "cancel", "1"],
    ["secret", "set", "X"], ["release", "create", "v1"],
])
def test_the_write_gate_refuses_everything_but_opening_a_pull_request(invocation):
    ok, reason = cli_is_allowed_write(invocation)
    assert not ok, f"'{' '.join(invocation)}' got through the write gate"
    assert reason


@pytest.mark.parametrize("invocation", [
    ["pr", "create", "--web"], ["pr", "create", "--fill"],
    ["pr", "create", "--fill-first"],
])
def test_a_permitted_write_can_still_carry_a_forbidden_shape(invocation):
    assert not cli_is_allowed_write(invocation)[0]


def test_the_read_gate_never_opens_the_write_one():
    """Two lists, so widening reads can never grant writes."""
    assert not cli_is_read(["pr", "create"])[0]
    assert cli_is_allowed_write(["pr", "create"])[0]


def test_the_read_gate_still_refuses_every_write():
    for invocation in (["pr", "create"], ["pr", "merge", "1"], ["repo", "delete", "x"]):
        assert not cli_is_read(invocation)[0]


# ---------------------------------------------------------------------------
# The agent holds none of this
# ---------------------------------------------------------------------------

def test_the_agent_has_no_push_or_pull_request_capability():
    from regente.ports.agent import Permissions
    default = Permissions()
    assert not default.push
    assert not default.open_pr


def test_remote_delivery_never_consults_the_agents_outcome():
    """Structural: producing a commit does not entitle an agent to publish it."""
    import ast
    import inspect
    from regente.engine import remote
    tree = ast.parse(inspect.getsource(remote))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "Outcome" not in names
    assert "outcome" not in attrs


def test_the_policy_actions_are_distinct_authorities():
    """One name could not express 'may push but may not open a pull request'."""
    assert PUSH_ACTION != PR_ACTION
    from regente.core.policy import required_level, AutonomyLevel
    assert required_level(PUSH_ACTION) is AutonomyLevel.L2
    assert required_level(PR_ACTION) is AutonomyLevel.L2
    assert required_level("repo.merge") is AutonomyLevel.L3
    assert required_level("repo.review") is AutonomyLevel.L3


def test_without_a_write_provider_a_pull_request_is_impossible(store):
    """Fail-closed. Absence of configuration is not permission to improvise."""
    d = RemoteDelivery(store=store, areas=FakeAreas(), repos_write=None, cicd=None,
                       policy=PolicyEngine.from_config(RULES),
                       autonomy=AutonomyLevel.L2)
    with pytest.raises(Refused, match="no pull-request provider"):
        d.open_pull_request(area(), identity(), new_delivery_row(store),
                            base="main", title="t", body="b")


def test_the_composition_root_builds_delivery_without_a_write_provider(tmp_path):
    '''A workspace that only reads still assembles -- and still cannot publish.'''
    from regente.app.config import load
    from regente.app.container import build

    (tmp_path / "tasks").mkdir()
    (tmp_path / "regente.yaml").write_text(f'''
organization: org
client: cli
workspace: ws
autonomy: L1
root: {(tmp_path / '.regente').as_posix()}
providers:
  tasks:
    name: filesystem
    directory: {(tmp_path / 'tasks').as_posix()}
  workspace_provider:
    name: directory
  runner:
    name: script
''', encoding="utf-8")

    engine = build(load(tmp_path / "regente.yaml"))
    try:
        assert engine.delivery is not None
        assert engine.delivery.repos_write is None, "no write adapter was configured"
        assert engine.delivery.cicd is None
        row = engine.store.open_delivery(
            engine.workspace.id, "K-1", "run_1", "github", "acme/api",
            "regente/k-1", SHA)
        with pytest.raises(Refused):
            engine.delivery.open_pull_request(
                area(), identity(), row, base="main", title="t", body="b")
    finally:
        engine.close()
