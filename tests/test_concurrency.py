# -*- coding: utf-8 -*-
"""Two or more workers, one database, genuine contention.

`max_workers > 1` is a setting. Concurrency is a fact about processes, and until
this file existed the project had only ever had the setting -- a thousand-tick
soak with `max_workers=3` never held more than one lease at a time, so leases,
`BEGIN IMMEDIATE` and the `(workspace_id, resource)` key were untested code that
looked tested.

The scenarios below split into two kinds and both are needed:

  **Store-level**, where the exact interleaving is chosen rather than hoped for.
  A race reproduced on demand can be used to prove a fix; a race observed once
  cannot.

  **Process-level**, with real interpreters, real memory and real `SIGKILL`.
  Threads share a GIL and a heap and would let a broken design pass.

The most important test in the file is the stale worker: a process that is
ALIVE, still holding a mission, and no longer the owner. A dead process takes no
further action, so only a live one can act without the right to -- and stopping
it needs authorisation to be rechecked at the moment of acting, not at the
moment of starting.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from concurrency import analyse, write_tasks
from faults import FrozenRenewal, Schedule, StolenLease
from soak import Clock, Soak

from regente.core.model import RunState, now
from regente.core.states import TaskState
from regente.engine.ownership import Heartbeat, Ownership, OwnershipLost
from regente.engine.store_sqlite import SqliteStore

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Lease semantics, at the level where the interleaving is chosen
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path) -> SqliteStore:
    s = SqliteStore(tmp_path / "race.db")
    s.migrate()
    yield s
    s.close()


def test_2_only_one_worker_can_hold_a_resource(store):
    """Mandatory scenario 2. The other must be refused, not queued silently."""
    at = now()
    assert store.acquire_lease("repo:a", "runA", "w", 60, when=at) is not None
    assert store.acquire_lease("repo:a", "runB", "w", 60, when=at) is None

    assert store.holds_lease("repo:a", "runA", "w", when=at)
    assert not store.holds_lease("repo:a", "runB", "w", when=at)

    leases = [l for l in store.leases("w") if l.resource == "repo:a"]
    assert len(leases) == 1, "one resource, one row, one owner"
    assert leases[0].owner == "runA"


def test_1_different_resources_do_not_block_each_other(store):
    """Mandatory scenario 1. Contention must be per resource, not global."""
    at = now()
    assert store.acquire_lease("repo:a", "runA", "w", 60, when=at)
    assert store.acquire_lease("repo:b", "runB", "w", 60, when=at)
    assert store.holds_lease("repo:a", "runA", "w", when=at)
    assert store.holds_lease("repo:b", "runB", "w", when=at)


def test_4_a_live_lease_cannot_be_stolen_and_an_expired_one_can(store):
    """Mandatory scenario 4, and the window between them.

    There must be no instant at which both runs are told they own it.
    """
    at = now()
    store.acquire_lease("repo:a", "runA", "w", 30, when=at)

    # Walk right up to the boundary, one second at a time, and check at every
    # step that exactly one owner exists. A single check at the end could not
    # tell a clean handover from a moment of shared belief.
    for offset in range(0, 31):
        moment = at + timedelta(seconds=offset)
        a = store.holds_lease("repo:a", "runA", "w", when=moment)
        stolen = store.acquire_lease("repo:a", "runB", "w", 30, when=moment)
        b = store.holds_lease("repo:a", "runB", "w", when=moment)
        assert not (a and b), f"both owned it at +{offset}s"
        if offset < 30:
            assert a and stolen is None, f"A lost it early at +{offset}s"
        else:
            assert stolen is not None and b, "B could not take an expired lease"


def test_6_two_workspaces_do_not_contend_for_the_same_resource_name(store):
    """Mandatory scenario 6. Identity is (workspace_id, resource)."""
    at = now()
    assert store.acquire_lease("repo:x", "runA", "wks_a", 60, when=at)
    assert store.acquire_lease("repo:x", "runB", "wks_b", 60, when=at)

    assert store.holds_lease("repo:x", "runA", "wks_a", when=at)
    assert store.holds_lease("repo:x", "runB", "wks_b", when=at)
    assert not store.holds_lease("repo:x", "runA", "wks_b", when=at)
    assert len(store.leases("wks_a")) == 1
    assert len(store.leases("wks_b")) == 1


def test_a_stale_owner_cannot_renew_what_it_no_longer_holds(store):
    """The clause that makes everything else safe.

    Without `AND owner=?` on the renewal, a worker whose lease expired would
    quietly extend a lease that now belongs to somebody else, and two workers
    would believe they owned the same resource.
    """
    at = now()
    store.acquire_lease("repo:a", "runA", "w", 10, when=at)
    later = at + timedelta(seconds=11)
    assert store.acquire_lease("repo:a", "runB", "w", 60, when=later)

    assert store.renew_lease("repo:a", "runA", 60, "w", when=later) is False
    assert not store.holds_lease("repo:a", "runA", "w", when=later)
    assert store.holds_lease("repo:a", "runB", "w", when=later)


def test_releasing_is_scoped_to_the_owner(store):
    """A departing worker must not free a lease that is no longer its own."""
    at = now()
    store.acquire_lease("repo:a", "runA", "w", 10, when=at)
    later = at + timedelta(seconds=11)
    store.acquire_lease("repo:a", "runB", "w", 60, when=later)

    store.release_lease("repo:a", "runA", "w")     # the stale worker tidying up
    assert store.holds_lease("repo:a", "runB", "w", when=later), (
        "a stale worker released the new owner's lease")


# ---------------------------------------------------------------------------
# Ownership: the right to act, revalidated when acting
# ---------------------------------------------------------------------------

def test_ownership_refuses_when_the_lease_expired(store):
    at = now()
    store.acquire_lease("repo:a", "runA", "w", 10, when=at)
    clock = Clock(at=at)
    owner = Ownership(store=store, workspace_id="w", run_id="runA",
                      resources=("repo:a",), lease_seconds=10, clock=clock)
    owner.verify()

    clock.at = at + timedelta(seconds=11)
    with pytest.raises(OwnershipLost, match="no longer holds"):
        owner.verify()
    assert not owner.held()


def test_ownership_refuses_when_one_of_several_resources_is_lost(store):
    """All of them, or none. A mission holding two resources and half the
    authority is a mission that must not write."""
    at = now()
    store.acquire_lease("repo:a", "runA", "w", 600, when=at)
    store.acquire_lease("repo:b", "runA", "w", 10, when=at)
    clock = Clock(at=at + timedelta(seconds=11))

    owner = Ownership(store=store, workspace_id="w", run_id="runA",
                      resources=("repo:a", "repo:b"), lease_seconds=10,
                      clock=clock)
    with pytest.raises(OwnershipLost, match="repo:b"):
        owner.verify()


def test_a_heartbeat_keeps_a_long_mission_alive(store):
    """A mission longer than its lease must not lose it while working.

    The field comment on `lease_seconds` promised "o worker renova" long before
    anything renewed. This is that promise, and the test is what keeps it.
    """
    at = now()
    store.acquire_lease("repo:a", "runA", "w", 1, when=at)
    owner = Ownership(store=store, workspace_id="w", run_id="runA",
                      resources=("repo:a",), lease_seconds=1)

    with Heartbeat(ownership=owner, interval=0.05) as beat:
        time.sleep(1.6)          # comfortably past the original expiry
        assert not beat.lost
        assert owner.held(), "the heartbeat let a live worker's lease expire"


def test_a_heartbeat_notices_when_renewal_is_refused(store):
    """The other half: a worker that cannot renew must learn that it lost."""
    at = now()
    store.acquire_lease("repo:a", "runA", "w", 60, when=at)
    frozen = FrozenRenewal(inner=store)
    owner = Ownership(store=frozen, workspace_id="w", run_id="runA",
                      resources=("repo:a",), lease_seconds=60)

    with Heartbeat(ownership=owner, interval=0.05) as beat:
        time.sleep(0.3)
    assert beat.lost
    assert frozen.refusals > 0


# ---------------------------------------------------------------------------
# The stale worker: alive, working, and no longer the owner
# ---------------------------------------------------------------------------

class ClockBurningAgent:
    """A runner that consumes engine time while it works.

    With an injected clock, sleeping in real seconds does not move the engine's
    idea of "now", so a lease cannot expire during a mission however long the
    test waits. This agent advances the clock instead -- deterministically, by
    exactly the amount the scenario needs.

    That is the honest way to express "this mission outlived its lease": the
    engine sees the same sequence of timestamps it would see in production, and
    the test does not depend on how fast the machine is.
    """
    name = "clock-burner"

    def __init__(self, clock, seconds: int, inner):
        self.clock, self.seconds, self.inner = clock, seconds, inner

    def availability(self):
        return self.inner.availability()

    def describe(self) -> dict:
        return {"adapter": self.name}

    def run(self, mission):
        self.clock.advance(seconds=self.seconds)
        return self.inner.run(mission)


def test_a_worker_that_lost_its_lease_writes_nothing(tmp_path):
    """The most important test in this milestone.

    The engine must refuse a result produced by a run that no longer owns its
    resources -- even though that process is alive, finished its mission, and
    has an answer in hand.

    On the first real three-process run this happened and was refused only
    because the transition it attempted was illegal from the state the task
    happened to be in. Had the task been one state further along, the write
    would have been legal and would have overwritten another worker's run.
    """
    s = Soak(root=tmp_path / "stale", lease_seconds=30)
    s.open()
    s.write_task("STALE-1", resources=("repo:a",))
    try:
        s.tick(1)                       # discovery
        s.clock.advance(minutes=1)

        # Alive, but unable to keep its lease -- a process that is running and
        # not being scheduled. Its mission then outlives the lease window.
        s.orchestrator.store = FrozenRenewal(inner=s.store)
        s.orchestrator.runner = ClockBurningAgent(s.clock, 120,
                                                  s.orchestrator.runner)
        outcome = s.orchestrator.tick()

        assert outcome.dispatched, "the task must have been dispatched"
        assert any("ownership lost" in e for e in outcome.errors), (
            "a worker that lost its lease reported success instead")

        task = s.store.task_by_key(s.workspace_id, "filesystem", "STALE-1")
        assert task.state is not TaskState.TESTING, (
            "the stale worker's result was written to the task")

        run = s.store.task_runs(task.id)[-1]
        assert run.state is RunState.INTERRUPTED
        assert "posse" in run.reason or "ownership" in run.reason.lower()
    finally:
        s.close()


def test_a_stolen_lease_is_caught_even_when_the_heartbeat_was_happy(tmp_path):
    """The second guard, on its own.

    Renewal keeps succeeding while the lease is quietly reassigned, so the
    heartbeat notices nothing. Only the check immediately before the write can
    catch this, and testing the two guards together could not tell which one was
    holding.
    """
    s = Soak(root=tmp_path / "stolen", lease_seconds=600)
    s.open()
    s.write_task("STOLEN-1", resources=("repo:a",))
    try:
        s.tick(1)
        s.clock.advance(minutes=1)

        thief = StolenLease(inner=s.store)
        s.orchestrator.store = thief

        original_run = s.orchestrator.runner.run

        def steal_then_run(mission):
            thief.steal_for = mission.run_id
            thief.steal("repo:a", s.workspace_id, when=s.clock())
            return original_run(mission)

        s.orchestrator.runner.run = steal_then_run
        outcome = s.orchestrator.tick()

        assert thief.stolen == ["repo:a"]
        assert any("ownership lost" in e for e in outcome.errors), (
            "the pre-write ownership check did not catch a stolen lease")
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Acquisition order
# ---------------------------------------------------------------------------

def test_5_resources_are_acquired_in_a_deterministic_global_order(tmp_path):
    """Mandatory scenario 5.

    Acquisition never blocks -- a held lease is refused at once and the caller
    gives back what it took -- so a classic deadlock cannot occur. LIVELOCK can:
    two tasks wanting {A,B} in opposite orders take one each, both fail on the
    other, both release, and both retry for ever.

    A global order breaks the symmetry. Asserted structurally, because a timing
    test for livelock is a test that passes on a fast machine.
    """
    import ast

    from regente.engine import store_sqlite as module

    # Ordering lives where acquisition lives, which is now inside the atomic
    # claim. Asserted there rather than at the call site: a caller that forgot
    # to sort would be harmless, and the claim forgetting would not be.
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    claim = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "claim")
    sorted_loops = [
        node for node in ast.walk(claim)
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Call)
        and getattr(node.iter.func, "id", "") == "sorted"]
    assert len(sorted_loops) >= 2, (
        "both the check and the take must walk resources in the same "
        "deterministic order, or two workers can livelock taking one each")


def test_a_mission_needing_two_resources_takes_both_or_neither(tmp_path):
    s = Soak(root=tmp_path / "pair", lease_seconds=300)
    s.open()
    s.write_task("PAIR-1", resources=("repo:a", "repo:b"))
    try:
        s.tick(1)
        s.clock.advance(minutes=1)

        # Somebody else already holds the second resource. Given a real run
        # row, because that is what a real competitor has -- a lease whose owner
        # exists nowhere is residue, and recovery is supposed to clear it.
        from regente.core.model import Run, RunState

        rival = Run(id="run_other", task_id="tsk_other",
                    workspace_id=s.workspace_id, agent="rival",
                    state=RunState.RUNNING, started_at=s.clock())
        assert s.store.claim(rival, ("repo:b",), 300, when=s.clock())
        s.tick(2)

        held = {l.resource: l.owner for l in s.store.leases(s.workspace_id)}
        assert held.get("repo:b") == "run_other"
        assert "repo:a" not in held, (
            "a mission that could not take every resource kept one of them")
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Real processes
# ---------------------------------------------------------------------------

def race(tmp_path, **kw) -> dict:
    """Run the multi-process harness and return its findings."""
    root = tmp_path / kw.pop("name", "race")
    (root / "areas").mkdir(parents=True)
    write_tasks(root, kw.pop("tasks", 4), kw.pop("resources", 2))

    args = [sys.executable, str(HERE / "concurrency.py"),
            "--root", str(root), "--workers", str(kw.pop("workers", 3)),
            "--seconds", str(kw.pop("seconds", 5)),
            "--lease-seconds", str(kw.pop("lease_seconds", 2)),
            "--slow", str(kw.pop("slow", 0.3))]
    for key, value in kw.items():
        args += [f"--{key.replace('_', '-')}", str(value)]

    finished = subprocess.run(args, capture_output=True, encoding="utf-8",
                              errors="replace", timeout=300)
    found = analyse(root)
    return {"exit": finished.returncode, "out": finished.stdout,
            "counters": dict(found.counters), "violations": found.violations}


@pytest.mark.slow
def test_3_and_7_real_processes_contend_without_breaking_anything(tmp_path):
    """Mandatory scenarios 3 and 7, with real interpreters.

    Several processes race for the same tasks and the same resources; one is
    killed part way through. Exactly one may execute a task at a time, and the
    survivors must carry on.
    """
    result = race(tmp_path, name="contend", workers=3, seconds=6, tasks=5,
                  resources=2, lease_seconds=2, slow=0.3, kill_at=2.0)

    counters = result["counters"]
    assert counters.get("workers", 0) >= 2, "the processes must have started"
    assert counters.get("dispatches", 0) > 0, "nothing was contended for"
    assert counters.get("duplicate_execution", 0) == 0
    assert counters.get("duplicate_ownership", 0) == 0
    assert counters.get("unhandled_database_locked", 0) == 0
    assert not result["violations"], "\n".join(result["violations"][:10])


@pytest.mark.slow
def test_8_workers_restarting_while_others_keep_working(tmp_path):
    """Mandatory scenario 8: no process is the keeper of the truth.

    Workers are killed at different moments while others carry on, and the state
    on disk stays consistent throughout.
    """
    result = race(tmp_path, name="restart", workers=4, seconds=7, tasks=6,
                  resources=3, lease_seconds=2, slow=0.25, kill_at=3.0)

    assert result["counters"].get("duplicate_execution", 0) == 0
    assert result["counters"].get("duplicate_ownership", 0) == 0
    assert not result["violations"], "\n".join(result["violations"][:10])


# ---------------------------------------------------------------------------
# Gaps a mutation sweep found: guards nothing was holding
# ---------------------------------------------------------------------------

def test_the_atomic_claim_is_scoped_to_the_workspace(store):
    """Tenancy, checked where the claim checks it.

    The existing tenancy test went through `acquire_lease`, so the workspace
    scoping *inside* `claim` was never exercised: dropping `workspace_id` from
    its lookup broke no test. Two clients with a repository of the same name
    would have blocked each other, which is the failure multi-tenancy exists to
    prevent.
    """
    from regente.core.model import Run, RunState

    at = now()
    a = Run(id="run_a", task_id="t1", workspace_id="wks_a", agent="coder",
            state=RunState.RUNNING, started_at=at)
    b = Run(id="run_b", task_id="t2", workspace_id="wks_b", agent="coder",
            state=RunState.RUNNING, started_at=at)

    assert store.claim(a, ("repo:same",), 300, when=at)
    assert store.claim(b, ("repo:same",), 300, when=at), (
        "a claim in one workspace blocked a claim in another")

    assert store.holds_lease("repo:same", "run_a", "wks_a", when=at)
    assert store.holds_lease("repo:same", "run_b", "wks_b", when=at)


def test_a_second_claim_in_the_same_workspace_is_still_refused(store):
    """The other half: scoping must not become a way to bypass the lease."""
    from regente.core.model import Run, RunState

    at = now()
    first = Run(id="run_1", task_id="t1", workspace_id="wks_a", agent="coder",
                state=RunState.RUNNING, started_at=at)
    second = Run(id="run_2", task_id="t2", workspace_id="wks_a", agent="coder",
                 state=RunState.RUNNING, started_at=at)
    assert store.claim(first, ("repo:same",), 300, when=at)
    assert not store.claim(second, ("repo:same",), 300, when=at)
    assert store.run("run_2") is None, "a refused claim wrote a run row"


def hold_the_write_lock(path, seconds: float):
    """Another connection genuinely holding the write lock, then letting go."""
    import sqlite3
    import threading

    blocker = sqlite3.connect(str(path), isolation_level=None,
                              check_same_thread=False)
    blocker.execute("PRAGMA busy_timeout=100")
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("INSERT INTO counters(workspace_id, day, name, value) "
                    "VALUES('w','2026-01-01','held',1)")
    released = threading.Event()

    def let_go():
        time.sleep(seconds)
        blocker.execute("COMMIT")
        blocker.close()
        released.set()

    threading.Thread(target=let_go, daemon=True).start()
    return released


def test_ordinary_lock_contention_is_waited_out_not_raised(tmp_path):
    """SQLite's own `busy_timeout` absorbs the common case.

    Worth stating explicitly, because it is why the Python-level retry almost
    never fires: for contention shorter than the timeout the engine simply
    waits, and the write happens exactly once when the lock frees.
    """
    path = tmp_path / "busy.db"
    s = SqliteStore(path)
    s.migrate()
    try:
        released = hold_the_write_lock(path, 0.4)
        s.mark_dispatch("w", "2026-01-01")      # blocks, then succeeds
        assert released.wait(timeout=5)
        assert s.dispatch_count("w", "2026-01-01") == 1, (
            "the write must happen exactly once, not zero times and not twice")
    finally:
        s.close()


def test_a_lock_held_past_the_timeout_is_retried_rather_than_raised(tmp_path):
    """And when the timeout IS exhausted, the retry is what saves the tick.

    Nothing in the suite forced contention this hard, so deleting the retry
    broke no test -- the guard was present and idle. Here `busy_timeout` is
    deliberately tiny so the lock outlives it and the Python retry has to act.

    The retry sits BEFORE the block, on the acquisition of the write lock, so
    nothing inside has run when it fires: repeating is exactly equivalent to
    having started later. That is why the count below can be greater than one
    and the write still happens exactly once.
    """
    path = tmp_path / "verybusy.db"
    s = SqliteStore(path)
    s.migrate()
    s.lock_backoff = 0.05
    try:
        s._con.execute("PRAGMA busy_timeout=30")
        released = hold_the_write_lock(path, 0.5)

        before = s.lock_retries
        s.mark_dispatch("w", "2026-01-01")
        assert released.wait(timeout=5)
        assert s.lock_retries > before, (
            "the lock outlived busy_timeout and no retry was needed?")
        assert s.dispatch_count("w", "2026-01-01") == 1, (
            "a retried transaction wrote its effect more than once")
    finally:
        s.close()


def test_the_orchestrator_renews_leases_while_a_mission_runs(tmp_path):
    """That the heartbeat EXISTS was tested; that the tick uses it was not.

    Removing the whole `with Heartbeat(...)` block from the dispatch path broke
    no test, so a mission longer than its lease window would have lost its
    resources with nothing noticing.
    """
    s = Soak(root=tmp_path / "renew", lease_seconds=1)
    s.open()
    s.write_task("RENEW-1", resources=("repo:a",))
    try:
        s.tick(1)
        s.clock.advance(minutes=1)

        renewals: list[str] = []
        real_renew = s.store.renew_lease

        def counted(*args, **kwargs):
            renewals.append(args[0] if args else "?")
            return real_renew(*args, **kwargs)

        s.store.renew_lease = counted

        # A mission long enough in REAL time for the heartbeat thread to fire
        # several times: the heartbeat sleeps on a wall clock, not the engine's.
        inner = s.orchestrator.runner

        class Slow:
            name = "slow"

            def availability(self):
                return inner.availability()

            def describe(self):
                return {"adapter": "slow"}

            def run(self, mission):
                time.sleep(1.2)
                return inner.run(mission)

        s.orchestrator.runner = Slow()
        outcome = s.orchestrator.tick()

        assert renewals, "the tick ran a mission without renewing anything"
        assert "repo:a" in renewals
        assert not any("ownership lost" in e for e in outcome.errors), (
            "the lease was lost during a mission the worker was renewing")
    finally:
        s.close()


def test_recovery_clears_a_lease_whose_owner_has_no_run(tmp_path):
    """Residue from an older database must not block a resource for ever.

    With an atomic claim this cannot arise any more, which is exactly why it
    needs a test: nothing else would ever produce the condition, so the clearing
    code would sit unexercised until a real database needed it.
    """
    s = Soak(root=tmp_path / "orphan", lease_seconds=300)
    s.open()
    s.write_task("ORPH-1", resources=("repo:a",))
    try:
        s.tick(1)
        # A lease owned by a run that does not exist: what a killed process left
        # behind before the claim became atomic.
        s.store.acquire_lease("repo:a", "run_from_an_older_version",
                              s.workspace_id, 300, when=s.clock())
        assert s.store.orphan_leases(s.workspace_id, when=s.clock())

        s.clock.advance(minutes=1)
        s.tick(2)

        assert not s.store.orphan_leases(s.workspace_id, when=s.clock()), (
            "a lease held by nobody still blocks its resource")
        assert any(e.kind == "lease_orfao"
                   for e in s.store.events(s.workspace_id, limit=50))
    finally:
        s.close()
