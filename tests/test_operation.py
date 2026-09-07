# -*- coding: utf-8 -*-
"""Continuous operation: time, faults, and coming back from the dead.

The premise being tested is the one the whole project rests on -- that the
engine keeps working without somebody watching it. Every other milestone proved
a capability in a single supervised invocation. This one asks what happens on
the fourth day.

The two most dangerous defects this project has produced were both invisible to
single-shot tests: a daily counter written under one name and read under
another, so the budget never applied; and a verifier that tested the previous
revision. Neither could fail a test that ran once. So the tests here run many
times, cross day boundaries, kill real processes, and check invariants after
every step rather than at the end -- a run that only checks its final state
cannot tell a system that stayed correct from one that broke and healed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from faults import DyingRunner, FlakyTasks, Schedule
from soak import Clock, Soak, check_invariants

from regente.core.model import RunState
from regente.core.states import ACTIVE, TaskState, is_terminus
from regente.engine import health as health_module
from regente.engine.health import Level
from regente.engine.store_sqlite import SqliteStore


def build(tmp_path, name: str = "world", **kw) -> Soak:
    """A world with three tasks on the board, ready to tick.

    Built by a function rather than only a fixture because several tests need a
    different ceiling, and `Limits` is frozen -- it has to be right before the
    engine is assembled, not patched afterwards.
    """
    s = Soak(root=tmp_path / name, **kw)
    s.open()
    for i in range(1, 4):
        s.write_task(f"OP-{i}", resources=(f"repo:r{i}",))
    return s


@pytest.fixture
def soak(tmp_path) -> Soak:
    s = build(tmp_path)
    yield s
    s.close()


# ---------------------------------------------------------------------------
# The day boundary
# ---------------------------------------------------------------------------

def test_the_daily_budget_resets_at_the_day_boundary_and_not_before(tmp_path):
    """Spend it, cross midnight, spend again -- and never reset the day before.

    Deterministic on purpose. A test that waited for real midnight is a test
    nobody runs, and a test that reset the counter by hand would be asserting
    on its own arithmetic.
    """
    soak = build(tmp_path, "budget", max_dispatches=2)
    day_one = soak.clock.at.strftime("%Y-%m-%d")
    for n in range(1, 6):
        soak.tick(n)
        soak.clock.advance(minutes=5)

    spent = soak.store.dispatch_count(soak.workspace_id, day_one)
    assert spent == 2, "the ceiling held within the day"

    # Cross into the next day. The previous day's count must survive untouched:
    # a reset that reached backwards would let a workspace spend a day twice.
    soak.clock.at = soak.clock.at.replace(hour=0, minute=1) + timedelta(days=1)
    day_two = soak.clock.at.strftime("%Y-%m-%d")
    assert day_two != day_one

    assert soak.store.dispatch_count(soak.workspace_id, day_two) == 0
    assert soak.store.dispatch_count(soak.workspace_id, day_one) == spent

    soak.decide_everything()
    for n in range(6, 11):
        soak.tick(n)
        soak.clock.advance(minutes=5)

    assert soak.store.dispatch_count(soak.workspace_id, day_two) > 0, (
        "the new day must have its own budget")
    assert soak.store.dispatch_count(soak.workspace_id, day_one) == spent, (
        "yesterday's spend was rewritten")


def test_a_budget_is_never_reset_by_merely_restarting(tmp_path):
    """Restarting must not hand back a day's spend.

    The counter lives on disk precisely so a crash loop cannot buy more
    dispatches by dying repeatedly.
    """
    soak = build(tmp_path, "restart", max_dispatches=2)
    day = soak.clock.at.strftime("%Y-%m-%d")

    for n in range(1, 4):
        soak.tick(n)
        soak.clock.advance(minutes=1)
    spent = soak.store.dispatch_count(soak.workspace_id, day)
    assert spent > 0

    for _ in range(3):
        soak.crash()
        assert soak.store.dispatch_count(soak.workspace_id, day) == spent
    soak.close()


# ---------------------------------------------------------------------------
# Death and recovery
# ---------------------------------------------------------------------------

def test_a_dead_worker_is_detected_by_its_lease_and_the_task_returns(tmp_path):
    """Proof of death is the expired lease, never the absence of a process.

    'I cannot see the worker, so it must have finished' is a test that works by
    accident on one machine and fails on the day the engine runs on two.
    """
    s = Soak(root=tmp_path / "w", lease_seconds=60,
             schedules={"worker_dies": Schedule(kind="error", at=frozenset({1}),
                                                reason="killed")})
    s.open()
    s.write_task("DEAD-1", resources=("repo:a",))
    try:
        s.tick(1)   # discovery
        s.clock.advance(seconds=30)
        s.tick(2)   # dispatch; the worker dies here

        running = s.store.active_runs(s.workspace_id)
        assert running, "the dead worker left its run marked RUNNING"
        assert s.store.leases(s.workspace_id), "and its lease still held"

        # Before the lease expires, nothing may be concluded.
        s.clock.advance(seconds=10)
        early = s.tick(3)
        assert early.recovered == 0, "a live lease is not proof of death"

        # After it expires, recovery returns the work to the queue.
        s.clock.advance(seconds=120)
        late = s.tick(4)
        assert late.recovered == 1

        run = s.store.runs_in_state(s.workspace_id, RunState.INTERRUPTED.value)
        assert run and "lease" in run[0].reason

        # Asserted on the recorded recovery rather than on the task's state
        # afterwards: the same tick legitimately dispatches the recovered task
        # again, so reading the state at the end sees the NEXT thing that
        # happened and would fail while the engine is behaving correctly.
        recovered = [e for e in s.store.events(s.workspace_id, limit=200)
                     if e.kind == "recuperada"]
        assert recovered, "the recovery must be on the record"
        assert "READY" in recovered[0].summary, (
            "the task must go back where the scheduler can pick it up")
    finally:
        s.close()


def test_state_survives_a_real_kill_and_the_next_process_finds_it(tmp_path):
    """A real child process, really killed, and a fresh engine reading the disk.

    Simulating this in-process would prove that objects can be dropped. What has
    to be proved is that the ROWS are enough: a second interpreter, sharing
    nothing but the file, has to work out what was left behind.
    """
    root = tmp_path / "killed"
    root.mkdir()
    database = root / "regente.db"

    worker = tmp_path / "worker.py"
    worker.write_text(textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from datetime import datetime, timezone
        from regente.core.model import Run, RunState, Workspace
        from regente.core.policy import AutonomyLevel
        from regente.engine.store_sqlite import SqliteStore

        store = SqliteStore({str(database)!r})
        store.migrate()
        store.save_workspace(Workspace(id="wks_k", client_id="c", name="k",
                                       max_autonomy=AutonomyLevel.L2))
        run = Run(id="run_killed", task_id="tsk_x", workspace_id="wks_k",
                  agent="coder", state=RunState.RUNNING)
        store.save_run(run)
        store.acquire_lease("repo:a", "run_killed", "wks_k", 1)
        print("READY", flush=True)
        time.sleep(300)
    """), encoding="utf-8")

    process = subprocess.Popen([sys.executable, str(worker)],
                               stdout=subprocess.PIPE, encoding="utf-8")
    try:
        assert process.stdout.readline().strip() == "READY"
    finally:
        process.kill()          # SIGKILL: no cleanup, no flush, no goodbye
        process.wait(timeout=30)

    assert process.returncode != 0 or process.poll() is not None

    # A brand new process. Nothing carried over but the file.
    store = SqliteStore(database)
    store.migrate()
    try:
        runs = store.active_runs("wks_k")
        assert [r.id for r in runs] == ["run_killed"], (
            "the killed process's run must still be on disk")

        later = datetime.now(timezone.utc) + timedelta(seconds=120)
        assert [l.resource for l in store.expired_leases("wks_k", when=later)] \
            == ["repo:a"]

        report = health_module.inspect(store, "wks_k", "k", when=later)
        running = report.of("running")
        assert running.level is Level.UNKNOWN, (
            "a RUNNING row with no live lease is not knowledge that it finished")
        assert "not known" in running.detail
        assert report.of("expired_leases").level is not Level.OK
    finally:
        store.close()


