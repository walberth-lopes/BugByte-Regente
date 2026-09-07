# -*- coding: utf-8 -*-
"""Policy Engine: who may do what, and where.

Three invariants. None of them is configurable, because they are what makes the
gate a gate:

1. **Default deny.** An action with no rule allowing it is denied. The opposite
   -- allowing whatever nobody anticipated -- turns every new capability into a
   silent hole the day a new adapter arrives.

2. **The most restrictive wins.** If any rule says DENY, the verdict is DENY, no
   matter how many say ALLOW. File order does not decide security; neither does
   a numeric priority, which always ends up misconfigured.

3. **The engine decides, not the model.** `decide()` is a pure function of
   (context, rules). It takes no text from the agent, calls no LLM and accepts
   no justification. An agent talked round by a PR comment still hits this wall.

The autonomy ceiling is orthogonal to the rules: it expresses "how far this
project lets the engine go on its own". Breaching the ceiling becomes
HUMAN_APPROVAL -- not DENY -- because the human can still authorise it. What
refuses outright is the written rule.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class AutonomyLevel(IntEnum):
    L0 = 0   # READ_ONLY
    L1 = 1   # CODE       -- writes in the isolated workspace
    L2 = 2   # PR         -- push and pull request
    L3 = 3   # STAGING    -- merge and deploy to staging
    L4 = 4   # PRODUCTION -- deploy to production

    @classmethod
    def from_text(cls, value: str | int) -> AutonomyLevel:
        if isinstance(value, int):
            return cls(value)
        t = str(value).strip().upper()
        apelidos = {
            "READ_ONLY": cls.L0, "READONLY": cls.L0,
            "CODE": cls.L1, "PR": cls.L2,
            "STAGING": cls.L3, "PRODUCTION": cls.L4, "PROD": cls.L4,
        }
        if t in apelidos:
            return apelidos[t]
        return cls[t]


class Effect(str):
    ALLOW = "ALLOW"
    DENY = "DENY"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"


#: Severity order. Used for "the most restrictive wins".
_SEVERITY = {Effect.ALLOW: 0, Effect.HUMAN_APPROVAL: 1, Effect.DENY: 2}


#: Minimum autonomy level each action family requires. The key is the action
#: prefix, matched from the most specific to the most generic.
REQUIRED_LEVEL: dict[str, AutonomyLevel] = {
    "repo.read": AutonomyLevel.L0,
    "task.read": AutonomyLevel.L0,
    "cloud.read": AutonomyLevel.L0,
    "db.read": AutonomyLevel.L0,
    "ci.read": AutonomyLevel.L0,
    "workspace.write": AutonomyLevel.L1,
    "repo.branch": AutonomyLevel.L1,
    "repo.commit": AutonomyLevel.L1,
    "repo.push": AutonomyLevel.L2,
    "repo.pr": AutonomyLevel.L2,
    "task.write": AutonomyLevel.L2,
    "repo.merge": AutonomyLevel.L3,
    "deploy.staging": AutonomyLevel.L3,
    "deploy.production": AutonomyLevel.L4,
    "db.write": AutonomyLevel.L4,
    "cloud.write": AutonomyLevel.L4,
}


@dataclass(frozen=True, slots=True)
class Action:
    """What an agent wants to do in the world.

    `kind` is always `<capability>.<verb>` -- 'repo.merge', 'deploy.production'.
    The format is not style: it is what lets the policy reason about families of
    actions without knowing any adapter.
    """
    kind: str
    resource: str = "*"
    environment: str = "local"
    detalhes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PolicyContext:
    action: Action
    organization: str = "*"
    client: str = "*"
    workspace: str = "*"
    project: str = "*"
    agent: str = "*"
    risk: str = "LOW"
    autonomy: AutonomyLevel = AutonomyLevel.L2

    def as_dict(self) -> dict[str, str]:
        return {
            "action": self.action.kind,
            "resource": self.action.resource,
            "environment": self.action.environment,
            "organization": self.organization,
            "client": self.client,
            "workspace": self.workspace,
            "project": self.project,
            "agent": self.agent,
            "risk": self.risk,
        }


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    effect: str
    match: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def matches(self, ctx: dict[str, str]) -> bool:
        """Every declared criterion must match. An absent criterion is a wildcard."""
        for field, esperado in self.match.items():
            value = ctx.get(field, "")
            patterns = esperado if isinstance(esperado, (list, tuple)) else [esperado]
            if not any(_matches_one(value, str(p)) for p in patterns):
                return False
        return True


def _matches_one(value: str, default_value: str) -> bool:
    if default_value == "*":
        return True
    v, p = value.lower(), default_value.lower()
    if "*" in p or "?" in p:
        return fnmatch.fnmatch(v, p)
    return v == p


@dataclass(frozen=True, slots=True)
class Decision:
    effect: str
    reason: str
    rule: str | None = None
    #: Every rule that matched, in the order they were evaluated. The owner has
    #: to see why an action was blocked even when another rule allowed it --
    #: without that, loosening a policy turns into trial and error.
    matched: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.effect == Effect.ALLOW

    @property
    def needs_human(self) -> bool:
        return self.effect == Effect.HUMAN_APPROVAL


def required_level(kind: str) -> AutonomyLevel:
    """Matches from the most specific prefix to the most generic one.

    An unknown action falls to the maximum ceiling on purpose: a new capability
    is born demanding the highest level, and somebody has to lower it
    deliberately.
    """
    best: AutonomyLevel | None = None
    longest = -1
    for prefix, level in REQUIRED_LEVEL.items():
        if (kind == prefix or kind.startswith(prefix + ".")) and len(prefix) > longest:
            best, longest = level, len(prefix)
    return best if best is not None else AutonomyLevel.L4


@dataclass(slots=True)
class PolicyEngine:
    rules: tuple[Rule, ...] = ()

    @classmethod
    def from_config(cls, raw: list[dict[str, Any]] | None) -> PolicyEngine:
        rules = []
        for i, b in enumerate(raw or []):
            effect = str(b["effect"]).strip().upper()
            if effect not in _SEVERITY:
                raise ValueError(f"unknown effect in policy: {effect!r}")
            rules.append(Rule(
                name=b.get("name") or f"rule_{i}",
                effect=effect,
                match={k: v for k, v in (b.get("match") or {}).items()},
                reason=b.get("reason", ""),
            ))
        return cls(rules=tuple(rules))

    def decide(self, ctx: PolicyContext) -> Decision:
        plan = ctx.as_dict()
        matched = [r for r in self.rules if r.matches(plan)]

        if not matched:
            return Decision(
                effect=Effect.DENY,
                reason=f"no rule allows '{ctx.action.kind}' on '{ctx.action.resource}'",
                matched=(),
            )

        # Invariant 2: the most restrictive wins, not the first nor the last.
        winner = max(matched, key=lambda r: _SEVERITY[r.effect])
        names = tuple(r.name for r in matched)

        # The autonomy ceiling only tightens: it never turns DENY into ALLOW.
        exigido = required_level(ctx.action.kind)
        if winner.effect == Effect.ALLOW and ctx.autonomy < exigido:
            return Decision(
                effect=Effect.HUMAN_APPROVAL,
                reason=(f"'{ctx.action.kind}' requires autonomy {exigido.name} and "
                        f"this scope only goes up to {ctx.autonomy.name}"),
                rule="autonomy_ceiling",
                matched=names,
            )

        return Decision(
            effect=winner.effect,
            reason=winner.reason or f"rule '{winner.name}'",
            rule=winner.name,
            matched=names,
        )
