# -*- coding: utf-8 -*-
"""A deterministic agent that really edits files. Still not a model.

`DeterministicAgent` applies edits declared in configuration and then reports
them in the same structured shape a real agent must use. Because the edits are
declared, every run is reproducible -- which is what makes it useful for proving
the chain and useless for proving intelligence. Presenting one as the other would
be the most flattering lie this project could tell itself.

It is also the instrument for the adversarial cases. A fake that only behaves
well proves nothing about the guards, so this one can be told to lie: claim files
it did not write, claim tests it did not run, write outside the area, take too
long, crash, or emit an unknown status. Every one of those is a real process
doing a real thing, and the engine has to catch it by looking rather than by
being told.

The escape attempt is deliberately NOT blocked here. An earlier version refused
the path itself, which made the adapter safe and the engine untested: the guard
that matters is the engine observing the escape afterwards, and a fake that
cannot escape can never demonstrate it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...ports.agent import (AgentAvailability, AgentCapabilities,
                            AgentRunner, AuthMode, Check, Claim, ClaimedFile, ClaimedTest,
                            Mission, Outcome, ProcessStatus)

VALID_STATUS = {s.value for s in ProcessStatus}
VALID_CLAIM = {c.value for c in Claim}


@dataclass(slots=True)
class DeterministicAgent(AgentRunner):
    """Applies declared edits and reports declared claims. Not a model."""

    #: task key -> {"edits": {relative path: content}, "status": ..., "claim": ...,
    #:              "claimed_files": [...], "summary": ..., "sleep": seconds}
    script: dict[str, dict[str, Any]] = field(default_factory=dict)
    fallback: dict[str, Any] = field(default_factory=lambda: {
        "status": "NO_PROGRESS", "summary": "no edit declared for this task"})
    name: str = "deterministic"
    #: Set by a test to make the process appear to break.
    raise_on_run: BaseException | None = None

    def availability(self) -> AgentAvailability:
        """Always ready. It is a program, not a service, and needs nobody."""
        return AgentAvailability(
            auth_mode=AuthMode.NONE, adapter=self.name,
            executable=Check.yes("in-process"),
            protocol=Check.yes("structured outcome, declared"),
            authentication=Check.yes("no credential is involved"),
            agent=Check.yes("deterministic; not a model"),
            capabilities=AgentCapabilities(edits_files=True,
                                           structured_output=True))

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "kind": "deterministic-not-a-model"}

    def run(self, mission: Mission) -> Outcome:
        started = time.monotonic()
        if self.raise_on_run is not None:
            raise self.raise_on_run

        plan = self.script.get(mission.task_key, self.fallback)
        area = Path(mission.allowed_root)

        if plan.get("sleep"):
            time.sleep(float(plan["sleep"]))

        written: list[ClaimedFile] = []
        for relative, content in (plan.get("edits") or {}).items():
            target = area / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            before = target.read_text(encoding="utf-8") if target.is_file() else ""
            target.write_text(content, encoding="utf-8")
            written.append(ClaimedFile(
                path=relative, additions=len(content.splitlines()),
                deletions=len(before.splitlines()),
                status="modified" if before else "added"))

        status = str(plan.get("status", "FINISHED" if written else "NO_PROGRESS")).upper()
        if status not in VALID_STATUS:
            return Outcome(
                status=ProcessStatus.ERROR,
                summary=f"the script declared an unknown status {status!r}",
                duration_seconds=time.monotonic() - started)

        claim = str(plan.get("claim", "COMPLETE" if written else "NONE")).upper()

        # `claimed_files` may be declared independently of `edits`. That is how a
        # test makes the agent lie: claim three files, write none.
        claimed = plan.get("claimed_files")
        if claimed is not None:
            written = [ClaimedFile(path=str(f)) for f in claimed]

        return Outcome(
            status=ProcessStatus(status),
            claim=Claim(claim) if claim in VALID_CLAIM else Claim.NONE,
            summary=str(plan.get("summary", "")),
            claimed_files=tuple(written),
            claimed_tests=tuple(
                ClaimedTest(command=str(t.get("command", "")),
                            exit_code=t.get("exit_code"),
                            output=str(t.get("output", "")))
                for t in (plan.get("claimed_tests") or []) if isinstance(t, dict)),
            blockers=tuple(str(b) for b in (plan.get("blockers") or [])),
            questions=tuple(str(q) for q in (plan.get("questions") or [])),
            escalation_requested=bool(plan.get("escalation_requested")),
            escalation_reason=str(plan.get("escalation_reason", "")),
            cost_usd=float(plan.get("cost_usd", 0.0)),
            tokens=int(plan.get("tokens", 0)),
            tool_calls=int(plan.get("tool_calls", 0)),
            duration_seconds=time.monotonic() - started)
