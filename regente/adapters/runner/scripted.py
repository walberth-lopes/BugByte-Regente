# -*- coding: utf-8 -*-
"""A declared agent: no model, no network, no surprises.

It exists to exercise the harness -- dispatch, isolation, structured outcome,
observation, verdict -- with a substrate that behaves identically every run. What
it demonstrates is the chain. It demonstrates nothing whatever about whether a
language model can do the work, and its name and `describe()` both say so, so
that a green run of this adapter can never be quoted as evidence about an agent.

It also writes a small file into the work area. That is not decoration: it leaves
a trace an isolation test can look for afterwards, which is how the engine proves
the area was real and was the agent's own.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...ports.agent import (AgentAvailability, AgentCapabilities,
                            AgentRunner, AuthMode, Check, Claim, ClaimedFile, Mission, Outcome,
                            ProcessStatus)

VALID_STATUS = {s.value for s in ProcessStatus}
VALID_CLAIM = {c.value for c in Claim}


@dataclass(slots=True)
class ScriptedAgent(AgentRunner):
    """Replays a declared outcome for a task key. Not a model."""

    name: str = "scripted"
    #: task key -> declared outcome
    script: dict[str, dict[str, Any]] = field(default_factory=dict)
    default_value: dict[str, Any] = field(default_factory=lambda: {
        "status": "NO_PROGRESS", "claim": "NONE",
        "summary": "no outcome declared for this task"})
    #: Whether to leave the trace file. Off for missions that assert on an
    #: untouched work area -- writing it would BE a change, and the engine
    #: observes changes for real now.
    leave_trace: bool = True

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
                "kind": "declared-not-a-model"}

    def run(self, mission: Mission) -> Outcome:
        started = time.monotonic()
        declared = self.script.get(mission.task_key, self.default_value)

        if self.leave_trace and mission.allowed_root:
            area = Path(mission.allowed_root)
            area.mkdir(parents=True, exist_ok=True)
            (area / "run.json").write_text(
                json.dumps({"run": mission.run_id, "goal": mission.goal,
                            "declared": declared}, ensure_ascii=False, indent=2),
                encoding="utf-8")

        status = str(declared.get("status", "NO_PROGRESS")).upper()
        if status not in VALID_STATUS:
            return Outcome(
                status=ProcessStatus.ERROR,
                summary=f"the script declared an unknown status {status!r}",
                duration_seconds=time.monotonic() - started)
        claim = str(declared.get("claim", "NONE")).upper()

        return Outcome(
            status=ProcessStatus(status),
            claim=Claim(claim) if claim in VALID_CLAIM else Claim.NONE,
            summary=str(declared.get("summary", "")),
            claimed_files=tuple(
                ClaimedFile(path=str(f)) for f in (declared.get("claimed_files") or [])),
            blockers=tuple(str(b) for b in (declared.get("blockers") or [])),
            questions=tuple(str(q) for q in (declared.get("questions") or [])),
            escalation_requested=bool(declared.get("escalation_requested")),
            escalation_reason=str(declared.get("escalation_reason", "")),
            cost_usd=float(declared.get("cost_usd", 0.0)),
            tokens=int(declared.get("tokens", 0)),
            tool_calls=int(declared.get("tool_calls", 0)),
            duration_seconds=time.monotonic() - started)
