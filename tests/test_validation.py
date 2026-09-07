# -*- coding: utf-8 -*-
"""Promotion to VALIDATED, commit authority, and where history may be written.

Two separations are enforced here, and both exist because the obvious shortcut
is wrong in a way that produces no error:

    Agent  -> Outcome        (an observation)
    Engine -> Validation     (a conclusion)
    Policy -> Permission     (an authority)
    Engine -> Commit         (an act)

An agent reporting "ready to commit" is the first line, never the last.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from regente.adapters.workspace.local import GitClone
from regente.engine import coder, testing
from regente.engine import validation as val
from regente.engine.discovery import Discovery, Investigator, Source
from regente.engine.target import Confidence, Evidence
from regente.ports import AdapterError
from regente.ports.agent import TestResult, Verdict
from regente.ports.repository import READ_CAPS, Branch, RepoInfo, RepoRef
from regente.ports.tasks import ExternalStatus, ExternalTask


def _discovery(source=Source.DISCOVERED) -> Discovery:
    return Discovery(task_key="K-1", repo="acme/api", confidence=Confidence.OBSERVED,
                     source=source, evidence=(Evidence("branch", "names K-1"),),
                     strength=50)


def _loop(test_result=TestResult.PASSED, baseline_exit=1, files=("a.py",),
          baseline_ran=True, has_tests=True, verdict=Verdict.READY_FOR_REVIEW):
    base = testing.TestRun("pytest", baseline_exit, "before", 0.1, ran=baseline_ran)
    tv = (testing.TestVerdict(test_result, "reason", duration_s=0.1)
          if has_tests else None)
    return coder.LoopResult(verdict=verdict, reason="r", changed_files=tuple(files),
                            test_verdict=tv, baseline=base)


# ---------------------------------------------------------------------------
# Promotion: an independent rule, never "the agent managed to work there"
# ---------------------------------------------------------------------------

def test_promotion_requires_verification_to_have_happened():
    """Absence of verification is never success -- the same rule that governs
    PREEXISTING_FAILURE, applied to promotion."""
    assert not val.may_promote_target(_discovery(), _loop(has_tests=False))
    assert not val.may_promote_target(_discovery(), _loop(baseline_ran=False))


@pytest.mark.parametrize("result", [
    TestResult.PREEXISTING_FAILURE, TestResult.REGRESSION,
    TestResult.ENVIRONMENT_FAILURE, TestResult.INFRASTRUCTURE_FAILURE,
    TestResult.UNKNOWN,
])
def test_promotion_requires_the_tests_to_have_passed(result):
    assert not val.may_promote_target(_discovery(), _loop(test_result=result))


def test_a_green_baseline_cannot_confirm_a_target():
    """The sharp one: green over already-green proves the agent broke nothing,
    and nothing at all about whether this was the right repository."""
    judgement = val.may_promote_target(_discovery(), _loop(baseline_exit=0))
    assert not judgement.allowed
    assert "already green" in judgement.reason


def test_promotion_requires_a_change_to_attribute_the_green_to():
    assert not val.may_promote_target(_discovery(), _loop(files=()))


def test_a_declared_target_is_never_promoted():
    """Nothing was discovered, so there is nothing to confirm."""
    assert not val.may_promote_target(_discovery(Source.DECLARED), _loop())


def test_promotion_happens_when_red_became_green_after_a_change():
    judgement = val.may_promote_target(_discovery(), _loop())
    assert judgement.allowed
    assert "red to green" in judgement.reason


# ---------------------------------------------------------------------------
# Commit authority
# ---------------------------------------------------------------------------

def test_the_engine_does_not_commit_an_unverified_change():
    assert not val.may_commit(Verdict.READY_FOR_REVIEW, _loop(has_tests=False))


def test_the_engine_does_not_commit_a_regression():
    assert not val.may_commit(Verdict.READY_FOR_REVIEW,
                              _loop(test_result=TestResult.REGRESSION))


def test_the_engine_does_not_commit_nothing():
    assert not val.may_commit(Verdict.READY_FOR_REVIEW, _loop(files=()))


@pytest.mark.parametrize("verdict", [
    Verdict.BLOCKED, Verdict.NEEDS_HUMAN, Verdict.FAILED, Verdict.REGRESSED,
    Verdict.NO_CHANGE,
])
def test_the_engine_does_not_commit_on_a_stopping_verdict(verdict):
    assert not val.may_commit(verdict, _loop(verdict=verdict))


def test_the_engine_commits_a_green_change():
    assert val.may_commit(Verdict.READY_FOR_REVIEW, _loop())


def test_inherited_red_still_commits_and_says_so():
    judgement = val.may_commit(Verdict.READY_FOR_REVIEW,
                               _loop(test_result=TestResult.PREEXISTING_FAILURE))
    assert judgement.allowed
    assert "inherited" in judgement.reason


def test_the_agents_own_opinion_never_enters_the_commit_decision():
    """An agent reporting FINISHED -- even "ready to commit" -- is an
    observation, and observations do not authorise writes."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(val.may_commit).lstrip())
    # Structural, not textual: the docstring names `Outcome.FINISHED` precisely
    # to explain why it is absent, and a text search would trip over the
    # explanation instead of the code.
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "Outcome" not in names
    assert "FINISHED" not in attrs and "outcome" not in attrs


