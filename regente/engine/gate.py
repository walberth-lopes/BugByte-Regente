# -*- coding: utf-8 -*-
"""The gate. Every agent action upon the world passes through here.

    Agent -> ToolRequest -> [risk] -> [policy] -> ALLOW/DENY/HUMAN_APPROVAL -> Tool

Three properties that make this a gate and not a convenience function:

1. **There is no alternative path.** No agent receives a write adapter directly;
   it receives this object. An agent talked round by a PR comment still hits the
   policy, which reads no text from the agent.

2. **It redoes the judgement from scratch.** The gate accepts no risk, no
   justification and no verdict computed by the caller. It takes the facts of the
   action and re-evaluates. Accepting a pre-computed risk would be letting the
   model choose its own limit.

3. **It records what it denied.** The refusal is the most valuable record the
   engine produces: it is what proves, weeks later, that the gate was alive --
   and what shows which rule to loosen when it clamped down too hard.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core import ids
from ..core.errors import PolicyDenied, HumanApprovalRequired
from ..core.model import ActionRecord
from ..core.policy import Action, AutonomyLevel, Decision, Effect, PolicyContext, PolicyEngine
from ..core.risk import RiskAssessment, RiskEngine
from ..ports.store import Store


@dataclass(frozen=True, slots=True)
class Scope:
    """Who is acting and under which tenancy. Assembled by the engine, never by the agent."""
    workspace_id: str
    organization: str = "*"
    client: str = "*"
    workspace: str = "*"
    project: str = "*"
    autonomy: AutonomyLevel = AutonomyLevel.L2
    agent: str = "engine"
    task_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class Verdicts:
    decision: Decision
    risk: RiskAssessment


@dataclass(slots=True)
class Gate:
    store: Store
    policy: PolicyEngine
    risk: RiskEngine
    #: When a HUMAN_APPROVAL appears, whoever turns it into a queue item.
    #: Injected so the gate does not know the queue -- it only knows how to block.
    on_human_needed: Callable[[Scope, Action, Verdicts], None] | None = None
    _extra_facts: dict[str, Any] = field(default_factory=dict)

    def assess(self, scope: Scope, action: Action, facts: dict[str, Any] | None = None) -> Verdicts:
        """Judges without executing. Used by anyone who wants to know before trying."""
        risk_context = {
            "action": action.kind,
            "environment": action.environment,
            "category": action.kind.split(".", 1)[0],
            "paths": (facts or {}).get("paths", ()),
            "lines": (facts or {}).get("lines", 0),
            "files": (facts or {}).get("files", 0),
            **self._extra_facts,
        }
        assessment = self.risk.assess(risk_context)
        decision = self.policy.decide(PolicyContext(
            action=action,
            organization=scope.organization, client=scope.client,
            workspace=scope.workspace, project=scope.project,
            agent=scope.agent, risk=assessment.level.name, autonomy=scope.autonomy,
        ))
        return Verdicts(decision=decision, risk=assessment)

    def execute(self, scope: Scope, action: Action, operation: Callable[[], Any],
                facts: dict[str, Any] | None = None) -> Any:
        """Judges and, if allowed, executes. Every passage becomes an ActionRecord.

        `operation` is a callable with no arguments precisely so the gate does not
        need to know any adapter's signature -- it authorises the declared
        *action*, not the function.
        """
        v = self.assess(scope, action, facts)
        started = time.monotonic()

        if v.decision.effect == Effect.DENY:
            self._register_call(scope, action, v, "denied", 0)
            raise PolicyDenied(v.decision.reason, v.decision.rule)

        if v.decision.effect == Effect.HUMAN_APPROVAL:
            self._register_call(scope, action, v, "awaiting human", 0)
            if self.on_human_needed:
                self.on_human_needed(scope, action, v)
            raise HumanApprovalRequired(v.decision.reason, v.decision.rule)

        try:
            result = operation()
        except Exception as e:
            self._register_call(scope, action, v, f"error: {type(e).__name__}: {e}"[:300],
                           int((time.monotonic() - started) * 1000))
            raise
        self._register_call(scope, action, v, "ok", int((time.monotonic() - started) * 1000))
        return result

    def _register_call(self, scope: Scope, action: Action, v: Verdicts,
                  result: str, duration_ms: int) -> None:
        self.store.record_action(ActionRecord(
            id=ids.new_id(ids.ACTION), workspace_id=scope.workspace_id,
            agent=scope.agent, action=action.kind, resource=action.resource,
            effect=v.decision.effect, risk=v.risk.level.name,
            task_id=scope.task_id, run_id=scope.run_id,
            rule=v.decision.rule,
            reason="; ".join([v.decision.reason, *v.risk.reasons])[:500],
            result=result, duration_ms=duration_ms))
