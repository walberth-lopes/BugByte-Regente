# -*- coding: utf-8 -*-
"""Mission selection, the validation loop, and the two invariants.

The two invariants below are the ones the milestone adds, and both protect
against silent failure -- the kind that produces no error at all:

  1. No work without a resolved target backed by evidence. Code written into a
     repository chosen by guess succeeds completely, in the wrong place.
  2. No work on a task a person is already handling. An agent on top of a person
     is invisible from both sides until the collision surfaces in a diff.
"""

from __future__ import annotations

import pytest

from regente.adapters.runner.external import DeterministicAgent
from regente.core.policy import AutonomyLevel, Effect, PolicyEngine
from regente.core.risk import RiskEngine, RiskLevel
from regente.engine import coder, mission, testing
from regente.engine.discovery import Discovery, Investigator, Source
from regente.engine.mission import MissionPlanner, Refusal
from regente.engine.runner import InvariantViolated, MissionRunner
from regente.engine.target import Confidence, Evidence, TargetResolver
from regente.ports.agent import (Budget, Permissions, ProcessStatus, TestResult,
                                 Verdict)
from regente.ports.repository import (READ_CAPS, Branch, RepoCapability, RepoInfo,
                                      RepoRef, RepositoryProvider)
from regente.ports.tasks import BLOCKS, ExternalStatus, ExternalTask, TaskRef

RULES = [
    {"name": "code", "effect": "ALLOW", "match": {"action": "repo.branch*"}},
    {"name": "no_production", "effect": "DENY",
     "match": {"action": "repo.*", "environment": "production"}},
]


def repo(key: str, base: str = "main", archived: bool = False,
         caps=READ_CAPS) -> RepoInfo:
    return RepoInfo(ref=RepoRef(provider="p", key=key), name=key.rsplit("/", 1)[-1],
                    base_branch=base, archived=archived, capabilities=caps)


def task(key: str, status=ExternalStatus.NOT_STARTED, links=(), title="do a thing",
         labels=()) -> ExternalTask:
    return ExternalTask(key=key, title=title, status=status,
                        external_status="TO DO", links=tuple(links),
                        labels=tuple(labels), project="P")


class NoRepos(RepositoryProvider):
    """A provider that answers nothing. Discovery must cope without inventing."""
    name = "p"

    def list_repositories(self, filters=None):
        return []

    def get_repository(self, key):
        raise KeyError(key)


CATALOG = [repo("acme/api"), repo("acme/web")]


def planner(max_risk=RiskLevel.LOW, autonomy=AutonomyLevel.L2,
            by_task=None, environment="staging") -> MissionPlanner:
    return MissionPlanner(
        repos=NoRepos(),
        investigator=Investigator(repos=NoRepos(),
                                  resolver=TargetResolver(by_task=by_task or {})),
        policy=PolicyEngine.from_config(RULES), risk=RiskEngine(),
        autonomy=autonomy, workspace_name="ws", workspace_id="wks_1",
        environment=environment, max_risk=max_risk)


# ---------------------------------------------------------------------------
# Selection: every criterion is a veto, and every refusal names itself
# ---------------------------------------------------------------------------

def test_selects_the_only_qualifying_task():
    s = planner(by_task={"K-1": "acme/api"}).select([task("K-1")], CATALOG)
    assert s.mission is not None
    assert s.mission.repo.ref.key == "acme/api"
    assert s.mission.branch == "regente/k-1"
    assert s.mission.resource == "repo:wks_1/p/acme/api"


def test_refuses_when_the_origin_says_someone_is_on_it():
    s = planner(by_task={"K-1": "acme/api"}).select(
        [task("K-1", status=ExternalStatus.IN_PROGRESS)], CATALOG)
    assert s.refused
    assert s.rejections[0].refusal is Refusal.NO_WORK_AVAILABLE


def test_refuses_without_a_target():
    s = planner().select([task("K-1")], CATALOG)
    assert s.refused
    assert s.rejections[0].refusal is Refusal.TARGET_UNKNOWN


def test_refuses_on_an_ambiguous_target():
    s = planner().select(
        [task("K-1")], CATALOG,
        branches={"acme/api": [Branch(name="feat/K-1-a")],
                  "acme/web": [Branch(name="fix/K-1-b")]})
    assert s.refused
    assert s.rejections[0].refusal is Refusal.TARGET_AMBIGUOUS


def test_refuses_on_a_blocking_dependency():
    s = planner(by_task={"K-1": "acme/api"}).select(
        [task("K-1", links=[TaskRef(key="K-0", kind=BLOCKS)])], CATALOG)
    assert s.refused
    assert s.rejections[0].refusal is Refusal.BLOCKED_BY_DEPENDENCY