def test_a_crash_between_the_write_and_the_confirmation_leaves_a_readable_world(
        tmp_path):
    """The transition committed; the process died before acting on it.

    The engine must come back and see the committed state, not a half-written
    one. SQLite's transaction is what makes this true, and the test exists to
    keep it true.
    """
    from faults import HalfWrittenStore

    s = Soak(root=tmp_path / "half")
    s.open()
    s.write_task("HALF-1", resources=("repo:a",))
    s.tick(1)                       # baseline: discovery only
    s.clock.advance(minutes=5)

    # The crash is armed on the tick that actually dispatches, because
    # `mark_dispatch` is only reached there. Arming it on the baseline tick
    # would have proved nothing and passed.
    broken = HalfWrittenStore(inner=s.store, method="mark_dispatch")
    s.orchestrator.store = broken
    outcome = s.orchestrator.tick()

    # The tick survives one worker's crash on purpose -- a single failure must
    # not take the whole cycle down -- but it must SAY so. A crash that vanished
    # into a clean tick is the failure mode this milestone is about.
    assert broken.fired, "the injected crash never happened"
    assert outcome.errors, "the crash must be reported on the tick"
    assert any("injected crash" in e for e in outcome.errors)
    assert any(e.kind == "error" for e in s.store.events(s.workspace_id, limit=50))

    day = s.clock().strftime("%Y-%m-%d")
    committed = s.store.dispatch_count(s.workspace_id, day)
    s.crash()
    assert s.store.dispatch_count(s.workspace_id, day) == committed, (
        "what was committed before the crash must still be there, exactly")
    assert not check_invariants(s.store, s.workspace_id, s.clock(), s.areas)
    s.close()


