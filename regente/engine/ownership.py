# -*- coding: utf-8 -*-
"""The right to act, revalidated at the moment of acting.

A lease stops two workers taking the same resource at the same instant. It does
not stop the first worker from *carrying on* after it stopped being the owner,
and those are different problems with different fixes.

The sequence that made this necessary, observed on the first real contention
run of three processes against one database:

    worker A acquires repo:r1, dispatches RACE-3, starts a mission
    A's mission takes longer than the lease
    the lease expires; recovery returns RACE-3 to the queue
    worker B picks RACE-3 up
    A's mission finishes and A writes its result

A was refused only because `READY -> TESTING` happens to be an illegal
transition, so the state machine caught it by accident. Had the task been at
`IMPLEMENTING` under B -- the ordinary case -- the write would have been
perfectly legal and A would have silently overwritten B's work.

So possession is checked twice: once to start, and again immediately before any
write made on the run's behalf. Between those two moments the world is allowed to
have changed, and the second check is the only thing that notices.

Renewal is the other half. A worker that renews while it works keeps its lease
alive and is never wrongly declared dead; a worker that stops renewing is
declared dead exactly as intended. `Heartbeat` does the renewing, and -- more
importantly -- notices when a renewal is REFUSED, which means somebody else owns
the resource now and this worker must stop.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from ..core.model import now
from ..ports.store import Store


class OwnershipLost(RuntimeError):
    """This worker no longer owns what it is about to act on.

    An exception rather than a returned flag: every caller has to handle it, and
    a refusal that can be ignored by forgetting to check a boolean is a refusal
    that will eventually be ignored. The same reasoning as `Refused` in the
    remote path, for the same reason.
    """


@dataclass(slots=True)
class Ownership:
    """Everything one run is allowed to touch, and whether it still may.

    Holds no lock of its own. It asks the store, which asks the database, which
    is the only place two processes can agree about anything.
    """
    store: Store
    workspace_id: str
    run_id: str
    resources: tuple[str, ...]
    lease_seconds: int
    clock: Callable[[], datetime] = now

    def verify(self) -> None:
        """Raise unless every resource is still held by this run, right now.

        Checked against the same clock the leases were stamped with. Two clocks
        was a real defect in the previous milestone and it made expiry
        unobservable; there is no reason to reintroduce it here.
        """
        at = self.clock()
        for resource in self.resources:
            if not self.store.holds_lease(resource, self.run_id,
                                          self.workspace_id, when=at):
                raise OwnershipLost(
                    f"run {self.run_id} no longer holds '{resource}'; another "
                    f"worker owns it or the lease expired. Nothing this run "
                    f"produced may be written")

    def held(self) -> bool:
        try:
            self.verify()
        except OwnershipLost:
            return False
        return True

    def renew(self) -> bool:
        """Extend every lease. False as soon as one refuses.

        A refusal is not a retryable hiccup: the row is owned by somebody else,
        and no amount of trying again changes that.
        """
        at = self.clock()
        for resource in self.resources:
            if not self.store.renew_lease(resource, self.run_id,
                                          self.lease_seconds,
                                          self.workspace_id, when=at):
                return False
        return True

    def release(self) -> None:
        for resource in self.resources:
            self.store.release_lease(resource, self.run_id, self.workspace_id)


@dataclass
class Heartbeat:
    """Renews a run's leases while it works, and notices when it cannot.

    A background thread, because the mission is a blocking call and the alternative
    is a lease that expires under a worker doing everything right. The field
    comment on `Orchestrator.lease_seconds` promised "o worker renova" long before
    anything renewed; this is that promise, kept.

    `lost` is the valuable part. If a renewal is ever refused, this worker has
    been superseded, and the flag survives for the caller to check after the
    mission returns -- which is the moment it can still decide not to write.
    """
    ownership: Ownership
    #: Told to stop when ownership is lost. Optional, because not every runner
    #: can be stopped -- see `_beat`.
    cancel: Callable[[str], None] | None = None
    interval: float = 0.0
    lost: bool = False
    cancelled: bool = False
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def __post_init__(self) -> None:
        if not self.interval:
            # Renew several times per lease window. Once per window would mean a
            # single slow renewal loses the lease, which is the failure the
            # heartbeat exists to prevent.
            self.interval = max(0.05, self.ownership.lease_seconds / 4)

    def __enter__(self) -> Heartbeat:
        self._thread = threading.Thread(target=self._beat, daemon=True,
                                        name=f"heartbeat-{self.ownership.run_id}")
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval * 2))

    def _stop_the_work(self) -> None:
        """Ask the runner to stop, because refusing the result is not enough.

        Discarding the output of a worker that lost its lease keeps the STATE
        correct, and a contention campaign showed that is not the whole
        problem: in one round of forty, two processes ran the same task at the
        same time in the same work area. Neither write survived -- the engine
        refused the stale one -- but both had already executed, and two agents
        editing one directory can produce a mess no verdict can undo.

        Whether it works depends on the runner. One that drives a subprocess can
        be killed and stops within milliseconds. One that runs in-process cannot
        be interrupted, and for those this is a request that may be ignored --
        which is a property of that runner, and is stated here rather than
        quietly assumed away.
        """
        if self.cancel is None:
            return
        try:
            self.cancel(self.ownership.run_id)
            self.cancelled = True
        except Exception:      # noqa: BLE001 - cancelling must not raise upward
            pass

    def _beat(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                if not self.ownership.renew():
                    self.lost = True
                    self._stop_the_work()
                    return
            except Exception:      # noqa: BLE001 - a heartbeat must not crash a run
                # An error renewing is not proof of loss. The lease will expire
                # on its own if this keeps failing, and `verify()` before the
                # write is what actually decides. Saying "lost" here would
                # abandon good work over a transient database lock.
                continue
