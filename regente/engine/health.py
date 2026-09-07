# -*- coding: utf-8 -*-
"""What is running, what is stuck, for how long, and why -- read from the disk.

The rule that shapes every line here: **health is computed from persisted state,
never from anything the running process remembers.** If the engine dies at three
in the morning, `regente health` at nine must still say what happened. A report
assembled from in-memory counters would be empty exactly when it is needed, and
an empty report reads like a healthy one.

The second rule, which is the same rule the rest of this engine already lives
by: **absence of information is never success.** Every question can answer
`UNKNOWN`, and `UNKNOWN` is not `OK`. "I could not find the worker, so it must
have finished" is the shape of reasoning this module exists to refuse.

Four levels, and the ordering matters when they are combined:

    OK        -- answered, and the answer is fine
    ATTENTION -- answered, worth a look, the engine is still moving
    UNKNOWN   -- could not be determined; treated as worse than ATTENTION
    STUCK     -- answered, and nothing will progress without a person

`UNKNOWN` sits above `ATTENTION` on purpose. A thing the engine cannot see is
more dangerous than a thing it can see and dislikes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from ..core.model import now
from ..core.states import (ACTIVE, AWAITING_EXTERNAL, TaskState,
                            is_terminus)
from ..core.model import RunState
from ..ports.store import Store


class Level(str, Enum):
    OK = "OK"
    ATTENTION = "ATTENTION"
    UNKNOWN = "UNKNOWN"
    STUCK = "STUCK"

    @property
    def rank(self) -> int:
        return {"OK": 0, "ATTENTION": 1, "UNKNOWN": 2, "STUCK": 3}[self.value]


@dataclass(frozen=True, slots=True)
class Signal:
    """One question, its answer, and the evidence the answer rests on.

    `evidence` is not decoration. A report that says "3 tasks are stuck" and
    cannot name them sends a person to go and look for them, which is the work
    the report was supposed to do.
    """
    question: str
    level: Level
    detail: str
    evidence: tuple[str, ...] = ()

    def render(self) -> str:
        head = f"  [{self.level.value:<9}] {self.question}: {self.detail}"
        return "\n".join([head, *(f"                 - {e}" for e in self.evidence[:8])])


@dataclass(frozen=True, slots=True)
class Health:
    workspace: str
    at: datetime
    signals: tuple[Signal, ...] = ()
    #: Raw numbers, so a reader can form their own opinion about growth.
    measurements: dict[str, int] = field(default_factory=dict)

    @property
    def level(self) -> Level:
        return max((s.level for s in self.signals), key=lambda l: l.rank,
                   default=Level.OK)

    @property
    def healthy(self) -> bool:
        """OK only. `UNKNOWN` is not healthy -- it is unexamined."""
        return self.level is Level.OK

    def of(self, question: str) -> Signal | None:
        return next((s for s in self.signals if s.question == question), None)

    def render(self) -> str:
        lines = [f"health of '{self.workspace}' at {self.at.isoformat()}",
                 f"overall: {self.level.value}", ""]
        lines += [s.render() for s in self.signals]
        if self.measurements:
            lines += ["", "  measurements:"]
            lines += [f"    {k:<24} {v}"
                      for k, v in sorted(self.measurements.items())]
        return "\n".join(lines)


#: How long a task may sit in an ACTIVE state before the engine says so. Active
#: states are the ones something is supposed to be working on; a task that has
#: not moved in this long is either being worked on very slowly or is stuck, and
#: the engine cannot tell which -- so it reports ATTENTION and names it.
IDLE_ACTIVE_SECONDS = 3600

#: How long a task may wait for a person before that is worth reporting. Longer,
#: because a person taking a day is normal and an agent taking a day is not.
IDLE_WAITING_SECONDS = 86_400

#: Event kinds that mean an external provider misbehaved.
PROVIDER_TROUBLE = ("error", "provider_failure", "chamada_provedor_falhou")


def inspect(
    store: Store,
    workspace_id: str,
    workspace_name: str = "",
    areas_root: str | Path | None = None,
    budget_usd: float | None = None,
    max_dispatches: int | None = None,
    when: datetime | None = None,
) -> Health:
    """Answer the thirteen questions. Every answer from a row, or `UNKNOWN`."""
    at = when or now()
    signals: list[Signal] = []
    measurements: dict[str, int] = {}

    runs_active = store.active_runs(workspace_id)
    leases = store.leases(workspace_id)
    tasks = store.tasks(workspace_id)
    live = [l for l in leases if l.expires_at and l.expires_at > at]
    expired = [l for l in leases if l.expires_at and l.expires_at <= at]

    # 1. What is running now?
    signals.append(_running(runs_active, live, at))
    # 2 & 3. What is stopped, and for how long?
    signals.extend(_stalled(store, workspace_id, tasks, at))
    # 5. Expired leases.
    signals.append(_expired_leases(expired, runs_active, at))
    # 6. Interrupted runs.
    signals.append(_interrupted(store, workspace_id))
    # 7. Orphaned work areas.
    signals.append(_orphan_areas(areas_root, runs_active, tasks))
    # 8. Tasks in states with no way out.
    signals.append(_dead_ends(tasks))
    # 9. Budget.
    signals.append(_budget(store, workspace_id, at, budget_usd, max_dispatches))
    # 10. Escalations.
    signals.append(_escalations(store, workspace_id, at))
    # 11. Provider failures.
    signals.append(_provider_failures(store, workspace_id, at))
    # 12. Database growth.
    growth, measurements = _growth(store, workspace_id, at)
    signals.append(growth)
    # 13. Deliveries in flight.
    signals.append(_in_flight(store, workspace_id, tasks, at))

    return Health(workspace=workspace_name or workspace_id, at=at,
                  signals=tuple(signals), measurements=measurements)


# ---------------------------------------------------------------------------

def _age(then: datetime | None, at: datetime) -> str:
    if then is None:
        return "unknown age"
    seconds = max(0, int((at - then).total_seconds()))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}min"
    if seconds < 172_800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


#: How many lease spans a run may exceed before its age is itself the finding.
#: A worker renews its lease while it works, so a run much older than its own
#: lease window means either nobody is renewing (and the lease is stale) or
#: something is renewing for work nobody is doing. Either way the engine cannot
#: call it fine.
RUN_AGE_LEASE_MULTIPLE = 4


def _running(runs, live_leases, at) -> Signal:
    """A run is only genuinely running if something is holding its lease.

    A RUNNING row with no live lease is the signature of a process that died
    without saying so. It is reported UNKNOWN rather than OK, because the engine
    genuinely does not know whether that work finished -- and the next tick's
    recovery, not this report, is what decides.

    A live lease is not enough on its own either. A soak run left a run RUNNING
    with a lease that read as live for 103 consecutive ticks -- four simulated
    days -- because the lease had been stamped from one clock and was being
    checked against another. Health reported OK the whole way through. So the
    age of the run is now a finding in its own right: no legitimate run outlives
    its own lease window several times over, whatever the lease claims.
    """
    if not runs:
        return Signal("running", Level.OK, "nothing is executing")

    owners = {l.owner for l in live_leases}
    spans = [(l.expires_at - l.renewed_at).total_seconds()
             for l in live_leases if l.expires_at and l.renewed_at]
    ceiling = max(spans) * RUN_AGE_LEASE_MULTIPLE if spans else None

    held = [r for r in runs if r.id in owners]
    unheld = [r for r in runs if r.id not in owners]
    overdue = [r for r in held
               if ceiling is not None and r.started_at
               and (at - r.started_at).total_seconds() > ceiling]

    evidence = [f"{r.id} on {r.task_key or r.task_id} since {_age(r.started_at, at)} ago"
                for r in held]

    if unheld:
        return Signal(
            "running", Level.UNKNOWN,
            f"{len(held)} run(s) executing and {len(unheld)} marked RUNNING "
            f"with no live lease; whether that work finished is not known",
            tuple(evidence + [f"{r.id} on {r.task_key or r.task_id}: RUNNING, no live lease, "
                              f"started {_age(r.started_at, at)} ago"
                              for r in unheld]))

    if overdue:
        return Signal(
            "running", Level.STUCK,
            f"{len(overdue)} run(s) have been executing far longer than a lease "
            f"window allows; a live lease on work this old means the lease is "
            f"being kept alive for a worker that is not there",
            tuple(f"{r.id} on {r.task_key or r.task_id}: RUNNING for {_age(r.started_at, at)}, "
                  f"over {int(ceiling)}s of lease window" for r in overdue))

    return Signal("running", Level.OK, f"{len(held)} run(s) executing",
                  tuple(evidence))


def _stalled(store, workspace_id, tasks, at) -> list[Signal]:
    """Questions 2, 3 and 4: what is stopped, how long, and why.

    `updated_at` is the clock here, and it is written by every transition, so it
    measures how long a task has actually sat still.
    """
    idle_active = store.tasks_idle_since(
        workspace_id, at - timedelta(seconds=IDLE_ACTIVE_SECONDS))
    idle_waiting = store.tasks_idle_since(
        workspace_id, at - timedelta(seconds=IDLE_WAITING_SECONDS))

    stuck_active = [(t, u) for t, u in idle_active if t.state in ACTIVE]
    waiting = [(t, u) for t, u in idle_waiting
               if t.state is TaskState.WAITING_HUMAN]

    signals = []
    if stuck_active:
        signals.append(Signal(
            "stalled_work", Level.ATTENTION,
            f"{len(stuck_active)} task(s) have not moved in over "
            f"{IDLE_ACTIVE_SECONDS // 60}min",
            tuple(f"{t.key}: {t.state.value} for {_age(u, at)} "
                  f"(attempts={t.attempts})" for t, u in stuck_active[:8])))
    else:
        signals.append(Signal("stalled_work", Level.OK,
                              "no task is sitting in an active state"))

    if waiting:
        signals.append(Signal(
            "waiting_on_a_person", Level.ATTENTION,
            f"{len(waiting)} task(s) have waited over "
            f"{IDLE_WAITING_SECONDS // 3600}h for a decision",
            tuple(f"{t.key}: paused from "
                  f"{t.paused_at.value if t.paused_at else '?'} for {_age(u, at)}"
                  for t, u in waiting[:8])))
    return signals


def _expired_leases(expired, runs, at) -> Signal:
    if not expired:
        return Signal("expired_leases", Level.OK, "none")
    owners = {r.id for r in runs}
    orphaned = [l for l in expired if l.owner not in owners]
    return Signal(
        "expired_leases",
        Level.ATTENTION if not orphaned else Level.STUCK,
        f"{len(expired)} expired; {len(orphaned)} belong to no active run and "
        f"no tick has cleared them",
        tuple(f"{l.resource} held by {l.owner}, expired {_age(l.expires_at, at)} ago"
              for l in expired[:8]))


def _interrupted(store, workspace_id) -> Signal:
    interrupted = store.runs_in_state(workspace_id, RunState.INTERRUPTED.value)
    if not interrupted:
        return Signal("interrupted_runs", Level.OK, "none")
    return Signal(
        "interrupted_runs", Level.ATTENTION,
        f"{len(interrupted)} run(s) were interrupted and recorded as such",
        tuple(f"{r.id} on {r.task_key or r.task_id}: {r.reason[:80]}" for r in interrupted[:8]))


def _orphan_areas(areas_root, runs, tasks) -> Signal:
    """Directories on disk that no run and no task account for.

    Reported, never deleted. A work area holds uncommitted work, and an engine
    that tidies away a directory it cannot explain is an engine that throws away
    the one copy of something.
    """
    if areas_root is None:
        return Signal("orphan_areas", Level.UNKNOWN,
                      "no work-area root was given, so nothing was examined")
    root = Path(areas_root)
    if not root.is_dir():
        return Signal("orphan_areas", Level.OK,
                      f"no work-area root exists at {root}")

    known = {r.workspace_path for r in runs if r.workspace_path}
    keys = {t.key for t in tasks} | {_sanitized(t.key) for t in tasks}
    orphans = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if str(child) in known or child.name in keys:
            continue
        orphans.append(child)

    if not orphans:
        return Signal("orphan_areas", Level.OK, "every work area has an owner")
    return Signal(
        "orphan_areas", Level.ATTENTION,
        f"{len(orphans)} work area(s) belong to no known task or run; they are "
        f"reported and never deleted, because they may hold the only copy of "
        f"uncommitted work",
        tuple(str(p) for p in orphans[:8]))


def _sanitized(key: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in key)


def _dead_ends(tasks) -> Signal:
    """Tasks parked where the scheduler will never pick them up again.

    The most dangerous condition in this whole report, because it is invisible:
    the queue looks busy, the engine looks alive, and a task will simply never
    be touched again. It was a real defect once -- recovery returned tasks to an
    active state that no tick dispatches -- so it is checked by asking the state
    machine, not by listing states somebody believed were safe.
    """
    stranded = []
    for t in tasks:
        if t.state is TaskState.WAITING_HUMAN:
            # Legitimate: it is waiting for a person, and a decision moves it.
            if t.paused_at is None:
                stranded.append((t, "WAITING_HUMAN with no record of where it "
                                    "paused; a decision has nowhere to send it"))
            continue
        if is_terminus(t.state):
            stranded.append((
                t, f"{t.state.value} is an active state this engine has no "
                   f"stage to advance; nothing will pick it up again"))
    if not stranded:
        return Signal("dead_end_tasks", Level.OK,
                      "every task has a route out of its state")
    return Signal("dead_end_tasks", Level.STUCK,
                  f"{len(stranded)} task(s) cannot be moved by any tick",
                  tuple(f"{t.key}: {why}" for t, why in stranded[:8]))


def _in_flight(store, workspace_id, tasks, at) -> Signal:
    """Changes already on a remote, waiting on somebody else.

    These are the tasks nobody in this engine is working on and nobody is
    supposed to be: `AWAITING_EXTERNAL` means the work left and the answer has
    not come back. They are the easiest thing in the system to lose sight of --
    no run, no lease, no worker, nothing that expires and complains.

    Two failures are asked about separately, because they need different people:
    a delivery with no record of what it waits for is broken, and a delivery
    that has been waiting a long time is merely slow.
    """
    waiting = [t for t in tasks if t.state in AWAITING_EXTERNAL]
    if not waiting:
        return Signal("deliveries_in_flight", Level.OK, "none")

    lost, slow, evidence = [], [], []
    for task in waiting:
        rows = [r for r in store.deliveries(workspace_id, task.key)
                if r.get("pr_number")]
        if not rows:
            lost.append(task)
            evidence.append(f"{task.key}: {task.state.value} with no delivery "
                            f"recorded; nothing says what it waits for")
            continue
        row = rows[-1]
        asked = int(row.get("ci_observations") or 0)
        age = _age(_since(row.get("pr_opened_at")), at)
        state = row.get("ci_state") or "not yet read"
        evidence.append(f"{task.key}: PR #{row['pr_number']} for "
                        f"{row['commit_sha'][:12]}, CI {state}, asked {asked}x, "
                        f"open {age}")
        if state in ("PENDING", "UNAVAILABLE") and asked >= 10:
            slow.append(task)

    if lost:
        return Signal("deliveries_in_flight", Level.STUCK,
                      f"{len(lost)} delivery(ies) wait on nothing recorded",
                      tuple(evidence[:8]))
    level = Level.ATTENTION if slow else Level.OK
    return Signal("deliveries_in_flight", level,
                  f"{len(waiting)} delivery(ies) in flight"
                  + (f", {len(slow)} with no answer yet" if slow else ""),
                  tuple(evidence[:8]))


def _since(raw: str | None):
    """Parse a stored timestamp, or admit it cannot be read."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def _budget(store, workspace_id, at, budget_usd, max_dispatches) -> Signal:
    day = at.strftime("%Y-%m-%d")
    dispatched = store.dispatch_count(workspace_id, day)
    spent = sum(r.cost_usd for r in store.runs_in_state(
        workspace_id, RunState.SUCCEEDED.value, limit=2000))

    parts = [f"{dispatched} dispatch(es) today"]
    level = Level.OK
    if max_dispatches is not None:
        parts[0] += f" of {max_dispatches}"
        if dispatched >= max_dispatches:
            level = Level.ATTENTION
    if budget_usd is not None:
        parts.append(f"US$ {spent:.2f} of {budget_usd:.2f} spent")
        if spent >= budget_usd:
            level = Level.ATTENTION
    if max_dispatches is None and budget_usd is None:
        return Signal("budget", Level.UNKNOWN,
                      f"{dispatched} dispatch(es) today and no ceiling was "
                      f"given, so nothing can be said about whether it is spent")
    return Signal("budget", level, "; ".join(parts))