# ---------------------------------------------------------------------------
# Providers behaving badly
# ---------------------------------------------------------------------------

def test_an_unreachable_board_is_never_read_as_an_empty_board(tmp_path):
    """The most expensive possible misreading of an outage.

    An empty board would mean every task vanished, and an engine that believed
    it would cancel work that is perfectly fine.
    """
    s = Soak(root=tmp_path / "outage",
             schedules={"tasks": Schedule(kind="unavailable",
                                          at=frozenset({2, 3}),
                                          reason="board down")})
    s.open()
    for i in range(1, 4):
        s.write_task(f"OUT-{i}")
    try:
        s.tick(1)
        before = {t.key for t in s.store.tasks(s.workspace_id)}
        assert before

        s.clock.advance(minutes=5)
        outage = s.tick(2)
        assert outage.errors == 1, "the outage must be recorded as an error"

        after = {t.key for t in s.store.tasks(s.workspace_id)}
        assert after == before, "an outage must not delete or cancel anything"
        for task in s.store.tasks(s.workspace_id):
            assert task.state is not TaskState.CANCELLED
    finally:
        s.close()


def test_repeated_provider_failure_is_reported_rather_than_absorbed(tmp_path):
    s = Soak(root=tmp_path / "flaky",
             schedules={"tasks": Schedule(kind="rate_limit", every=1,
                                          reason="always limited")})
    s.open()
    s.write_task("RL-1")
    try:
        for n in range(1, 6):
            s.tick(n)
            s.clock.advance(minutes=5)
        report = health_module.inspect(s.store, s.workspace_id, "flaky",
                                       when=s.clock())
        failures = report.of("provider_failures")
        assert failures.level is not Level.OK
        assert "provider failure" in failures.detail
    finally:
        s.close()


# ---------------------------------------------------------------------------
# The queue keeps moving
# ---------------------------------------------------------------------------

def test_the_engine_never_parks_a_task_where_no_tick_can_reach_it(soak):
    """The defect a soak run found on its fourth tick.

    Every successfully dispatched task used to stop in TESTING: an active state
    the scheduler skips and nothing advances. The queue looked busy, the ticks
    came back clean, and the work was never touched again.
    """
    for n in range(1, 6):
        soak.tick(n)
        soak.clock.advance(minutes=5)

    for task in soak.store.tasks(soak.workspace_id):
        assert not is_terminus(task.state), (
            f"{task.key} is parked in {task.state.value}, which no tick advances")


