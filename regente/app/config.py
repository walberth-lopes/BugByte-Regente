# -*- coding: utf-8 -*-
"""Configuracao: YAML -> objetos validados.

A configuracao e o unico lugar onde nome de fornecedor aparece fora de
`adapters/`. Trocar Jira por Linear e editar uma linha aqui.

Validacao acontece no carregamento, nao no uso. Descobrir que falta um campo no
meio de um despacho custa um worker perdido e um estado ambiguo; descobrir no
`regente doctor` custa uma linha de error.
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
    def de(cls, bruto: Any, field: str) -> AdapterConf:
        if isinstance(bruto, str):
            return cls(name=bruto)
        if isinstance(bruto, dict):
            if "name" not in bruto:
                raise ValueError(f"{field}: missing the 'name' key")
            return cls(name=str(bruto["name"]),
                       options={k: v for k, v in bruto.items() if k != "name"})
        raise ValueError(f"{field}: esperava texto ou mapeamento, veio {type(bruto).__name__}")


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
    #: Vida do lease de um worker. Vencido sem renovacao = worker morto.
    #: Precisa ser maior que o maior trabalho normal e menor que a paciencia
    #: do dono: curto demais rouba trabalho vivo, longo demais deixa a task
    #: parada depois de um crash.
    lease_seconds: int = 900
    #: Referencias de segredo que ESTE workspace pode resolver. E a lista
    #: que o SecretProvider usa como escopo -- o que nao esta aqui, este
    #: workspace nao alcanca, nem por engano de configuracao.
    secrets: tuple[str, ...] = ()
    #: Onde cada trabalho roda, declarado. E a capacidade que falta no provedor
    #: de tasks -- medido: o campo natural estava vazio em 100 de 100 issues, e
    #: o sinal mais forte disponivel cobria 14. Enquanto a origem nao emitir o
    #: alvo, este mapa e o que evita o motor adivinhar.
    targets: dict[str, dict[str, str]] = field(default_factory=dict)
    risk_factors: tuple[dict[str, Any], ...] = ()
    modelos: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Sombra: o motor decide e registra, mas nao executa escrita externa.
    #: Nasce ligado. Desligar e decisao explicita do dono, nunca default.
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
#: Capacidades sem as quais um tick nao roda. As demais sao opcionais e sua
#: ausencia vira CapacidadeAusente na hora em que alguem tentar usa-las.
ESSENTIAL_PROVIDERS = ("tasks", "workspace_provider", "runner")


def load(path: str | Path) -> Config:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"configuracao nao encontrada: {p}")
    bruto = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(bruto, dict):
        raise ValueError(f"{p}: o arquivo precisa ser um mapeamento")

    faltando = [c for c in REQUIRED_FIELDS if not bruto.get(c)]
    if faltando:
        raise ValueError(f"{p}: faltam campos obrigatorios: {', '.join(faltando)}")

    providers_brutos = bruto.get("providers") or {}
    providers = {k: AdapterConf.de(v, f"providers.{k}") for k, v in providers_brutos.items()}
    sem = [c for c in ESSENTIAL_PROVIDERS if c not in providers]
    if sem:
        raise ValueError(f"{p}: providers essenciais ausentes: {', '.join(sem)}")

    root = Path(bruto.get("root") or (p.parent / ".regente")).expanduser()
    lim = bruto.get("limits") or {}
    orc = bruto.get("budget") or {}

    policies = bruto.get("policies")
    caminho_policies = (p.parent / policies).resolve() if policies else None
    if caminho_policies and not caminho_policies.is_file():
        raise ValueError(f"{p}: arquivo de policies nao existe: {caminho_policies}")

    return Config(
        organization=str(bruto["organization"]),
        client=str(bruto["client"]),
        workspace=str(bruto["workspace"]),
        autonomy=AutonomyLevel.from_text(bruto.get("autonomy", "L2")),
        root=root,
        providers=providers,
        projects=tuple(
            ProjectConf(
                name=str(pr["name"]),
                default_environment=str(pr.get("default_environment", "staging")),
                autonomy=(AutonomyLevel.from_text(pr["autonomy"])
                           if pr.get("autonomy") is not None else None),
                repositories=tuple(str(r) for r in (pr.get("repositories") or [])))
            for pr in (bruto.get("projects") or [])),
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
        lease_seconds=int(bruto.get('lease_seconds', 900)),
        secrets=tuple(str(x) for x in (bruto.get('secrets') or ())),
        targets={k: dict(v) for k, v in (bruto.get('targets') or {}).items()},
        risk_factors=tuple(bruto.get("risk_factors") or ()),
        modelos=dict(bruto.get("models") or {}),
        shadow=bool(bruto.get("shadow", True)),
    )


def load_policies(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    bruto = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    regras = bruto.get("rules")
    if not isinstance(regras, list):
        raise ValueError(f"{path}: esperava uma lista em 'regras'")
    return regras