def test_a_completed_dependency_does_not_block():
    s = planner(by_task={"K-1": "acme/api"}).select(
        [task("K-1", links=[TaskRef(key="K-0", kind=BLOCKS)])], CATALOG,
        completed={"K-0"})
    assert s.mission is not None


def test_refuses_an_unusable_repository():
    s = planner(by_task={"K-1": "acme/api"}).select(
        [task("K-1")], [repo("acme/api", archived=True)])
    assert s.rejections[0].refusal is Refusal.REPO_UNUSABLE


def test_refuses_when_the_provider_lacks_a_capability():
    s = planner(by_task={"K-1": "acme/api"}).select(
        [task("K-1")], [repo("acme/api",
                             caps=frozenset({RepoCapability.READ_METADATA}))])
    assert s.rejections[0].refusal is Refusal.PROVIDER_CANNOT


def test_refuses_high_risk_work():
    s = planner(by_task={"K-1": "acme/api"}).select(
        [task("K-1", title="rotate the production IAM credentials")], CATALOG)
    assert s.rejections[0].refusal is Refusal.RISK_TOO_HIGH


def test_refuses_when_policy_denies():
    s = planner(by_task={"K-1": "acme/api"}, environment="production").select(
        [task("K-1")], CATALOG)
    assert s.rejections[0].refusal in (Refusal.POLICY_DENIED, Refusal.RISK_TOO_HIGH)


def test_refuses_when_the_ceiling_demands_a_human():
    s = planner(by_task={"K-1": "acme/api"}, autonomy=AutonomyLevel.L0).select(
        [task("K-1")], CATALOG)
    assert s.rejections[0].refusal is Refusal.NEEDS_HUMAN_APPROVAL


def test_refusal_is_a_first_class_outcome_not_an_error():
    """An engine that always finds something to do will eventually do the wrong
    thing, and the likeliest moment is when nothing safe is available."""
    s = planner().select([task("K-1"), task("K-2")], CATALOG)
    assert s.refused
    assert s.considered == 2
    assert len(s.rejections) == 2
    assert "REFUSED" in s.render()


def test_every_rejection_names_its_criterion():
    s = planner().select([task("K-1", status=ExternalStatus.IN_REVIEW), task("K-2")],
                         CATALOG)
    for r in s.rejections:
        assert isinstance(r.refusal, Refusal)
        assert r.detail


# ---------------------------------------------------------------------------
# The human-collision invariant: the same signal read two ways
# ---------------------------------------------------------------------------

def test_a_branch_ahead_of_base_means_a_person_is_inside():
    """The strongest target evidence is also evidence someone already started.

    Only the commits tell the two readings apart, so the engine looks.
    """
    s = planner().select(
        [task("K-1")], [repo("acme/api")],
        branches={"acme/api": [Branch(name="feat/K-1-wip")]},
        branch_is_ahead=lambda repo_key, branch: True)
    assert s.refused
    assert s.rejections[0].refusal is Refusal.HUMAN_WORK_IN_FLIGHT


def test_a_branch_level_with_base_does_not_block():
    s = planner().select(
        [task("K-1")], [repo("acme/api")],
        branches={"acme/api": [Branch(name="feat/K-1-wip")]},
        branch_is_ahead=lambda repo_key, branch: False)
    assert s.mission is not None
    assert s.mission.discovery.confidence is Confidence.OBSERVED


def test_not_being_able_to_check_is_not_permission_to_proceed():
    from regente.ports import AdapterError

    def explode(repo_key, branch):
        raise AdapterError("remote unreachable")

    s = planner().select(
        [task("K-1")], [repo("acme/api")],
        branches={"acme/api": [Branch(name="feat/K-1-wip")]},
        branch_is_ahead=explode)
    assert s.refused
    assert s.rejections[0].refusal is Refusal.HUMAN_WORK_IN_FLIGHT


# ---------------------------------------------------------------------------
# The invariants, enforced again at dispatch
# ---------------------------------------------------------------------------

def _runner(tmp_path, store, agent=None) -> MissionRunner:
    from regente.adapters.workspace.local import IsolatedDirectory
    return MissionRunner(
        store=store, workspace_id="wks_1", workspace_name="ws",
        repos=NoRepos(), areas=IsolatedDirectory(root=tmp_path / "areas"),
        agent=agent or DeterministicAgent(),
        planner=planner(by_task={"K-1": "acme/api"}),
        budget=Budget(max_iterations=2, max_seconds=60),
        permissions=Permissions(read=True, write_code=True, run_tests=True))


