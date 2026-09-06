# -*- coding: utf-8 -*-
"""Configuration: YAML -> validated objects.

The configuration is the only place where a provider's name appears outside
`adapters/`. Swapping Jira for Linear means editing one line here.

Validation happens at load time, not at use time. Finding out a field is missing
in the middle of a dispatch costs a lost worker and an ambiguous state; finding
out at `regente doctor` costs one line of error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..core.policy import AutonomyLevel
from ..core.scheduling import Limits
from ..engine.supervisor import Budget


@dataclass(frozen=True, slots=True)
class AdapterConf:
    name: str
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def de(cls, raw: Any, field: str) -> AdapterConf:
        if isinstance(raw, str):
            return cls(name=raw)
        if isinstance(raw, dict):
            if "name" not in raw:
                raise ValueError(f"{field}: missing the 'name' key")
            return cls(name=str(raw["name"]),
                       options={k: v for k, v in raw.items() if k != "name"})
        raise ValueError(f"{field}: expected text or a mapping, got {type(raw).__name__}")


@dataclass(frozen=True, slots=True)
class ProjectConf:
    name: str
    default_environment: str = "staging"
    autonomy: AutonomyLevel | None = None
    repositories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Config:
    organization: str
    client: str
    workspace: str
    autonomy: AutonomyLevel
    root: Path
    providers: dict[str, AdapterConf]
    projects: tuple[ProjectConf, ...] = ()
    limits: Limits = field(default_factory=Limits)
    budget: Budget = field(default_factory=Budget)
    policies: Path | None = None
    #: Lifetime of a worker's lease. Expired without renewal = dead worker.
    #: It has to be longer than the longest normal job and shorter than the
    #: owner's patience: too short steals live work, too long leaves the task
    #: stopped after a crash.
    lease_seconds: int = 900
    #: Secret references THIS workspace may resolve. It is the list the
    #: SecretProvider uses as its scope -- what is not here, this workspace
    #: cannot reach, not even through a configuration mistake.
    secrets: tuple[str, ...] = ()
    #: Where each job runs, declared. It is the capability the task provider
    #: lacks -- measured: the natural field was empty in 100 of 100 issues, and
    #: the strongest signal available covered 14. Until the source emits the
    #: target, this map is what keeps the engine from guessing.
    targets: dict[str, dict[str, str]] = field(default_factory=dict)
    risk_factors: tuple[dict[str, Any], ...] = ()
    modelos: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Shadow: the engine decides and records, but performs no external write.
    #: It is born on. Turning it off is the owner's explicit decision, never a
    #: default.
    shadow: bool = True

    @property
    def banco(self) -> Path:
        return self.root / "regente.db"

    @property
    def areas(self) -> Path:
        return self.root / "areas"

    @property
    def journal(self) -> Path:
        return self.root / "jornal.log"


REQUIRED_FIELDS = ("organization", "client", "workspace")
#: Capabilities without which a tick does not run. The rest are optional and
#: their absence becomes CapabilityMissing the moment somebody tries to use them.
ESSENTIAL_PROVIDERS = ("tasks", "workspace_provider", "runner")


def load(path: str | Path) -> Config:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"configuration not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: the file must be a mapping")

    faltando = [c for c in REQUIRED_FIELDS if not raw.get(c)]
    if faltando:
        raise ValueError(f"{p}: required fields are missing: {', '.join(faltando)}")

    providers_brutos = raw.get("providers") or {}
    providers = {k: AdapterConf.de(v, f"providers.{k}") for k, v in providers_brutos.items()}
    without = [c for c in ESSENTIAL_PROVIDERS if c not in providers]
    if without:
        raise ValueError(f"{p}: essential providers missing: {', '.join(without)}")

    root = Path(raw.get("root") or (p.parent / ".regente")).expanduser()
    lim = raw.get("limits") or {}
    orc = raw.get("budget") or {}

    policies = raw.get("policies")
    caminho_policies = (p.parent / policies).resolve() if policies else None
    if caminho_policies and not caminho_policies.is_file():
        raise ValueError(f"{p}: policies file does not exist: {caminho_policies}")

    return Config(
        organization=str(raw["organization"]),
        client=str(raw["client"]),
        workspace=str(raw["workspace"]),
        autonomy=AutonomyLevel.from_text(raw.get("autonomy", "L2")),
        root=root,
        providers=providers,
        projects=tuple(
            ProjectConf(
                name=str(pr["name"]),
                default_environment=str(pr.get("default_environment", "staging")),
                autonomy=(AutonomyLevel.from_text(pr["autonomy"])
                           if pr.get("autonomy") is not None else None),
                repositories=tuple(str(r) for r in (pr.get("repositories") or [])))
            for pr in (raw.get("projects") or [])),
        limits=Limits(
            max_workers=int(lim.get("max_workers", 2)),
            max_dispatches_per_day=int(lim.get("max_dispatches_per_day", 8))),
        budget=Budget(
            max_iterations=int(orc.get("max_iterations", 24)),
            max_tool_calls=int(orc.get("max_tool_calls", 120)),
            max_cost_usd=float(orc.get("max_cost_usd", 5.0)),
            max_seconds=int(orc.get("max_seconds", 2700)),
            max_attempts=int(orc.get("max_attempts", 3))),
        policies=caminho_policies,
        lease_seconds=int(raw.get('lease_seconds', 900)),
        secrets=tuple(str(x) for x in (raw.get('secrets') or ())),
        targets={k: dict(v) for k, v in (raw.get('targets') or {}).items()},
        risk_factors=tuple(raw.get("risk_factors") or ()),
        modelos=dict(raw.get("models") or {}),
        shadow=bool(raw.get("shadow", True)),
    )


def load_policies(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    regras = raw.get("rules")
    if not isinstance(regras, list):
        raise ValueError(f"{path}: expected a list in 'rules'")
    return regras
