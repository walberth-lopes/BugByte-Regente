# -*- coding: utf-8 -*-
"""What a mission cost, and what it produced.

The headline number is `time_to_useful_change_s`: seconds from the moment the
engine started looking at the board to the first change that exists AND does not
break the tests. Everything else is diagnosis.

It is the headline because the engine's stated objective is useful work completed
per unit of time, not volume of reasoning -- and the two diverge in a way that is
easy to miss. An agent can burn an hour of impeccable analysis and produce
nothing; measured by tokens or tool calls it looks busy, measured by this it
looks like what it is.

`None` is a legitimate value. It means no useful change happened, and writing a
zero there would turn a failure into a suspiciously fast success.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from ..ports.agent import TestResult, Verdict


@dataclass(frozen=True, slots=True)
class MissionMetrics:
    task_key: str
    repository: str
    verdict: Verdict

    # --- resolution --------------------------------------------------
    target_confidence: str = ""
    target_source: str = ""
    target_strength: int = 0
    target_queries: int = 0
    task_resolution_s: float = 0.0

    # --- execution ---------------------------------------------------
    agent: str = ""
    agent_duration_s: float = 0.0
    iterations: int = 0
    retries: int = 0
    tool_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0

    # --- product -----------------------------------------------------
    files_changed: int = 0
    lines_changed: int = 0
    commits: int = 0

    # --- verification ------------------------------------------------
    baseline_duration_s: float = 0.0
    test_duration_s: float = 0.0
    test_result: str = ""

    # --- attention ---------------------------------------------------
    human_escalations: int = 0

    # --- the headline -------------------------------------------------
    time_to_useful_change_s: float | None = None

    total_duration_s: float = 0.0
    started_at: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        def row(label: str, value: object) -> str:
            return f"  {label:<26} {value}"

        useful = ("(none)" if self.time_to_useful_change_s is None
                  else f"{self.time_to_useful_change_s:.1f}s")
        return "\n".join([
            "MISSION METRICS",
            "",
            row("task", self.task_key),
            row("repository", self.repository),
            row("verdict", self.verdict.value),
            "",
            row("time_to_useful_change", useful),
            "",
            row("target confidence", f"{self.target_confidence} "
                                     f"({self.target_source}, strength "
                                     f"{self.target_strength})"),
            row("target queries", self.target_queries),
            row("task resolution", f"{self.task_resolution_s:.1f}s"),
            "",
            row("agent", self.agent),
            row("agent duration", f"{self.agent_duration_s:.1f}s"),
            row("iterations", self.iterations),
            row("retries", self.retries),
            row("tool calls", self.tool_calls),
            row("tokens", self.tokens),
            row("cost", f"US$ {self.cost_usd:.4f}"),
            "",
            row("files changed", self.files_changed),
            row("lines changed", self.lines_changed),
            row("commits", self.commits),
            "",
            row("baseline duration", f"{self.baseline_duration_s:.1f}s"),
            row("test duration", f"{self.test_duration_s:.1f}s"),
            row("test result", self.test_result or "(not run)"),
            "",
            row("human escalations", self.human_escalations),
            row("total duration", f"{self.total_duration_s:.1f}s"),
        ])


def build(
    task_key: str,
    repository: str,
    verdict: Verdict,
    discovery,
    loop_result,
    resolution_s: float,
    total_s: float,
    agent: str,
    started_at: datetime,
    escalations: int = 0,
) -> MissionMetrics:
    tests = loop_result.test_verdict
    attempts = loop_result.attempts
    return MissionMetrics(
        task_key=task_key,
        repository=repository,
        verdict=verdict,
        target_confidence=discovery.confidence.value if discovery else "",
        target_source=discovery.source.value if discovery else "",
        target_strength=discovery.strength if discovery else 0,
        target_queries=discovery.queries if discovery else 0,
        task_resolution_s=resolution_s,
        agent=agent,
        agent_duration_s=sum(a.duration_s for a in attempts),
        iterations=len(attempts),
        # The first turn is the attempt; every later turn is a retry. Counting
        # them as one number would hide the loop that this milestone exists to
        # bound.
        retries=max(0, len(attempts) - 1),
        tool_calls=loop_result.total_tool_calls,
        tokens=loop_result.total_tokens,
        cost_usd=loop_result.total_cost_usd,
        files_changed=len(loop_result.changed_files),
        lines_changed=loop_result.changed_lines,
        commits=len(loop_result.commits),
        baseline_duration_s=loop_result.baseline.duration_s if loop_result.baseline else 0.0,
        test_duration_s=tests.duration_s if tests else 0.0,
        test_result=tests.result.value if tests else "",
        human_escalations=escalations,
        time_to_useful_change_s=loop_result.time_to_useful_change_s,
        total_duration_s=total_s,
        started_at=started_at.isoformat(),
    )