def _mission(confidence=Confidence.DECLARED, evidence=(Evidence("map", "declared"),),
             repo_key="acme/api", status=ExternalStatus.NOT_STARTED) -> mission.Mission:
    return mission.Mission(
        task=task("K-1", status=status), repo=repo(repo_key),
        discovery=Discovery(task_key="K-1", repo=repo_key, confidence=confidence,
                            source=Source.DECLARED, evidence=tuple(evidence)),
        risk=RiskEngine().assess({"action": "repo.branch"}),
        policy_effect=Effect.ALLOW, policy_rule="code",
        branch="regente/k-1", resource="repo:wks_1/p/acme/api")


@pytest.fixture
def store(tmp_path):
    from regente.engine.store_sqlite import SqliteStore
    s = SqliteStore(tmp_path / "t.db")
    s.migrate()
    return s


def test_invariant_no_work_without_evidence(tmp_path, store):
    r = _runner(tmp_path, store)
    with pytest.raises(InvariantViolated, match="no evidence"):
        r.run(mission.Selection(mission=_mission(evidence=())))


def test_invariant_no_work_on_an_unresolved_target(tmp_path, store):
    r = _runner(tmp_path, store)
    with pytest.raises(InvariantViolated, match="confidence"):
        r.run(mission.Selection(mission=_mission(confidence=Confidence.AMBIGUOUS)))


def test_invariant_target_must_match_the_mission_repository(tmp_path, store):
    r = _runner(tmp_path, store)
    m = _mission()
    wrong = mission.Mission(task=m.task, repo=repo("acme/web"), discovery=m.discovery,
                            risk=m.risk, policy_effect=m.policy_effect,
                            policy_rule=m.policy_rule, branch=m.branch,
                            resource=m.resource)
    with pytest.raises(InvariantViolated, match="does not match"):
        r.run(mission.Selection(mission=wrong))


def test_invariant_no_work_where_a_person_is_working(tmp_path, store):
    r = _runner(tmp_path, store)
    with pytest.raises(InvariantViolated, match="on top of a person"):
        r.run(mission.Selection(mission=_mission(status=ExternalStatus.IN_PROGRESS)))


def test_a_refused_selection_runs_nothing(tmp_path, store):
    r = _runner(tmp_path, store)
    out = r.run(mission.Selection(mission=None, rejections=(), considered=0))
    assert out.refused
    assert out.verdict is None
    assert not store.active_runs("wks_1")


# ---------------------------------------------------------------------------
# Test classification: two defects found by running against a real repository
# ---------------------------------------------------------------------------

def test_the_python_launcher_form_of_a_missing_module_is_an_environment_failure():
    """Found in the first real run, and it failed in the dangerous direction.

    `python -m pytest` without pytest prints neither `ModuleNotFoundError` nor
    `ImportError:` -- the launcher says "<interpreter>: No module named pytest".
    It fell through to the baseline comparison and came back
    PREEXISTING_FAILURE, which reads as "the change is fine, the red is
    inherited" when nothing had been verified at all.
    """
    after = testing.TestRun(
        "python -m pytest -q", 1,
        r"C:\Python313\python.exe: No module named pytest", 0.1)
    baseline = testing.TestRun("python -m pytest -q", 1, "same thing", 0.1)
    v = testing.classify(after, baseline)
    assert v.result is TestResult.ENVIRONMENT_FAILURE
    assert not v.allows_delivery, "an unverified run must not authorise delivery"


def test_the_detected_interpreter_is_not_bare_python(tmp_path):
    """`python` from PATH resolves to whatever comes first, which usually lacks
    the test dependencies -- and fails quietly, as a red test."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    command = testing.detect_command(tmp_path)
    assert command is not None
    assert command[0] != "python"
    assert command[0].endswith(("python", "python.exe"))


def test_a_repository_virtualenv_wins_over_the_engine_interpreter(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    venv = tmp_path / ".venv" / ("Scripts" if __import__("os").name == "nt" else "bin")
    venv.mkdir(parents=True)
    exe = venv / ("python.exe" if __import__("os").name == "nt" else "python")
    exe.write_text("", encoding="utf-8")
    assert testing.detect_command(tmp_path)[0] == str(exe)


def test_no_marker_file_means_no_guess(tmp_path):
    """Guessing `pytest` where nothing declares it produces an infrastructure
    failure the engine caused itself."""
    assert testing.detect_command(tmp_path) is None
