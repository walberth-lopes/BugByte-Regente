# -*- coding: utf-8 -*-
"""Configuracao: YAML -> objetos validados.

A configuracao e o unico lugar onde nome de fornecedor aparece fora de
`adapters/`. Trocar Jira por Linear e editar uma linha aqui.

Validacao acontece no carregamento, nao no uso. Descobrir que falta um campo no
meio de um despacho custa um worker perdido e um estado ambiguo; descobrir no
`regente doctor` custa uma linha de erro.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..core.policy import AutonomyLevel
from ..core.scheduling import Limites
from ..engine.supervisor import Orcamento


@dataclass(frozen=True, slots=True)
class AdapterConf:
    nome: str
    opcoes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def de(cls, bruto: Any, campo: str) -> AdapterConf:
        if isinstance(bruto, str):
            return cls(nome=bruto)
        if isinstance(bruto, dict):
            if "nome" not in bruto:
                raise ValueError(f"{campo}: falta a chave 'nome'")
            return cls(nome=str(bruto["nome"]),
                       opcoes={k: v for k, v in bruto.items() if k != "nome"})
        raise ValueError(f"{campo}: esperava texto ou mapeamento, veio {type(bruto).__name__}")


@dataclass(frozen=True, slots=True)
class ProjectConf:
    nome: str
    ambiente_padrao: str = "staging"
    autonomia: AutonomyLevel | None = None
    repositorios: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Config:
    organizacao: str
    cliente: str
    workspace: str
    autonomia: AutonomyLevel
    raiz: Path
    providers: dict[str, AdapterConf]
    projetos: tuple[ProjectConf, ...] = ()
    limites: Limites = field(default_factory=Limites)
    orcamento: Orcamento = field(default_factory=Orcamento)
    policies: Path | None = None
    #: Vida do lease de um worker. Vencido sem renovacao = worker morto.
    #: Precisa ser maior que o maior trabalho normal e menor que a paciencia
    #: do dono: curto demais rouba trabalho vivo, longo demais deixa a task
    #: parada depois de um crash.
    lease_segundos: int = 900
    #: Referencias de segredo que ESTE workspace pode resolver. E a lista
    #: que o SecretProvider usa como escopo -- o que nao esta aqui, este
    #: workspace nao alcanca, nem por engano de configuracao.
    segredos: tuple[str, ...] = ()
    #: Onde cada trabalho roda, declarado. E a capacidade que falta no provedor
    #: de tasks -- medido: o campo natural estava vazio em 100 de 100 issues, e
    #: o sinal mais forte disponivel cobria 14. Enquanto a origem nao emitir o
    #: alvo, este mapa e o que evita o motor adivinhar.
    alvos: dict[str, dict[str, str]] = field(default_factory=dict)
    fatores_de_risco: tuple[dict[str, Any], ...] = ()
    modelos: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Sombra: o motor decide e registra, mas nao executa escrita externa.
    #: Nasce ligado. Desligar e decisao explicita do dono, nunca default.
    sombra: bool = True

    @property
    def banco(self) -> Path:
        return self.raiz / "regente.db"

    @property
    def areas(self) -> Path:
        return self.raiz / "areas"

    @property
    def jornal(self) -> Path:
        return self.raiz / "jornal.log"


OBRIGATORIOS = ("organizacao", "cliente", "workspace")
#: Capacidades sem as quais um tick nao roda. As demais sao opcionais e sua
#: ausencia vira CapacidadeAusente na hora em que alguem tentar usa-las.
ESSENCIAIS = ("tasks", "workspace_provider", "runner")


def carrega(caminho: str | Path) -> Config:
    p = Path(caminho)
    if not p.is_file():
        raise FileNotFoundError(f"configuracao nao encontrada: {p}")
    bruto = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(bruto, dict):
        raise ValueError(f"{p}: o arquivo precisa ser um mapeamento")

    faltando = [c for c in OBRIGATORIOS if not bruto.get(c)]
    if faltando:
        raise ValueError(f"{p}: faltam campos obrigatorios: {', '.join(faltando)}")

    providers_brutos = bruto.get("providers") or {}
    providers = {k: AdapterConf.de(v, f"providers.{k}") for k, v in providers_brutos.items()}
    sem = [c for c in ESSENCIAIS if c not in providers]
    if sem:
        raise ValueError(f"{p}: providers essenciais ausentes: {', '.join(sem)}")

    raiz = Path(bruto.get("raiz") or (p.parent / ".regente")).expanduser()
    lim = bruto.get("limites") or {}
    orc = bruto.get("orcamento") or {}

    policies = bruto.get("policies")
    caminho_policies = (p.parent / policies).resolve() if policies else None
    if caminho_policies and not caminho_policies.is_file():
        raise ValueError(f"{p}: arquivo de policies nao existe: {caminho_policies}")

    return Config(
        organizacao=str(bruto["organizacao"]),
        cliente=str(bruto["cliente"]),
        workspace=str(bruto["workspace"]),
        autonomia=AutonomyLevel.de_texto(bruto.get("autonomia", "L2")),
        raiz=raiz,
        providers=providers,
        projetos=tuple(
            ProjectConf(
                nome=str(pr["nome"]),
                ambiente_padrao=str(pr.get("ambiente_padrao", "staging")),
                autonomia=(AutonomyLevel.de_texto(pr["autonomia"])
                           if pr.get("autonomia") is not None else None),
                repositorios=tuple(str(r) for r in (pr.get("repositorios") or [])))
            for pr in (bruto.get("projetos") or [])),
        limites=Limites(
            max_workers=int(lim.get("max_workers", 2)),
            max_despachos_dia=int(lim.get("max_despachos_dia", 8))),
        orcamento=Orcamento(
            max_iteracoes=int(orc.get("max_iteracoes", 24)),
            max_tool_calls=int(orc.get("max_tool_calls", 120)),
            max_custo_usd=float(orc.get("max_custo_usd", 5.0)),
            max_segundos=int(orc.get("max_segundos", 2700)),
            max_tentativas=int(orc.get("max_tentativas", 3))),
        policies=caminho_policies,
        lease_segundos=int(bruto.get('lease_segundos', 900)),
        segredos=tuple(str(x) for x in (bruto.get('segredos') or ())),
        alvos={k: dict(v) for k, v in (bruto.get('alvos') or {}).items()},
        fatores_de_risco=tuple(bruto.get("fatores_de_risco") or ()),
        modelos=dict(bruto.get("modelos") or {}),
        sombra=bool(bruto.get("sombra", True)),
    )


def carrega_policies(caminho: Path | None) -> list[dict[str, Any]]:
    if caminho is None:
        return []
    bruto = yaml.safe_load(Path(caminho).read_text(encoding="utf-8")) or {}
    regras = bruto.get("regras")
    if not isinstance(regras, list):
        raise ValueError(f"{caminho}: esperava uma lista em 'regras'")
    return regras
