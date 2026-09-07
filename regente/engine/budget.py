# -*- coding: utf-8 -*-
"""When to try again, and when trying again is just buying the same answer.

The rule this module exists to enforce:

    retry only when new information can plausibly change the result

and never:

    retry because retry budget remains

Those two sound similar and behave nothing alike. The second turns a stuck agent
into a money furnace that finishes with an empty branch and a large invoice; and
because it *does* eventually stop, it looks like it worked. A budget is a
ceiling, not a plan.

So a retry has to be argued for. `may_retry` asks what the next attempt would
have that the last one lacked -- a new failure to react to, a change to build on,
a first sight of the tests -- and refuses when the honest answer is "nothing".

Four independent ceilings, because they fail differently and need different
words: attempts, wall clock, validation retries, and repetition. The last is the
only one that measures progress rather than consumption, and it is the one that
usually fires first.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from ..ports.agent import Budget, Outcome, ProcessStatus


def signature(*parts: str) -> str:
    """Reduce a set of strings to a comparable mark, digits removed.

    Without dropping digits, "timed out after 30.2s" and "timed out after 31.7s"
    look like different failures and the repetition detector never fires. That
    single detail is the difference between a detector and a decoration.
    """
    text = " ".join(p or "" for p in parts).lower()
    cleaned = "".join(c for c in text if not c.isdigit())
    return hashlib.sha1(" ".join(cleaned.split()).encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether to run the agent again, and the reason either way.

    The reason is required in both directions. A refusal nobody can read is
    indistinguishable from a crash, and an approval nobody can read is how a
    loop quietly becomes infinite.
    """
    proceed: bool
    reason: str
    #: What the engine should do instead when `proceed` is False.
    next_step: str = "stop"     # 'stop' | 'escalate' | 'change_strategy'

    def __bool__(self) -> bool:
        return self.proceed


@dataclass(slots=True)
class RunBudget:
    """Tracks one mission's consumption and decides whether to continue.

    Every check happens BEFORE spending. A budget verified after the call has
    already been paid, and the most expensive attempt is always the one that
    took the budget over the line.
    """
    budget: Budget
    started: float = field(default_factory=time.monotonic)
    attempts: int = 0
    validation_retries: int = 0
    cost_usd: float = 0.0
    tokens: int = 0
    tool_calls: int = 0
    #: signature -> how many times it has been seen
    _seen: dict[str, int] = field(default_factory=dict)
    #: What each attempt produced, in order, for the "nothing new" argument.
    _history: list[str] = field(default_factory=list)

    # ---- consumption -------------------------------------------------
    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def spend(self, outcome: Outcome) -> None:
        self.cost_usd += outcome.cost_usd
        self.tokens += outcome.tokens
        self.tool_calls += outcome.tool_calls

    # ---- the ceilings ------------------------------------------------
    def exhausted(self) -> Decision:
        """A hard ceiling was reached. Consumption, not progress."""
        if self.attempts >= self.budget.max_iterations:
            return Decision(False, f"{self.attempts} attempt(s) of "
                                   f"{self.budget.max_iterations}", "escalate")
        if self.elapsed > self.budget.max_seconds:
            return Decision(False, f"timebox: {int(self.elapsed)}s of "
                                   f"{self.budget.max_seconds}s", "escalate")
        if self.cost_usd > self.budget.max_cost_usd:
            return Decision(False, f"budget: US$ {self.cost_usd:.2f} of "
                                   f"{self.budget.max_cost_usd:.2f}", "escalate")
        if self.tool_calls > self.budget.max_tool_calls:
            return Decision(False, f"budget: {self.tool_calls} tool calls of "
                                   f"{self.budget.max_tool_calls}", "escalate")
        if self.validation_retries > self.budget.max_validation_retries:
            return Decision(False,
                            f"{self.validation_retries} validation retries of "
                            f"{self.budget.max_validation_retries}", "escalate")
        return Decision(True, "within every ceiling")

    # ---- the argument for another attempt ----------------------------
    def record(self, outcome: Outcome, observed_change: bool,
               failure: str = "") -> int:
        """Register what an attempt produced; return how many times it repeated.

        The mark deliberately mixes the agent's own signature with what the
        ENGINE observed. An agent that reports something different every time
        while changing nothing is repeating, and a signature built only from its
        own words would never notice.
        """
        mark = signature(outcome.signature(), str(observed_change), failure)
        self._seen[mark] = self._seen.get(mark, 0) + 1
        self._history.append(mark)
        return self._seen[mark]

    def may_retry(self, last: Outcome, observed_change: bool,
                  new_failure: str = "") -> Decision:
        """Would the next attempt know anything this one did not?

        This is the question the whole module exists to ask. It is asked after
        the ceilings, because a cheap "nothing new happened" beats an expensive
        "there is budget left" every time.
        """
        ceiling = self.exhausted()
        if not ceiling:
            return ceiling

        # A process that broke without producing anything might break for a
        # transient reason -- but only once. The second identical break is the
        # same purchase.
        repeats = self._seen.get(
            signature(last.signature(), str(observed_change), new_failure), 0)
        if repeats >= 2:
            return Decision(
                False,
                f"the same result {repeats} times: status={last.status.value}, "
                f"changed={observed_change}. Another attempt would buy a third "
                f"copy of the same answer", "change_strategy")

        if last.status is ProcessStatus.NEEDS_HUMAN or last.escalation_requested:
            return Decision(False, "the agent asked for a person; retrying would "
                                   "ask the same question again", "escalate")

        if last.status in (ProcessStatus.TIMEBOX, ProcessStatus.BUDGET):
            return Decision(False, f"the agent stopped on its own "
                                   f"({last.status.value}); the next attempt "
                                   f"starts from the same place", "escalate")

        if new_failure:
            return Decision(True, "there is a new failure to react to")

        if observed_change:
            return Decision(True, "the work area changed; the next attempt "
                                  "builds on something real")

        if last.status is ProcessStatus.ERROR and repeats < 1:
            return Decision(True, "the process broke once; a transient cause is "
                                  "plausible and one repeat is cheap")

        return Decision(
            False,
            "nothing changed and no new failure appeared; the next attempt "
            "would run with exactly the information this one had", "escalate")

    def describe(self) -> dict[str, object]:
        return {"attempts": self.attempts, "elapsed_s": round(self.elapsed, 1),
                "cost_usd": round(self.cost_usd, 4), "tokens": self.tokens,
                "tool_calls": self.tool_calls,
                "validation_retries": self.validation_retries,
                "distinct_results": len(self._seen)}
