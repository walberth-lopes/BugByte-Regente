# -*- coding: utf-8 -*-
"""Fault injection at the real boundaries.

Everything here **wraps** a real port implementation rather than replacing it.
That distinction is the whole design: a mock that returns a canned failure tests
the mock's idea of a failure, while a wrapper that lets the real adapter run and
then interrupts it tests the engine's handling of the real thing. The engine
under test is identical either way; only the world misbehaves.

The faults are scheduled, not random. A soak run that fails differently every
time cannot be used to prove a fix -- and "it passed the second time" is exactly
the reasoning this project keeps refusing.

Two families:

  **Provider faults** -- timeouts, outages, rate limits, intermittent errors.
  They arrive as the same exception types the real transports raise, so the
  engine cannot tell an injected outage from a real one.

  **Process faults** -- a worker that dies mid-run, an orchestrator killed
  between a mutation and its confirmation. These do not raise; they leave the
  world in the state a dead process leaves it in, and then stop. Recovery has to
  work it out from what is on disk, which is the point.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from regente.adapters.tasks.transport import (AuthFailure, ProviderUnavailable,
                                              RateLimited)
from regente.ports import AdapterError
from regente.ports.agent import AgentRunner, Mission, Outcome, ProcessStatus
from regente.ports.tasks import ExternalTask, TaskProvider

#: The failures a real provider actually produces. Named so a test asks for a
#: condition rather than for an exception class -- the engine is supposed to
#: distinguish these, and a test that passed the class directly would be
#: asserting on its own choice.
FAILURES: dict[str, Callable[[str], Exception]] = {
    "unavailable": lambda why: ProviderUnavailable(f"injected outage: {why}"),
    "timeout": lambda why: ProviderUnavailable(f"injected timeout: {why}"),
    "rate_limit": lambda why: RateLimited(f"injected rate limit: {why}"),
    "auth": lambda why: AuthFailure(f"injected credential refusal: {why}"),
    "error": lambda why: AdapterError(f"injected adapter error: {why}"),
}


@dataclass
class Schedule:
    """When a fault fires, deterministically.

    `at` fires on exactly those call numbers; `every` fires periodically. Both
    count calls rather than wall time, because a soak run has to reproduce.
    """
    kind: str = "unavailable"
    at: frozenset[int] = frozenset()
    every: int | None = None
    reason: str = "scheduled"
    calls: int = 0
    fired: int = 0

    def should_fire(self) -> bool:
        self.calls += 1
        if self.calls in self.at:
            return True
        return self.every is not None and self.calls % self.every == 0

    def fire(self) -> Exception:
        self.fired += 1
        return FAILURES[self.kind](f"{self.reason} (call {self.calls})")


@dataclass
class FlakyTasks(TaskProvider):
    """A real task provider that sometimes cannot be reached.

    `list_tasks` is the call every tick makes, so this is where an outage is
    felt. The engine must treat the failure as "I do not know what the board
    says" and never as "the board is empty" -- an empty board would cancel work
    that is perfectly fine.
    """
    inner: TaskProvider
    schedule: Schedule = field(default_factory=Schedule)
    name: str = "flaky-tasks"

    def describe(self) -> dict[str, str]:
        return {**self.inner.describe(), "wrapped_by": self.name}

    def verify(self) -> None:
        self.inner.verify()

    def list_tasks(self) -> list[ExternalTask]:
        if self.schedule.should_fire():
            raise self.schedule.fire()
        return self.inner.list_tasks()

    def get_task(self, key: str) -> ExternalTask | None:
        return self.inner.get_task(key)

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)


@dataclass
class SlowTasks(TaskProvider):
    """A provider that answers, eventually. Real seconds, on purpose.

    Used sparingly: a soak run cannot afford many, but a timeout that is only
    ever simulated never exercises the code that actually waits.
    """
    inner: TaskProvider
    seconds: float = 0.0
    schedule: Schedule = field(default_factory=Schedule)
    name: str = "slow-tasks"

    def describe(self) -> dict[str, str]:
        return {**self.inner.describe(), "wrapped_by": self.name}

    def verify(self) -> None:
        self.inner.verify()

    def list_tasks(self) -> list[ExternalTask]:
        if self.schedule.should_fire():
            time.sleep(self.seconds)
        return self.inner.list_tasks()

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)


@dataclass
class DyingRunner(AgentRunner):
    """A runner whose worker dies without saying anything.

    It does NOT raise. That is the difference between this and a runner that
    fails: an exception is a report, and a dead worker files no report. The run
    row stays RUNNING, the lease stays held until it expires, and the engine has
    to work out from persisted state that nobody is coming back.

    `deaths` records which calls died, so a soak run can assert that recovery
    happened as many times as death did.
    """
    inner: AgentRunner
    schedule: Schedule = field(default_factory=Schedule)
    name: str = "dying-runner"
    deaths: list[str] = field(default_factory=list)

    class WorkerDied(BaseException):
        """Deliberately not an `Exception`.

        The orchestrator catches `Exception` around the runner and turns it into
        a failed run -- which is correct for a worker that crashed and reported
        it. A worker killed by the operating system reports nothing, so this
        escapes that handler and leaves the state exactly as a `kill -9` would.
        """

    def availability(self):
        return self.inner.availability()

    def describe(self) -> dict[str, str]:
        return {**self.inner.describe(), "wrapped_by": self.name}

    def run(self, mission: Mission) -> Outcome:
        if self.schedule.should_fire():
            self.schedule.fired += 1
            self.deaths.append(mission.run_id)
            raise self.WorkerDied(f"worker for {mission.task_key} was killed")
        return self.inner.run(mission)


@dataclass
class FailingRunner(AgentRunner):
    """A runner that fails and says so. The ordinary kind of failure."""
    inner: AgentRunner
    schedule: Schedule = field(default_factory=Schedule)
    name: str = "failing-runner"

    def availability(self):
        return self.inner.availability()

    def describe(self) -> dict[str, str]:
        return {**self.inner.describe(), "wrapped_by": self.name}

    def run(self, mission: Mission) -> Outcome:
        if self.schedule.should_fire():
            self.schedule.fired += 1
            return Outcome(status=ProcessStatus.ERROR,
                           summary=f"injected runner failure on "
                                   f"{mission.task_key}")
        return self.inner.run(mission)


@dataclass
class HalfWrittenStore:
    """A store whose write fails after the mutation and before the confirmation.

    Wraps a real store and lets one named method run to completion and then
    raise. It exists to answer a specific question: when the process dies in
    that window, does the engine come back believing the write happened, or
    believing it did not, or does it check?

    The wrapper is deliberately narrow -- one method, one shot. A general
    "fail randomly anywhere" wrapper produces failures nobody can reason about,
    and a failure nobody can reason about cannot be turned into a fix.
    """
    inner: Any
    method: str = ""
    after_calls: int = 1
    calls: int = 0
    fired: bool = False

    def __getattr__(self, item: str) -> Any:
        attribute = getattr(self.inner, item)
        if item != self.method or self.fired:
            return attribute

        def wrapped(*args, **kwargs):
            self.calls += 1
            result = attribute(*args, **kwargs)
            if self.calls >= self.after_calls:
                self.fired = True
                raise RuntimeError(
                    f"injected crash after '{self.method}' committed and before "
                    f"the caller could act on it")
            return result

        return wrapped


@dataclass
class FrozenRenewal:
    """A store whose lease renewals silently fail for this worker.

    Simulates the process that is alive but not running: a long garbage
    collection, a swapped-out page, a suspended VM, a thread that never gets
    scheduled. From the outside it is indistinguishable from a healthy worker
    right up until its lease expires and somebody else takes the resource.

    This is the fault that produces a STALE worker -- alive, still holding a
    mission, and no longer the owner. Killing a process cannot produce it,
    because a dead process takes no further action. Only a live one can act
    without the right to.
    """
    inner: Any
    frozen: bool = True
    refusals: int = 0

    def renew_lease(self, *args, **kwargs) -> bool:
        if self.frozen:
            self.refusals += 1
            return False
        return self.inner.renew_lease(*args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)


@dataclass
class StolenLease:
    """A store that lets another owner take a lease out from under a run.

    Renewal keeps succeeding -- so the heartbeat notices nothing -- and the
    lease is quietly reassigned. It exists to test the SECOND guard on its own:
    the ownership check immediately before the write, which must catch the loss
    even when the heartbeat was perfectly happy.

    Two guards, tested separately, because a test that only exercised both
    together could not tell which one was holding.
    """
    inner: Any
    steal_for: str = ""
    thief: str = "run_thief"
    stolen: list[str] = field(default_factory=list)

    def renew_lease(self, resource: str, owner: str, *args, **kwargs) -> bool:
        if self.steal_for and owner == self.steal_for:
            return True          # the heartbeat is told everything is fine
        return self.inner.renew_lease(resource, owner, *args, **kwargs)

    def steal(self, resource: str, workspace_id: str, seconds: int = 300,
              when=None) -> None:
        """Hand the lease to somebody else, the way recovery legitimately does.

        `acquire_lease` refuses to take a LIVE lease -- correctly -- so a steal
        that only called it would quietly do nothing and the test would pass
        while proving the opposite of what it claimed. The realistic sequence is
        the one recovery performs: the previous holder is released, and the next
        worker takes it.
        """
        self.inner.release_lease(resource, self.steal_for or "", workspace_id)
        taken = self.inner.acquire_lease(resource, self.thief, workspace_id,
                                         seconds, when=when)
        if taken is None:
            raise AssertionError(f"could not reassign '{resource}'")
        self.stolen.append(resource)

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)