def test_a_decision_actually_moves_the_task(soak):
    """The return path from escalation, which was a one-way door.

    `regente decide` recorded the choice and printed that the next tick would
    resume the task. No tick read it back. Everything that escalated stayed
    escalated, and the CLI said otherwise.
    """
    for n in range(1, 5):
        soak.tick(n)
        soak.clock.advance(minutes=5)

    waiting = [t for t in soak.store.tasks(soak.workspace_id)
               if t.state is TaskState.WAITING_HUMAN]
    assert waiting, "something should be waiting for a person by now"

    assert soak.decide_everything(choice="investigar") == len(waiting)
    assert not soak.store.open_approvals(soak.workspace_id)

    resumed = soak.tick(99)
    applied = [e for e in soak.store.events(soak.workspace_id, limit=400)
               if e.kind == "decisao_aplicada"]
    assert len(applied) == len(waiting), (
        "every decision must be acted on by the next tick")
    assert all("investigar" in e.summary for e in applied)

    # The tick then dispatched them again, which is the point: the queue moved.
    assert resumed.dispatched == len(waiting)


@pytest.mark.parametrize("choice,expected", [
    ("cancelar", TaskState.CANCELLED),
    ("bloquear", TaskState.BLOCKED),
])
def test_every_decision_option_has_a_destination(soak, choice, expected):
    for n in range(1, 5):
        soak.tick(n)
        soak.clock.advance(minutes=5)
    waiting = [t for t in soak.store.tasks(soak.workspace_id)
               if t.state is TaskState.WAITING_HUMAN]
    assert waiting

    soak.decide_everything(choice=choice)
    soak.tick(50)
    states = {t.key: t.state for t in soak.store.tasks(soak.workspace_id)}
    assert any(states[t.key] is expected for t in waiting)


def test_an_unrecognised_decision_still_moves_the_task(soak):
    """A choice the engine cannot parse must not silently stop the queue."""
    for n in range(1, 5):
        soak.tick(n)
        soak.clock.advance(minutes=5)
    waiting = [t for t in soak.store.tasks(soak.workspace_id)
               if t.state is TaskState.WAITING_HUMAN]
    assert waiting

    for approval in soak.store.open_approvals(soak.workspace_id):
        # Options are validated by the store, so reach past it to produce the
        # condition a future option would produce.
        soak.store.decide_approval(approval.id, approval.options[0].id,
                                   per="tester")
        soak.store._con.execute("UPDATE approvals SET choice='algo_novo' WHERE id=?",
                                (approval.id,))
    soak.tick(60)
    for task in waiting:
        current = soak.store.task(task.id)
        assert current.state is not TaskState.WAITING_HUMAN


# ---------------------------------------------------------------------------
# Health answers from the disk
# ---------------------------------------------------------------------------

def test_health_answers_after_the_engine_is_gone(soak):
    for n in range(1, 4):
        soak.tick(n)
        soak.clock.advance(minutes=10)
    database = soak.database
    soak.close()

    reopened = SqliteStore(database)
    reopened.migrate()
    try:
        report = health_module.inspect(reopened, soak.workspace_id, "soak",
                                       areas_root=soak.areas,
                                       max_dispatches=8, when=soak.clock())
        assert report.of("running") is not None
        assert report.of("escalations").level is not None
        assert "rows.events" in report.measurements
        assert report.measurements["rows.events"] > 0
    finally:
        reopened.close()


