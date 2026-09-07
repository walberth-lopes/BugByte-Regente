# -*- coding: utf-8 -*-
"""Risk Engine: measures the risk of a proposed action.

**Risk is rigour, not a waiting queue.** This is the distinction that defines the
engine:

- The *policy* decides **authority**: who may act. A merge into production
  requires a human because the organisation decided so, and no degree of model
  confidence changes that.
- The *risk* decides **rigour**: how much proof the action demands before it
  happens. High risk buys adversarial reading, a second pass, an extra test --
  it buys *work*, not waiting.

If high risk became "wait for a human to sign", the engine would hand the owner
back exactly the bottleneck it exists to remove: good work stalled in a queue.
What stops work is the policy, and it stops it by written rule, not by hesitation.

The Risk Engine is a pure function of declared signals. It calls no LLM: a risk
judgement that depends on the model is no gate against the model.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class RiskLevel(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass(frozen=True, slots=True)
class Signal:
    """A risk factor that fired, with the evidence that fired it.

    The evidence is mandatory because a risk without evidence is not auditable
    -- and without an audit trail the owner cannot loosen a rule safely.
    """
    name: str
    level: RiskLevel
    evidence: str


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    level: RiskLevel
    signals: tuple[Signal, ...] = ()

    @property
    def requires_second_pass(self) -> bool:
        """HIGH and CRITICAL do not wait for a human: they demand an adversarial re-read."""
        return self.level >= RiskLevel.HIGH

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(f"{s.name}: {s.evidence}" for s in self.signals)


@dataclass(frozen=True, slots=True)
class Factor:
    """A declarative risk rule, coming from configuration.

    `field` is read from the action context; `matches` is a list of glob
    patterns (for text) or a numeric threshold (for `greater_than`).
    """
    name: str
    level: RiskLevel
    field: str
    matches: tuple[str, ...] = ()
    greater_than: float | None = None
    equal_to: Any = None


#: Minimum base that holds for any client. The configuration file ADDS factors;
#: it does not replace these, because these are the ones describing physical
#: damage to the world (production, data, credentials) and not team preference.
#: The `producao` MATCH VALUE stays: it catches an environment spelled that way.
BASE_FACTORS: tuple[Factor, ...] = (
    Factor("production", RiskLevel.HIGH, "environment", ("prod", "production", "producao")),
    Factor("destructive", RiskLevel.CRITICAL, "action",
          ("*.delete", "*.drop", "*.destroy", "*.purge", "*.truncate", "*.rollback")),
    Factor("migration", RiskLevel.HIGH, "paths",
          ("*migrations/*", "*alembic/*", "*.sql", "*schema*")),
    Factor("infrastructure", RiskLevel.HIGH, "paths",
          ("*terraform/*", "*dockerfile*", "*workflows/*", "*pipelines/*",
           "*deploy/*", "*infra/*", "*chart/*")),
    Factor("credential", RiskLevel.CRITICAL, "paths",
          ("*secret*", "*credential*", "*.env*", "*iam*", "*token*")),
    Factor("authentication", RiskLevel.HIGH, "paths", ("*auth*", "*login*", "*session*", "*permission*")),
    Factor("payment", RiskLevel.HIGH, "paths", ("*payment*", "*billing*", "*invoice*", "*checkout*")),
    Factor("public_api", RiskLevel.MEDIUM, "paths", ("*api/*", "*routes/*", "*openapi*", "*proto*")),
    Factor("large_diff", RiskLevel.MEDIUM, "lines", greater_than=600),
    Factor("many_files", RiskLevel.MEDIUM, "files", greater_than=25),
    Factor("database", RiskLevel.HIGH, "category", ("database",)),
)


def _values_for(context: dict[str, Any], field: str) -> list[str]:
    v = context.get(field)
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v]
    return [str(v)]


def _fires(factor: Factor, context: dict[str, Any]) -> str | None:
    """Returns the evidence if the factor fired, or None."""
    if factor.greater_than is not None:
        raw = context.get(factor.field)
        try:
            n = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return f"{factor.field}={raw} > {factor.greater_than:g}" if n > factor.greater_than else None

    if factor.equal_to is not None:
        return f"{factor.field}={factor.equal_to}" if context.get(factor.field) == factor.equal_to else None

    for value in _values_for(context, factor.field):
        target = value.lower()
        for default_value in factor.matches:
            p = default_value.lower()
            # A pattern without a wildcard matches by substring: 'auth' has to
            # catch 'src/auth/handler.py' without every rule becoming '*auth*'.
            bateu = fnmatch.fnmatch(target, p) if ("*" in p or "?" in p) else (p in target)
            if bateu:
                return f"{factor.field}={value}"
    return None


@dataclass(slots=True)
class RiskEngine:
    factors: tuple[Factor, ...] = field(default=BASE_FACTORS)
    floor: RiskLevel = RiskLevel.LOW

    @classmethod
    def from_config(cls, extras: list[dict[str, Any]] | None = None) -> RiskEngine:
        """Client factors ADD to the base ones -- they never replace them."""
        extra: list[Factor] = []
        for raw in extras or []:
            extra.append(Factor(
                name=raw["name"],
                level=RiskLevel[str(raw.get("level", "MEDIUM")).upper()],
                field=raw.get("field", "paths"),
                matches=tuple(raw.get("matches", ()) or ()),
                greater_than=raw.get("greater_than"),
                equal_to=raw.get("equal_to"),
            ))
        return cls(factors=BASE_FACTORS + tuple(extra))

    def assess(self, context: dict[str, Any]) -> RiskAssessment:
        """`context` carries: action, environment, category, paths, lines, files.

        Missing information never lowers risk -- it merely fails to raise it.
        The caller is responsible for filling in `paths`; a truncated snapshot
        must be declared as a signal of its own by whoever produced it.
        """
        signals: list[Signal] = []
        for factor in self.factors:
            evidence = _fires(factor, context)
            if evidence:
                signals.append(Signal(factor.name, factor.level, evidence))

        level = max((s.level for s in signals), default=self.floor)
        return RiskAssessment(level=level, signals=tuple(signals))
