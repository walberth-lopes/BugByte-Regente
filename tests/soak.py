# -*- coding: utf-8 -*-
"""A soak harness: many ticks, injected faults, invariants checked every tick.

Runnable two ways. As a module it powers the fast deterministic tests in the
suite; as a script it runs for as long as you ask:

    python tests/soak.py --ticks 2000 --seed 7

The invariants are checked **after every tick**, not at the end. A run that only
checks its final state cannot tell a system that stayed correct from one that
broke and healed -- and the second is a system that will break and not heal on
the day it matters.

What it deliberately does not do is decide that a quiet tick is a good tick.
Silence is measured, not rewarded: the report distinguishes ticks that did work
from ticks that did nothing, because an engine that stops dispatching and keeps
returning cleanly is the exact failure this milestone exists to catch.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from regente.adapters.notify.console import Console
from regente.adapters.runner.scripted import ScriptedAgent
from regente.adapters.tasks.filesystem import FilesystemTasks
from regente.adapters.workspace.local import IsolatedDirectory
from regente.core.model import RunState, Workspace, now
from regente.core.policy import AutonomyLevel, PolicyEngine
from regente.core.risk import RiskEngine
from regente.core.scheduling import Limits
from regente.core.states import ACTIVE, TaskState
from regente.engine import health as health_module
from regente.engine.gate import Gate
from regente.engine.orchestrator import Orchestrator
from regente.engine.store_sqlite import SqliteStore

from faults import DyingRunner, FailingRunner, FlakyTasks, Schedule


# ---------------------------------------------------------------------------
# A clock that can be pushed forward
# ---------------------------------------------------------------------------

@dataclass
class Clock:
    """Time under the harness's control.

    Injected rather than patched, because patching `now()` globally would also
    move the store's row timestamps and hide the very drift a soak run is
    looking for. Here the orchestrator's sense of "now" moves and the rows keep
    saying when they were really written.
    """
    at: datetime = field(default_factory=lambda: datetime(
        2026, 1, 1, 9, 0, tzinfo=timezone.utc))

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kw) -> datetime:
        self.at = self.at + timedelta(**kw)
        return self.at


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TickMetrics:
    tick: int
    at: str
    dispatched: int
    recovered: int
    escalated: int
    completed: int
    errors: int
    live_leases: int
    expired_leases: int
    runs_running: int
    runs_interrupted: int
    open_approvals: int
    dispatches_today: int
    events: int
    database_bytes: int
    wal_bytes: int
    active_tasks: int
    health: str

    def as_dict(self) -> dict:
        return {f: getattr(self, f) for f in self.__slots__}


@dataclass
class SoakReport:
    ticks: list[TickMetrics] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    faults_fired: dict[str, int] = field(default_factory=dict)
    restarts: int = 0

    @property
    def worked(self) -> int:
        return sum(1 for t in self.ticks if t.dispatched or t.recovered)

    def summary(self) -> str:
        if not self.ticks:
            return "no ticks"
        last = self.ticks[-1]
        return (
            f"{len(self.ticks)} ticks, {self.worked} did work, "
            f"{sum(t.dispatched for t in self.ticks)} dispatches, "
            f"{sum(t.recovered for t in self.ticks)} recoveries, "
            f"{sum(t.escalated for t in self.ticks)} escalations, "
            f"{self.restarts} restarts, "
            f"data {last.database_bytes // 1024}KB + wal {last.wal_bytes // 1024}KB, "
            f"{last.events} events, "
            f"{len(self.violations)} invariant violation(s)")


# ---------------------------------------------------------------------------
# The invariants
# ---------------------------------------------------------------------------

def check_invariants(store, workspace_id: str, at: datetime,
                     areas_root: Path) -> list[str]:
    """Things that must be true after EVERY tick.

    Each one is a condition that, if it ever held, would mean the engine is
    quietly losing work rather than doing it. They are written as questions
    about persisted state so they answer identically before and after a crash.
    """
    broken: list[str] = []

    # A lease outliving its run means a resource nobody can take, forever.
    runs = {r.id for r in store.active_runs(workspace_id)}
    for lease in store.leases(workspace_id):
        if lease.expires_at and lease.expires_at > at and lease.owner not in runs:
            broken.append(
                f"live lease '{lease.resource}' is held by '{lease.owner}', "
                f"which is not an active run")

    # A task in an active state with no run is work nobody is doing, and the
    # scheduler will not pick it up because it looks busy.
    active = [t for t in store.tasks(workspace_id) if t.state in ACTIVE]
    owned = {r.task_id for r in store.active_runs(workspace_id)}
    for task in active:
        if task.id not in owned and task.state is not TaskState.READY:
            broken.append(
                f"task {task.key} is {task.state.value} with no active run")

    # WAITING_HUMAN without a record of where it paused cannot be resumed: a
    # decision would have nowhere to send it.
    for task in store.tasks(workspace_id):
        if task.state is TaskState.WAITING_HUMAN and task.paused_at is None:
            broken.append(f"task {task.key} waits for a person from nowhere")

    # A work area belonging to nothing is either abandoned work or a leak.
    if areas_root.is_dir():
        keys = {t.key for t in store.tasks(workspace_id)}
        keys |= {"".join(c if c.isalnum() or c in "-_" else "_" for c in k)
                 for k in keys}
        paths = {r.workspace_path for r in store.active_runs(workspace_id)}
        for child in areas_root.iterdir():
            if child.is_dir() and child.name not in keys and str(child) not in paths:
                broken.append(f"work area '{child.name}' belongs to no task")

    return broken


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------

@dataclass
class Soak:
    root: Path
    clock: Clock = field(default_factory=Clock)
    lease_seconds: int = 300
    max_dispatches: int = 8
    workspace_id: str = "wks_soak"
    #: Faults, by name, so a report can say which fired.
    schedules: dict[str, Schedule] = field(default_factory=dict)
    store: SqliteStore | None = None
    orchestrator: Orchestrator | None = None
    report: SoakReport = field(default_factory=SoakReport)
    arrivals: int = 0

    # ---- world ---------------------------------------------------------
    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def areas(self) -> Path:
        return self.root / "areas"

    @property
    def database(self) -> Path:
        return self.root / "regente.db"

    def write_task(self, key: str, status: str = "TO DO",
                   resources: tuple[str, ...] = ()) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        body = [f"key: {key}", f"title: work {key}", f"status: {status}"]
        if resources:
            body.append("resources:")
            body += [f"  - {r}" for r in resources]
        (self.tasks_dir / f"{key}.yaml").write_text("\n".join(body) + "\n",
                                                    encoding="utf-8")

    # ---- open / close, the way a restart does it ------------------------
    def open(self) -> Soak:
        """Build the engine from what is on disk. Called again after a restart.

        Nothing carries over in memory. That is the point: a restart that reused
        an object would prove that objects survive, which nobody doubted.
        """
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.areas.mkdir(parents=True, exist_ok=True)

        # One clock for the engine and for the rows it writes. Two was the
        # root cause of every time-related defect this harness found.
        self.store = SqliteStore(self.database, clock=self.clock)
        self.store.migrate()
        workspace = Workspace(id=self.workspace_id, client_id="cli_soak",
                              name="soak", max_autonomy=AutonomyLevel.L3)
        self.store.save_workspace(workspace)

        tasks = FilesystemTasks(self.tasks_dir)
        if "tasks" in self.schedules:
            tasks = FlakyTasks(inner=tasks, schedule=self.schedules["tasks"])

        runner = ScriptedAgent(default_value={
            "status": "FINISHED", "claim": "COMPLETE", "summary": "soak"})
        if "runner_fails" in self.schedules:
            runner = FailingRunner(inner=runner,
                                   schedule=self.schedules["runner_fails"])
        if "worker_dies" in self.schedules:
            runner = DyingRunner(inner=runner,
                                 schedule=self.schedules["worker_dies"])

        risk = RiskEngine()
        self.orchestrator = Orchestrator(
            store=self.store, workspace=workspace, tasks_provider=tasks,
            area_provider=IsolatedDirectory(self.areas), runner=runner,
            gate=Gate(store=self.store, policy=PolicyEngine.from_config([]),
                      risk=risk),
            risk=risk, clock=self.clock,
            limits=Limits(max_workers=3,
                          max_dispatches_per_day=self.max_dispatches),
            notifier=Console(journal=self.root / "journal.log"),
            lease_seconds=self.lease_seconds)
        return self

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
        self.store = None
        self.orchestrator = None

    def restart(self) -> None:
        """Drop everything in memory and rebuild from disk."""
        self.close()
        self.report.restarts += 1
        self.open()

    def crash(self) -> None:
        """Stop without closing anything. What a killed process leaves behind.

        Deliberately does NOT call `close()`. A clean close flushes and releases;
        a killed process does neither, and the difference is exactly what
        recovery has to survive.
        """
        self.store = None
        self.orchestrator = None
        self.report.restarts += 1
        self.open()

    # ---- one tick -------------------------------------------------------
    def tick(self, number: int) -> TickMetrics:
        assert self.orchestrator is not None and self.store is not None
        at = self.clock()
        try:
            outcome = self.orchestrator.tick()
            dispatched, recovered = len(outcome.dispatched), len(outcome.recovered)
            escalated, completed = len(outcome.escalated), len(outcome.completed)
            errors = len(outcome.errors)
        except DyingRunner.WorkerDied:
            # The worker was killed. Nothing is written by the dead process;
            # the run row stays RUNNING and its lease stays held until it
            # expires. The next tick has to notice from the disk.
            dispatched = recovered = escalated = completed = 0
            errors = 1

        leases = self.store.leases(self.workspace_id)
        counts = self.store.table_counts(self.workspace_id)
        report = health_module.inspect(
            self.store, self.workspace_id, "soak", areas_root=self.areas,
            budget_usd=None, max_dispatches=self.max_dispatches, when=at)

        metrics = TickMetrics(
            tick=number, at=at.isoformat(), dispatched=dispatched,
            recovered=recovered, escalated=escalated, completed=completed,
            errors=errors,
            live_leases=sum(1 for l in leases if l.expires_at and l.expires_at > at),
            expired_leases=sum(1 for l in leases
                               if l.expires_at and l.expires_at <= at),
            runs_running=len(self.store.active_runs(self.workspace_id)),
            runs_interrupted=len(self.store.runs_in_state(
                self.workspace_id, RunState.INTERRUPTED.value)),
            open_approvals=len(self.store.open_approvals(self.workspace_id)),
            dispatches_today=self.store.dispatch_count(
                self.workspace_id, at.strftime("%Y-%m-%d")),
            events=counts.get("events", 0),
            database_bytes=self.store.database_bytes()["data"],
            wal_bytes=self.store.database_bytes()["wal"],
            active_tasks=sum(1 for t in self.store.tasks(self.workspace_id)
                             if t.state in ACTIVE),
            health=report.level.value)

        self.report.ticks.append(metrics)
        for problem in check_invariants(self.store, self.workspace_id, at,
                                        self.areas):
            self.report.violations.append(f"tick {number}: {problem}")
        return metrics

    def decide_everything(self, choice: str = "investigate",
                          who: str = "soak-operator") -> int:
        """Stand in for the person at the other end of the queue.

        Without this the run proves only that the engine can stop. An engine
        whose every task ends in WAITING_HUMAN has nothing left to demonstrate
        about durability, and the queue draining is the half of the loop that
        had never been exercised at all.
        """
        decided = 0
        for approval in self.store.open_approvals(self.workspace_id):
            # Mostly "send it back", because that is what keeps an engine
            # working and is therefore what needs proving. Occasionally block or
            # cancel, so those routes are exercised too rather than assumed.
            pick = choice
            if decided and decided % 7 == 0:
                pick = "block"
            elif decided and decided % 11 == 0:
                pick = "cancel"
            self.store.decide_approval(approval.id, pick, by=who,
                                       note="decided by the soak harness")
            decided += 1
        return decided

    def run(self, ticks: int, advance_seconds: int = 60,
            crash_every: int | None = None,
            decide_every: int | None = None,
            new_task_every: int | None = None) -> SoakReport:
        for n in range(1, ticks + 1):
            self.tick(n)
            if decide_every and n % decide_every == 0:
                self.decide_everything()
            if new_task_every and n % new_task_every == 0:
                self.arrivals += 1
                self.write_task(f"ARRIVED-{self.arrivals}",
                                resources=(f"repo:r{self.arrivals % 3}",))
            self.clock.advance(seconds=advance_seconds)
            if crash_every and n % crash_every == 0:
                self.crash()
        self.report.faults_fired = {name: s.fired
                                    for name, s in self.schedules.items()}
        return self.report


# ---------------------------------------------------------------------------
# Script entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Regente soak run")
    parser.add_argument("--ticks", type=int, default=200)
    parser.add_argument("--tasks", type=int, default=6)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--advance", type=int, default=60,
                        help="seconds of engine time per tick")
    parser.add_argument("--crash-every", type=int, default=25)
    parser.add_argument("--new-task-every", type=int, default=9,
                        help="ticks between new work arriving on the board")
    parser.add_argument("--decide-every", type=int, default=5,
                        help="ticks between the simulated person's decisions")
    parser.add_argument("--root", default="")
    parser.add_argument("--json", default="", help="write per-tick metrics here")
    args = parser.parse_args()

    random.seed(args.seed)
    root = Path(args.root) if args.root else Path(
        __file__).resolve().parent / ".soak"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)

    soak = Soak(root=root, schedules={
        "tasks": Schedule(kind="unavailable", every=7, reason="board outage"),
        "runner_fails": Schedule(kind="error", every=11, reason="runner failure"),
        "worker_dies": Schedule(kind="error", every=13, reason="worker killed"),
    })
    soak.open()
    for i in range(1, args.tasks + 1):
        soak.write_task(f"SOAK-{i}", resources=(f"repo:r{i % 3}",))

    report = soak.run(args.ticks, advance_seconds=args.advance,
                      crash_every=args.crash_every,
                      decide_every=args.decide_every,
                      new_task_every=args.new_task_every)
    soak.close()

    print(report.summary())
    print("faults fired:", report.faults_fired)
    if report.violations:
        print("\nINVARIANT VIOLATIONS")
        for v in report.violations[:40]:
            print("  -", v)
    if args.json:
        Path(args.json).write_text(
            json.dumps([t.as_dict() for t in report.ticks], indent=2),
            encoding="utf-8")
        print(f"\nper-tick metrics written to {args.json}")
    return 1 if report.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