def _escalations(store, workspace_id, at) -> Signal:
    open_items = store.open_approvals(workspace_id)
    if not open_items:
        return Signal("escalations", Level.OK, "no decision is waiting")
    oldest = store.oldest_open_approval(workspace_id)
    return Signal(
        "escalations", Level.ATTENTION,
        f"{len(open_items)} decision(s) waiting, oldest {_age(oldest, at)}",
        tuple(f"{a.id}: {a.what_happened[:80]}" for a in open_items[:8]))


def _provider_failures(store, workspace_id, at) -> Signal:
    """Recurring provider trouble, counted from the event log.

    Read from events rather than from a counter, because the event is written
    on the path that actually made the call -- a counter is one refactor away
    from silently not counting.
    """
    recent = store.event_counts(workspace_id, at - timedelta(hours=24))
    failures = sum(n for kind, n in recent.items()
                   if any(mark in kind for mark in PROVIDER_TROUBLE))
    if failures == 0:
        return Signal("provider_failures", Level.OK, "none in the last 24h")
    ticks = recent.get("tick_inicio", 0)
    detail = f"{failures} provider failure(s) in the last 24h"
    if ticks:
        detail += f" across {ticks} tick(s)"
    return Signal("provider_failures",
                  Level.ATTENTION if failures < max(3, ticks // 2) else Level.STUCK,
                  detail)


def _growth(store, workspace_id, at) -> tuple[Signal, dict[str, int]]:
    """Is the database growing abnormally? Answerable only with a comparison.

    The event log carries its own timestamps, so today's rate can be compared
    with yesterday's without keeping a separate history table. With less than a
    day of data there is nothing to compare against, and the honest answer is
    `UNKNOWN` -- not `OK`.
    """
    counts = store.table_counts(workspace_id)
    measurements = {f"rows.{k}": v for k, v in counts.items()}
    sizes = store.database_bytes()
    measurements["bytes.data"] = sizes["data"]
    measurements["bytes.write_ahead_log"] = sizes["wal"]

    today = sum(store.event_counts(workspace_id, at - timedelta(hours=24)).values())
    yesterday = sum(store.event_counts(
        workspace_id, at - timedelta(hours=48), at - timedelta(hours=24)).values())
    measurements["events.last_24h"] = today
    measurements["events.previous_24h"] = yesterday

    if yesterday == 0:
        return (Signal("database_growth", Level.UNKNOWN,
                       f"{today} event(s) in the last 24h and no earlier day to "
                       f"compare against; growth cannot be called normal or not"),
                measurements)
    # The write-ahead log is churn, not growth: it is folded into the data file
    # by a checkpoint and shrinks back. Reporting it as growth would raise an
    # alarm about a file that is about to get smaller.
    if sizes["wal"] > 32 * 1024 * 1024:
        return (Signal("database_growth", Level.ATTENTION,
                       f"the write-ahead log is {sizes['wal'] // 1024 // 1024}MB "
                       f"and has not been checkpointed"), measurements)
    ratio = today / yesterday
    if ratio > 3:
        return (Signal("database_growth", Level.ATTENTION,
                       f"{today} events in 24h against {yesterday} the day "
                       f"before ({ratio:.1f}x)"), measurements)
    return (Signal("database_growth", Level.OK,
                   f"{today} events in 24h against {yesterday} the day before"),
            measurements)