def test_health_reports_unknown_rather_than_ok_when_it_cannot_tell(tmp_path):
    store = SqliteStore(tmp_path / "empty.db")
    store.migrate()
    try:
        report = health_module.inspect(store, "wks_x", "x")
        assert report.level is Level.UNKNOWN
        assert not report.healthy, "UNKNOWN is not healthy; it is unexamined"
        assert report.of("orphan_areas").level is Level.UNKNOWN
        assert report.of("budget").level is Level.UNKNOWN
        # Named individually. Asserting only on the overall level let a
        # mutation turn this one OK without anything noticing, because two
        # other signals were holding the total at UNKNOWN by themselves.
        growth = report.of("database_growth")
        assert growth.level is Level.UNKNOWN
        assert "cannot be called normal or not" in growth.detail
    finally:
        store.close()


def test_health_names_an_orphaned_work_area_and_refuses_to_delete_it(soak):
    for n in range(1, 3):
        soak.tick(n)
        soak.clock.advance(minutes=5)

    orphan = soak.areas / "LEFTOVER-99"
    orphan.mkdir()
    (orphan / "work.py").write_text("# uncommitted\n", encoding="utf-8")

    report = health_module.inspect(soak.store, soak.workspace_id, "soak",
                                   areas_root=soak.areas, when=soak.clock())
    signal = report.of("orphan_areas")
    assert signal.level is Level.ATTENTION
    assert any("LEFTOVER-99" in e for e in signal.evidence)
    assert orphan.is_dir(), "reported, never deleted"
    assert (orphan / "work.py").exists()


def test_health_flags_a_run_that_outlived_its_lease_window(tmp_path):
    """A live lease must not excuse a run that has been going for days.

    This was real: a clock mismatch left a run RUNNING with a lease that read as
    live for 103 consecutive ticks, and health reported OK the whole way.
    """
    s = Soak(root=tmp_path / "zombie", lease_seconds=60,
             schedules={"worker_dies": Schedule(kind="error", at=frozenset({1}),
                                                reason="killed")})
    s.open()
    s.write_task("ZOM-1", resources=("repo:a",))
    try:
        s.tick(1)
        s.clock.advance(seconds=30)
        s.tick(2)
        assert s.store.active_runs(s.workspace_id)

        # Keep the lease alive by hand, as a confused renewer would, and let the
        # run age far past any plausible window.
        far = s.clock() + timedelta(days=3)
        s.store.acquire_lease("repo:a", s.store.active_runs(s.workspace_id)[0].id,
                              s.workspace_id, 3600, when=far)

        report = health_module.inspect(s.store, s.workspace_id, "z", when=far)
        running = report.of("running")
        assert running.level is Level.STUCK
        assert "longer than a lease window" in running.detail
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Many ticks, invariants after every one
# ---------------------------------------------------------------------------

def test_a_short_soak_keeps_every_invariant(tmp_path):
    s = Soak(root=tmp_path / "short", schedules={
        "tasks": Schedule(kind="unavailable", every=5, reason="board outage"),
        "runner_fails": Schedule(kind="error", every=7, reason="runner failure"),
        "worker_dies": Schedule(kind="error", every=9, reason="worker killed"),
    })
    s.open()
    for i in range(1, 5):
        s.write_task(f"SOAK-{i}", resources=(f"repo:r{i % 2}",))
    try:
        report = s.run(40, advance_seconds=1800, crash_every=9,
                       decide_every=2, new_task_every=6)
        assert not report.violations, "\n".join(report.violations[:10])
        assert report.restarts >= 4
        assert sum(t.dispatched for t in report.ticks) > 0, (
            "a soak that dispatched nothing proves nothing")
        assert sum(t.recovered for t in report.ticks) > 0, (
            "workers died; recovery must have happened")
        assert report.faults_fired["worker_dies"] > 0
    finally:
        s.close()