# ---------------------------------------------------------------------------
# Where history may be written
# ---------------------------------------------------------------------------

def _make_repo(root: Path, name: str = "src") -> Path:
    p = Path(root) / name
    p.mkdir(parents=True)
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=str(p), check=True, capture_output=True)
    (p / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(p), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(p), check=True,
                   capture_output=True)
    return p


def _git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          encoding="utf-8").stdout.strip()


@pytest.fixture
def area_and_source(tmp_path):
    source = _make_repo(tmp_path)
    areas = GitClone(root=tmp_path / "areas", sources={"acme/x": str(source)})
    area = areas.prepare("K-1", repo="acme/x", branch="regente/k-1", base="main")
    return areas, area, source


def test_the_workspace_refuses_to_commit_on_an_integration_branch(area_and_source,
                                                                 tmp_path):
    areas, area, _ = area_and_source
    subprocess.run(["git", "checkout", "-q", "main"], cwd=area.path, check=True,
                   capture_output=True)
    (Path(area.path) / "a.txt").write_text("two\n", encoding="utf-8")
    with pytest.raises(AdapterError, match="integration branch"):
        areas.commit(area, "should never land")


def test_the_workspace_refuses_a_branch_the_run_does_not_own(area_and_source):
    areas, area, _ = area_and_source
    subprocess.run(["git", "checkout", "-q", "-b", "someone-else"], cwd=area.path,
                   check=True, capture_output=True)
    (Path(area.path) / "a.txt").write_text("two\n", encoding="utf-8")
    with pytest.raises(AdapterError, match="the run owns"):
        areas.commit(area, "wrong branch")


def test_committing_nothing_is_an_error_not_a_silent_success(area_and_source):
    areas, area, _ = area_and_source
    with pytest.raises(AdapterError, match="nothing to commit"):
        areas.commit(area, "empty")


def test_a_commit_lands_only_on_the_isolated_branch(area_and_source):
    areas, area, source = area_and_source
    before = _git(source, "rev-parse", "main")
    (Path(area.path) / "a.txt").write_text("two\n", encoding="utf-8")
    sha = areas.commit(area, "K-1: change")

    assert len(sha) == 40
    assert _git(source, "rev-parse", "main") == before, "the source repository moved"
    assert _git(source, "branch", "--format=%(refname:short)").split() == ["main"], \
        "a branch leaked into the source"
    assert _git(area.path, "rev-parse", "main") == before, "the base branch moved"
    assert _git(area.path, "rev-parse", "HEAD") == sha


def test_the_clone_never_writes_into_the_source(area_and_source):
    """A worktree would have created a ref inside the source. A clone does not --
    which is why this milestone clones even though it costs more."""
    _, _, source = area_and_source
    assert len(_git(source, "worktree", "list").splitlines()) == 1
    assert _git(source, "status", "--porcelain") == ""


def test_the_commit_author_is_the_engine_not_a_person(area_and_source):
    """History must say who actually wrote it."""
    areas, area, _ = area_and_source
    (Path(area.path) / "a.txt").write_text("two\n", encoding="utf-8")
    areas.commit(area, "K-1: change")
    assert "Regente" in _git(area.path, "log", "-1", "--format=%an")


# ---------------------------------------------------------------------------
# The record must say what was examined, not only what won
# ---------------------------------------------------------------------------

class _NoRepos:
    name = "p"

    def read_file(self, key, path, ref=None):
        raise AdapterError("nothing here")


def _repo(key: str) -> RepoInfo:
    return RepoInfo(ref=RepoRef(provider="p", key=key), name=key.rsplit("/", 1)[-1],
                    base_branch="main", capabilities=READ_CAPS)


CATALOG = [_repo("acme/api"), _repo("acme/web")]
TASK = ExternalTask(key="K-1", title="do a thing", status=ExternalStatus.NOT_STARTED,
                    external_status="TO DO", project="P")


def test_an_examined_repository_with_no_evidence_is_still_recorded():
    """A record that lists nothing reads as though nothing was looked at."""
    d = Investigator(repos=_NoRepos()).investigate(
        TASK, CATALOG, branches={"acme/api": [Branch(name="feat/K-1-x")]})
    assert d.repo == "acme/api"
    assert "acme/web" in {r.repo for r in d.alternatives_considered}
    assert any("examined" in r.reason for r in d.alternatives_considered)


def test_the_record_pairs_every_alternative_with_a_reason():
    d = Investigator(repos=_NoRepos()).investigate(
        TASK, CATALOG, branches={"acme/api": [Branch(name="feat/K-1-x")]})
    payload = d.as_dict()
    assert payload["alternatives_considered"]
    assert len(payload["alternatives_considered"]) == len(payload["reason_rejected"])


def test_a_long_catalog_is_truncated_and_says_so():
    catalog = [_repo(f"acme/r{i}") for i in range(20)] + [_repo("acme/api")]
    d = Investigator(repos=_NoRepos()).investigate(
        TASK, catalog, branches={"acme/api": [Branch(name="feat/K-1-x")]})
    reasons = " ".join(r.reason for r in d.alternatives_considered)
    assert "truncated" in reasons
