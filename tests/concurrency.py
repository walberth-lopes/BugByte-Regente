# -*- coding: utf-8 -*-
"""Real processes, one database, genuine contention.

Threads in one interpreter share a connection pool, a GIL and a heap. None of
that is what production looks like, and none of it exercises the reason leases,
`BEGIN IMMEDIATE` and the `(workspace_id, resource)` key exist. So this spawns
separate interpreters with separate memory and independent lifetimes, points
them at one SQLite file, and lets them fight.

Every worker appends one JSON line per decision to a shared ledger: which
process, which run, which resource, won or refused, and when. The ledger is the
evidence. The database says what the final state is; the ledger says whether two
processes ever believed the same thing at the same time, which is the question
this milestone actually asks and which no final state can answer.

Run it:

    python tests/concurrency.py --workers 3 --seconds 8 --tasks 6

Exits non-zero if any invariant broke.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))


def ledger_line(path: Path, **fields) -> None:
    """Append one event. Opened and closed per line on purpose.

    A held file handle buffers, and a buffered ledger loses exactly the last
    few lines -- the ones written just before a `SIGKILL`, which are the ones
    that matter. Appending in `a` mode with a single `write` of a line under the
    pipe buffer is atomic enough on both platforms for this purpose.
    """
    fields["pid"] = os.getpid()
    fields["wall"] = time.time()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(fields, default=str) + "\n")


# ---------------------------------------------------------------------------
# The worker process
# ---------------------------------------------------------------------------

def run_worker(root: Path, worker_id: str, seconds: float, tasks: int,
               lease_seconds: int, slow: float) -> int:
    """Tick until told to stop. Everything it decides goes to the ledger."""
    from regente.adapters.notify.console import Console
    from regente.adapters.tasks.filesystem import FilesystemTasks
    from regente.adapters.workspace.local import IsolatedDirectory
    from regente.core.model import Workspace
    from regente.core.policy import AutonomyLevel, PolicyEngine
    from regente.core.risk import RiskEngine
    from regente.core.scheduling import Limits
    from regente.engine.gate import Gate
    from regente.engine.orchestrator import Orchestrator
    from regente.engine.store_sqlite import SqliteStore

    from concurrency import SlowAgent

    ledger = root / "ledger.jsonl"
    store = SqliteStore(root / "regente.db")
    store.migrate()
    workspace = Workspace(id="wks_race", client_id="cli", name="race",
                          max_autonomy=AutonomyLevel.L3)
    store.save_workspace(workspace)

    risk = RiskEngine()
    orchestrator = Orchestrator(
        store=store, workspace=workspace,
        tasks_provider=FilesystemTasks(root / "tasks"),
        area_provider=IsolatedDirectory(root / "areas"),
        runner=SlowAgent(ledger=ledger, worker=worker_id, seconds=slow),
        gate=Gate(store=store, policy=PolicyEngine.from_config([]), risk=risk),
        risk=risk,
        limits=Limits(max_workers=4, max_dispatches_per_day=10_000),
        notificador=Console(journal=root / f"journal-{worker_id}.log"),
        lease_seconds=lease_seconds)

    ledger_line(ledger, event="worker_started", worker=worker_id)
    deadline = time.monotonic() + seconds
    n = 0
    while time.monotonic() < deadline:
        n += 1
        try:
            outcome = orchestrator.tick()
            ledger_line(ledger, event="tick", worker=worker_id, n=n,
                        dispatched=list(outcome.dispatched),
                        recovered=list(outcome.recovered),
                        errors=list(outcome.errors))
        except Exception as e:                       # noqa: BLE001
            ledger_line(ledger, event="tick_failed", worker=worker_id, n=n,
                        error=f"{type(e).__name__}: {e}"[:300])
        # Decide anything waiting, so the queue keeps moving and the workers
        # keep having something to fight over.
        for approval in store.open_approvals(workspace.id):
            try:
                store.decide_approval(approval.id, "investigar",
                                      per=f"operator-{worker_id}")
            except Exception:                        # noqa: BLE001
                pass                                 # another worker got there
        time.sleep(random.uniform(0.01, 0.05))

    ledger_line(ledger, event="worker_stopped", worker=worker_id, ticks=n)
    store.close()
    return 0


@dataclass(slots=True)
class SlowAgent:
    """An agent that takes real time, and says so in the ledger.

    Duration is the whole point: a run that finishes instantly never overlaps
    another, so contention would never happen and every lease would be
    uncontested. Slowness is what makes the race real.

    It also records the exact window it was inside a mission. Two overlapping
    windows for one task, from two processes, is duplicate execution -- and it
    is visible here even if the database ends up looking tidy.
    """
    ledger: Path
    worker: str
    seconds: float = 0.3
    name: str = "slow"
    cancelled: set = field(default_factory=set)

    def availability(self):
        from regente.ports.agent import (AgentAvailability, AuthMode, Check)
        return AgentAvailability(
            auth_mode=AuthMode.NONE, adapter=self.name,
            executable=Check.yes("in-process"), protocol=Check.yes(),
            authentication=Check.yes("none needed"), agent=Check.yes())

    def describe(self) -> dict[str, str]:
        return {"adapter": self.name}

    def run(self, mission):
        from regente.ports.agent import Claim, Outcome, ProcessStatus

        ledger_line(self.ledger, event="mission_start", worker=self.worker,
                    task=mission.task_key, run=mission.run_id)
        # Slept in slices so a cancellation can land between them. A real
        # process agent is killed outright; an in-process one can only be
        # cooperative, and this is what cooperative looks like.
        remaining = self.seconds
        while remaining > 0 and mission.run_id not in self.cancelled:
            step = min(0.02, remaining)
            time.sleep(step)
            remaining -= step
        stopped = mission.run_id in self.cancelled
        ledger_line(self.ledger, event="mission_end", worker=self.worker,
                    task=mission.task_key, run=mission.run_id,
                    cancelled=stopped)
        if stopped:
            self.cancelled.discard(mission.run_id)
            return Outcome(status=ProcessStatus.ERROR,
                           summary="cancelled: this worker lost its lease")
        return Outcome(status=ProcessStatus.FINISHED, claim=Claim.COMPLETE,
                       summary=f"done by {self.worker}")

    def cancel(self, run_id: str) -> None:
        """Stop the mission. Called when the engine sees ownership lost."""
        self.cancelled.add(run_id)
        ledger_line(self.ledger, event="cancelled", worker=self.worker,
                    run=run_id)


# ---------------------------------------------------------------------------
# Analysis: what the ledger and the database say together
# ---------------------------------------------------------------------------

@dataclass
class Findings:
    counters: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    violations: list[str] = field(default_factory=list)

    def count(self, name: str, by: int = 1) -> None:
        self.counters[name] += by

    def broke(self, what: str) -> None:
        self.violations.append(what)

    def render(self) -> str:
        rows = [f"  {k:<28} {v}" for k, v in sorted(self.counters.items())]
        head = "metrics\n" + "\n".join(rows)
        if not self.violations:
            return head + "\n\nno invariant violated"
        return (head + f"\n\n{len(self.violations)} INVARIANT VIOLATION(S)\n  - "
                + "\n  - ".join(self.violations[:40]))


def analyse(root: Path) -> Findings:
    from regente.core.model import RunState
    from regente.engine.store_sqlite import SqliteStore

    found = Findings()
    ledger = root / "ledger.jsonl"
    events = []
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass          # a line torn in half by a kill; not a finding

    for e in events:
        found.count(f"event.{e.get('event', '?')}")
    found.count("workers", len({e.get("worker") for e in events
                                if e.get("event") == "worker_started"}))
    for e in events:
        if e.get("event") == "tick":
            found.count("dispatches", len(e.get("dispatched", [])))
            found.count("recoveries", len(e.get("recovered", [])))
        if e.get("event") == "cancelled":
            found.count("missions_cancelled")
        if e.get("event") == "tick" and e.get("errors"):
            for err in e["errors"]:
                if "ownership lost" in err:
                    found.count("stale_refusals")
                elif "locked" in err.lower():
                    found.count("database_locked_in_tick")
        if e.get("event") == "tick_failed":
            found.count("tick_failures")
            if "locked" in str(e.get("error", "")).lower():
                found.count("unhandled_database_locked")
                found.broke(f"a tick died on a locked database: {e['error'][:120]}")

    # --- duplicate execution, measured from the engine's own rows ----------
    #
    # The first version timed this from the ledger, and the ledger lied. Every
    # line opens and closes a file, and with four processes hammering one path
    # a single write took over a second -- so a mission that slept for 150ms
    # measured as a 1.9s window and "overlapped" its own successor. The
    # instrument was reporting its own latency as concurrency.
    #
    # `runs.started_at` and `runs.ended_at` are written by the engine, on the
    # path that actually did the work, with no ledger in between. A run still
    # RUNNING belongs to a process that was killed inside it; its window closes
    # when that process last spoke, because a dead process executes nothing.
    from regente.core.model import RunState
    from regente.engine.store_sqlite import SqliteStore

    last_seen: dict[str, float] = {}
    for e in events:
        worker = e.get("worker")
        if worker:
            last_seen[worker] = max(last_seen.get(worker, 0.0),
                                    float(e.get("wall", 0.0)))
    run_to_worker = {e["run"]: e.get("worker")
                     for e in events if e.get("event") == "mission_start"
                     and e.get("run")}

    # --- the database's own account ----------------------------------------
    store = SqliteStore(root / "regente.db")
    store.migrate()
    try:
        at = datetime.now(timezone.utc)

        windows: dict[str, list[tuple[float, float, str, str]]] = defaultdict(list)
        for run in store.runs_in_state("wks_race", RunState.SUCCEEDED.value) \
                + store.runs_in_state("wks_race", RunState.FAILED.value) \
                + store.runs_in_state("wks_race", RunState.INTERRUPTED.value) \
                + store.active_runs("wks_race"):
            if run.started_at is None:
                continue
            worker = run_to_worker.get(run.id, run.id[:8])
            began = run.started_at.timestamp()
            if run.ended_at is not None:
                finished = run.ended_at.timestamp()
            else:
                finished = last_seen.get(worker or "", began)
                found.count("runs_ended_by_a_kill")
            windows[run.task_id].append((began, max(began, finished),
                                         worker or "?", run.id))

        for task_id, spans in windows.items():
            found.count("executions", len(spans))
            spans.sort()
            for i in range(len(spans) - 1):
                a_start, a_end, a_worker, a_run = spans[i]
                b_start, b_end, b_worker, b_run = spans[i + 1]
                if b_start < a_end and a_worker != b_worker:
                    found.count("duplicate_execution")
                    found.broke(
                        f"task {task_id} executed at once by {a_worker}"
                        f"({a_run[:12]}) and {b_worker}({b_run[:12]}): "
                        f"{a_start:.3f}-{a_end:.3f} overlaps {b_start:.3f}")

        leases = store.leases("wks_race")
        found.count("leases_now", len(leases))
        found.count("leases_expired_now",
                    sum(1 for l in leases if l.expires_at and l.expires_at <= at))

        by_resource: dict[str, set[str]] = defaultdict(set)
        for l in leases:
            by_resource[l.resource].add(l.owner)
        for resource, owners in by_resource.items():
            if len(owners) > 1:
                found.count("duplicate_ownership")
                found.broke(f"resource '{resource}' is held by {len(owners)} "
                            f"owners at once: {sorted(owners)}")

        runs = store.active_runs("wks_race")
        found.count("runs_still_running", len(runs))
        per_task: dict[str, list[str]] = defaultdict(list)
        for r in runs:
            per_task[r.task_id].append(r.id)
        for task_id, run_ids in per_task.items():
            if len(run_ids) > 1:
                found.count("duplicate_ownership")
                found.broke(f"task {task_id} has {len(run_ids)} active runs: "
                            f"{run_ids}")

        # The engine's own record of stale workers and contention, read from
        # the event log rather than from anything a process remembered.
        recent = store.event_counts("wks_race",
                                    datetime(2000, 1, 1, tzinfo=timezone.utc))
        for kind in ("posse_perdida", "descoberta_concorrente", "recuperada",
                     "adiada", "despachada"):
            if kind in recent:
                found.count(f"engine.{kind}", recent[kind])

        counts = store.table_counts("wks_race")
        for name, value in counts.items():
            found.count(f"rows.{name}", value)

        from soak import check_invariants
        for problem in check_invariants(store, "wks_race", at, root / "areas"):
            found.broke(f"state: {problem}")
    finally:
        store.close()
    return found


# ---------------------------------------------------------------------------
# The coordinator
# ---------------------------------------------------------------------------

def write_tasks(root: Path, count: int, resources: int) -> None:
    directory = root / "tasks"
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(1, count + 1):
        # Deliberately few distinct resources, so tasks collide.
        held = [f"repo:r{i % max(1, resources)}"]
        if i % 4 == 0 and resources > 1:
            # Some tasks need TWO resources: the shape that can deadlock.
            held.append(f"repo:r{(i + 1) % resources}")
        body = [f"key: RACE-{i}", f"title: contended work {i}", "status: TO DO",
                "resources:"] + [f"  - {r}" for r in held]
        (directory / f"RACE-{i}.yaml").write_text("\n".join(body) + "\n",
                                                  encoding="utf-8")


def campaign(args) -> int:
    """Run the same scenario many times and aggregate.

    A race that appears once in fifty runs is still a race, and a single green
    run says almost nothing about it. Repetition is the only instrument
    available for the failures that depend on scheduling luck.
    """
    total: dict[str, int] = defaultdict(int)
    violations: list[str] = []
    base = Path(args.root) if args.root else HERE / ".race"

    for round_number in range(1, args.repeat + 1):
        root = base.parent / f"{base.name}-{round_number}"
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        (root / "areas").mkdir(parents=True)
        write_tasks(root, args.tasks, args.resources)

        children = [subprocess.Popen(
            [sys.executable, str(Path(__file__)), "worker",
             "--root", str(root), "--id", f"w{i}",
             "--seconds", str(args.seconds), "--tasks", str(args.tasks),
             "--lease-seconds", str(args.lease_seconds),
             "--slow", str(args.slow)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for i in range(args.workers)]

        if args.kill_at:
            time.sleep(args.kill_at)
            children[round_number % len(children)].kill()
            total["workers_killed"] += 1
        for child in children:
            try:
                child.wait(timeout=args.seconds + 60)
            except subprocess.TimeoutExpired:
                child.kill()

        found = analyse(root)
        for key, value in found.counters.items():
            if key.startswith("rows."):
                continue
            total[key] += value
        violations += [f"round {round_number}: {v}" for v in found.violations]
        shutil.rmtree(root, ignore_errors=True)
        print(f"  round {round_number:3}/{args.repeat}: "
              f"{found.counters.get('dispatches', 0)} dispatches, "
              f"{len(found.violations)} violation(s)", flush=True)

    print()
    print("aggregate over", args.repeat, "rounds")
    for key, value in sorted(total.items()):
        print(f"  {key:<28} {value}")
    for name in ("duplicate_execution", "duplicate_ownership",
                 "unhandled_database_locked"):
        print(f"  {name:<28} {total.get(name, 0)}")
    if violations:
        print()
        print(f"{len(violations)} INVARIANT VIOLATION(S)")
        for v in violations[:30]:
            print("  -", v)
        return 1
    print()
    print("no invariant violated in any round")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="real multi-process contention")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--tasks", type=int, default=6)
    parser.add_argument("--resources", type=int, default=2)
    parser.add_argument("--lease-seconds", type=int, default=2)
    parser.add_argument("--slow", type=float, default=0.3)
    parser.add_argument("--kill-at", type=float, default=0.0,
                        help="seconds in, SIGKILL one worker (0 = never)")
    parser.add_argument("--root", default="")
    parser.add_argument("--repeat", type=int, default=1,
                        help="run the whole scenario N times and aggregate")
    args = parser.parse_args()

    if args.repeat > 1:
        return campaign(args)

    root = Path(args.root) if args.root else HERE / ".race"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    (root / "areas").mkdir(parents=True)
    write_tasks(root, args.tasks, args.resources)

    children = []
    for i in range(args.workers):
        children.append(subprocess.Popen(
            [sys.executable, str(Path(__file__)), "worker",
             "--root", str(root), "--id", f"w{i}",
             "--seconds", str(args.seconds), "--tasks", str(args.tasks),
             "--lease-seconds", str(args.lease_seconds),
             "--slow", str(args.slow)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace"))

    killed = []
    if args.kill_at:
        time.sleep(args.kill_at)
        victim = children[0]
        victim.kill()
        killed.append(victim)
        ledger_line(root / "ledger.jsonl", event="coordinator_killed",
                    worker="w0")

    for child in children:
        try:
            child.wait(timeout=args.seconds + 60)
        except subprocess.TimeoutExpired:
            child.kill()

    stderrs = [c.stderr.read() for c in children if c.stderr]
    found = analyse(root)
    found.count("workers_killed", len(killed))
    print(found.render())

    noisy = [s for s in stderrs if s and "Traceback" in s]
    if noisy:
        print(f"\n{len(noisy)} worker(s) died with a traceback:")
        print(noisy[0][-1200:])
        found.broke("a worker process crashed with a traceback")

    return 1 if found.violations else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        p = argparse.ArgumentParser()
        p.add_argument("worker")
        p.add_argument("--root", required=True)
        p.add_argument("--id", required=True)
        p.add_argument("--seconds", type=float, default=8.0)
        p.add_argument("--tasks", type=int, default=6)
        p.add_argument("--lease-seconds", type=int, default=2)
        p.add_argument("--slow", type=float, default=0.3)
        a = p.parse_args()
        raise SystemExit(run_worker(Path(a.root), a.id, a.seconds, a.tasks,
                                    a.lease_seconds, a.slow))
    raise SystemExit(main())