def test_a_soak_that_stops_doing_work_is_a_failure_not_a_pass(tmp_path):
    """Silence is measured, not rewarded.

    An engine that quietly stops dispatching returns clean ticks forever. This
    asserts that work kept happening in the second half of the run, which is the
    property a green tick cannot express.
    """
    s = Soak(root=tmp_path / "quiet")
    s.open()
    for i in range(1, 4):
        s.write_task(f"Q-{i}", resources=(f"repo:r{i}",))
    try:
        report = s.run(30, advance_seconds=3600, decide_every=2,
                       new_task_every=5)
        second_half = report.ticks[len(report.ticks) // 2:]
        assert any(t.dispatched for t in second_half), (
            "the engine stopped doing work half way through and said nothing")
    finally:
        s.close()


# ---------------------------------------------------------------------------
# The thirteenth question: what is in flight?
# ---------------------------------------------------------------------------
#
# A delivery waiting on somebody else's CI is the easiest thing in the system to
# lose sight of: no run, no lease, no worker, nothing that expires and
# complains. If `regente health` cannot see it, nothing can.

def _awaiting(store, workspace_id, key="FLY-1", with_delivery=True, asked=0,
              ci_state="PENDING"):
    from regente.core import ids
    from regente.core.model import ExternalRef, Task
    from regente.core.states import TaskState

    task = Task(id=ids.new_id(ids.TASK), workspace_id=workspace_id,
                project_id="prj", title="in flight", state=TaskState.READY,
                externo=ExternalRef(provider="filesystem", key=key))
    store.save_task(task)
    for step in (TaskState.ASSIGNED, TaskState.IMPLEMENTING, TaskState.TESTING,
                 TaskState.PR_CREATED, TaskState.CI_RUNNING):
        store.transition(task.id, step, actor="test", reason="setup",
                         workspace_id=workspace_id)
    if with_delivery:
        row = store.open_delivery(workspace_id, key, "run_1", "github",
                                  "acme/worker", "regente/fly", "e" * 40)
        store.record_push(row, "https://example.invalid/acme/worker.git")
        store.record_pull_request(row, 4, "https://example.invalid/pull/4",
                                  "e" * 40)
        for _ in range(asked):
            store.record_ci(row, state=ci_state, result=None, reason="waiting",
                            checks=[])
    return task


def test_health_sees_a_delivery_in_flight(tmp_path):
    from regente.core.model import Workspace
    from regente.core.policy import AutonomyLevel
    from regente.engine import health
    from regente.engine.store_sqlite import SqliteStore

    store = SqliteStore(tmp_path / "h.db")
    store.migrate()
    store.save_workspace(Workspace(id="wks_h", client_id="c", name="ws",
                                   max_autonomy=AutonomyLevel.L3))
    try:
        _awaiting(store, "wks_h", asked=2)
        report = health.inspect(store, "wks_h", areas_root=tmp_path / "areas")

        signal = next(s for s in report.signals
                      if s.question == "deliveries_in_flight")
        assert signal.level is health.Level.OK
        assert "1 delivery(ies) in flight" in signal.detail
        assert any("PR #4" in d and "asked 2x" in d for d in signal.evidence)
    finally:
        store.close()


def test_health_calls_a_delivery_waiting_on_nothing_stuck(tmp_path):
    """The dangerous shape: a task waits, and no row says for what."""
    from regente.core.model import Workspace
    from regente.core.policy import AutonomyLevel
    from regente.engine import health
    from regente.engine.store_sqlite import SqliteStore

    store = SqliteStore(tmp_path / "h.db")
    store.migrate()
    store.save_workspace(Workspace(id="wks_h", client_id="c", name="ws",
                                   max_autonomy=AutonomyLevel.L3))
    try:
        _awaiting(store, "wks_h", with_delivery=False)
        report = health.inspect(store, "wks_h", areas_root=tmp_path / "areas")

        signal = next(s for s in report.signals
                      if s.question == "deliveries_in_flight")
        assert signal.level is health.Level.STUCK
        assert not report.healthy
        assert any("nothing says what it waits for" in d for d in signal.evidence)
    finally:
        store.close()


def test_health_flags_a_delivery_nobody_has_answered_about(tmp_path):
    from regente.core.model import Workspace
    from regente.core.policy import AutonomyLevel
    from regente.engine import health
    from regente.engine.store_sqlite import SqliteStore

    store = SqliteStore(tmp_path / "h.db")
    store.migrate()
    store.save_workspace(Workspace(id="wks_h", client_id="c", name="ws",
                                   max_autonomy=AutonomyLevel.L3))
    try:
        _awaiting(store, "wks_h", asked=12, ci_state="UNAVAILABLE")
        report = health.inspect(store, "wks_h", areas_root=tmp_path / "areas")

        signal = next(s for s in report.signals
                      if s.question == "deliveries_in_flight")
        assert signal.level is health.Level.ATTENTION
        assert "no answer yet" in signal.detail
    finally:
        store.close()


def test_a_task_awaiting_ci_is_not_reported_as_a_dead_end(tmp_path):
    """It has a route out: the tick reads its checks and hands it to a person.

    Before the watcher existed it did not, and reporting it as healthy then
    would have been the exact lie `dead_end_tasks` is there to prevent.
    """
    from regente.core.model import Workspace
    from regente.core.policy import AutonomyLevel
    from regente.engine import health
    from regente.engine.store_sqlite import SqliteStore

    store = SqliteStore(tmp_path / "h.db")
    store.migrate()
    store.save_workspace(Workspace(id="wks_h", client_id="c", name="ws",
                                   max_autonomy=AutonomyLevel.L3))
    try:
        _awaiting(store, "wks_h", asked=1)
        report = health.inspect(store, "wks_h", areas_root=tmp_path / "areas")
        signal = next(s for s in report.signals
                      if s.question == "dead_end_tasks")
        assert signal.level is health.Level.OK, signal.detail
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Composition writes the option; the factory reads it. They must agree.
# ---------------------------------------------------------------------------
#
# `container.py` injected `"secrets"`; the jira factory read `o["segredos"]`.
# The rename to English moved one and not the other, and the result was a
# `KeyError` from inside a factory -- which is what a silent rename looks like
# from the outside. Same shape as the dispatch counter in Milestone 6: a writer
# and a reader that stopped agreeing, with nothing in between to notice.

def _config(tmp_path, providers: str) -> "object":
    from regente.app import config as config_mod

    (tmp_path / "policies.yaml").write_text(
        "rules:\n  - name: read\n    effect: ALLOW\n    match: {action: '*.read'}\n",
        encoding="utf-8")
    (tmp_path / "regente.yaml").write_text(
        "organization: org\nclient: cli\nworkspace: ws\nautonomy: L1\n"
        "shadow: true\nroot: .regente\npolicies: policies.yaml\n"
        "secrets:\n- env:JIRA_EMAIL\n- env:JIRA_API_TOKEN\n"
        f"providers:\n{providers}"
        "  workspace_provider:\n    name: directory\n"
        "  runner:\n    name: script\n"
        "projects:\n- name: P\n  default_environment: staging\n",
        encoding="utf-8")
    return config_mod.load(tmp_path / "regente.yaml")


def test_no_adapter_asks_for_an_option_composition_never_writes(tmp_path):
    """Every shipped adapter, built the way `regente doctor` builds it.

    A `KeyError` here means an option name drifted. It is checked separately
    from whether the adapter works, because "the token is missing" is a legible
    answer a person can act on and "KeyError: 'segredos'" is not.
    """
    from regente.app.container import diagnose

    for name, block in (
            ("jira", "  tasks:\n    name: jira\n    site: https://example.invalid\n"),
            ("filesystem", "  tasks:\n    name: filesystem\n    directory: board\n"),
            ("git-local", "  repository:\n    name: git-local\n"
                          f"    root: {tmp_path}\n"
                          "  tasks:\n    name: filesystem\n    directory: board\n"),
            ("console", "  notification:\n    name: console\n"
                        "  tasks:\n    name: filesystem\n    directory: board\n")):
        (tmp_path / "board").mkdir(exist_ok=True)
        results = diagnose(_config(tmp_path, block))
        for check, ok, detail in results:
            assert "KeyError" not in detail, (
                f"{name}: composition and the factory disagree about an option "
                f"name -- {check}: {detail}")


def test_a_missing_credential_is_reported_as_a_missing_credential(tmp_path):
    """The failure a person can act on, stated as itself."""
    import os

    from regente.app.container import diagnose

    for var in ("JIRA_EMAIL", "JIRA_API_TOKEN"):
        os.environ.pop(var, None)
    results = diagnose(_config(
        tmp_path, "  tasks:\n    name: jira\n    site: https://example.invalid\n"))
    tasks = next(d for name, _, d in results if name == "provider tasks")
    assert "SecretMissing" in tasks and "JIRA_EMAIL" in tasks


# ---------------------------------------------------------------------------
# One column, one meaning
# ---------------------------------------------------------------------------
#
# `runs.task_id` carried two: the orchestrator wrote the row id, the standalone
# mission path wrote the provider's key. `task_runs` matches on id, so half the
# runs were invisible from their own task -- no error, no log, just a screen
# saying "no executions". Found while building the read model, which is the
# first thing that had to join the two.

def test_a_run_is_reachable_from_its_own_task_whichever_path_made_it(tmp_path):
    from regente.core import ids
    from regente.core.model import ExternalRef, Run, RunState, Task, Workspace
    from regente.core.policy import AutonomyLevel
    from regente.engine.store_sqlite import SqliteStore

    store = SqliteStore(tmp_path / "r.db")
    store.migrate()
    store.save_workspace(Workspace(id="wks_r", client_id="c", name="ws",
                                   max_autonomy=AutonomyLevel.L3))
    try:
        task = Task(id=ids.new_id(ids.TASK), workspace_id="wks_r",
                    project_id="p", title="t",
                    externo=ExternalRef(provider="filesystem", key="R-1"))
        store.save_task(task)
        store.save_run(Run(id="run_a", task_id=task.id, task_key="R-1",
                           workspace_id="wks_r", agent="coder",
                           state=RunState.SUCCEEDED))

        found = store.task_runs(task.id, "wks_r")
        assert [r.id for r in found] == ["run_a"]
        assert found[0].task_key == "R-1"
    finally:
        store.close()


def test_the_migration_separates_a_key_that_was_stored_as_an_id(tmp_path):
    """The repair does not guess.

    A `task_id` that matches no task row can only be the one that held a key.
    Everything else gets its key by joining. A row that cannot be resolved
    either way keeps an empty id -- which is the truth, not a placeholder.
    """
    import sqlite3

    from regente.core import ids
    from regente.core.model import ExternalRef, Task, Workspace
    from regente.core.policy import AutonomyLevel
    from regente.engine.store_sqlite import SqliteStore

    path = tmp_path / "old.db"
    store = SqliteStore(path)
    store.migrate()
    store.save_workspace(Workspace(id="wks_r", client_id="c", name="ws",
                                   max_autonomy=AutonomyLevel.L3))
    task = Task(id=ids.new_id(ids.TASK), workspace_id="wks_r", project_id="p",
                title="t", externo=ExternalRef(provider="filesystem", key="R-9"))
    store.save_task(task)
    store.close()

    # Rewind to the shape that carried the ambiguity: no task_key column, and
    # one run per writer -- one holding an id, the other holding a key.
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE runs DROP COLUMN task_key")
    for run_id, stored in (("run_id_writer", task.id), ("run_key_writer", "R-9")):
        raw.execute(
            "INSERT INTO runs(id, task_id, workspace_id, agent, state, started_at)"
            " VALUES(?,?,?,?,?,?)",
            (run_id, stored, "wks_r", "coder", "SUCCEEDED",
             "2026-01-01T00:00:00.000000Z"))
    raw.execute("UPDATE meta SET value='7' WHERE key='schema'")
    raw.commit()
    raw.close()

    store = SqliteStore(path)
    store.migrate()
    try:
        by_id = {r.id: r for r in store.task_runs(task.id, "wks_r")}
        assert set(by_id) == {"run_id_writer"}, (
            "the run that stored a key should not claim to belong to a task id")
        assert by_id["run_id_writer"].task_key == "R-9"

        moved = store.run("run_key_writer", "wks_r")
        assert moved.task_key == "R-9"
        assert moved.task_id == "", (
            "a key left sitting in task_id keeps the ambiguity alive")
    finally:
        store.close()
