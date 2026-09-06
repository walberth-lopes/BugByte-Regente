# -*- coding: utf-8 -*-
"""Policy Engine: quem pode fazer o que, e onde.

Tres invariantes. Nenhuma delas e configuravel, porque sao elas que fazem o
portao ser portao:

1. **Default deny.** Acao sem regra que a permita e negada. O contrario --
   permitir o que ninguem previu -- transforma cada capacidade nova em brecha
   silenciosa no dia em que um adapter novo entra.

2. **Vence o mais restritivo.** Se qualquer regra diz DENY, o veredito e DENY,
   independentemente de quantas dizem ALLOW. Ordem de arquivo nao decide
   seguranca; nem prioridade numerica, que sempre acaba mal configurada.

3. **O motor decide, nao o modelo.** `decide()` e funcao pura de (contexto,
   regras). Nao recebe texto do agente, nao chama LLM e nao aceita justificativa.
   Um agente convencido por um comentario de PR ainda esbarra aqui.

O teto de autonomia e ortogonal as regras: ele exprime "ate onde este projeto
deixa o motor ir sozinho". Estourar o teto vira HUMAN_APPROVAL -- e nao DENY --
porque o humano continua podendo autorizar. Quem nega de vez e a regra escrita.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class AutonomyLevel(IntEnum):
    L0 = 0   # READ_ONLY
    L1 = 1   # CODE       -- escreve no workspace isolado
    L2 = 2   # PR         -- push e pull request
    L3 = 3   # STAGING    -- merge e deploy em staging
    L4 = 4   # PRODUCTION -- deploy em producao

    @classmethod
    def de_texto(cls, valor: str | int) -> AutonomyLevel:
        if isinstance(valor, int):
            return cls(valor)
        t = str(valor).strip().upper()
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


#: Ordem de severidade. Usada para "vence o mais restritivo".
_SEVERIDADE = {Effect.ALLOW: 0, Effect.HUMAN_APPROVAL: 1, Effect.DENY: 2}


#: Nivel minimo de autonomia que cada familia de acao exige. Chave e prefixo da
#: acao, casada do mais especifico para o mais generico.
NIVEL_EXIGIDO: dict[str, AutonomyLevel] = {
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
    """O que um agente quer fazer no mundo.

    `kind` e sempre `<capacidade>.<verbo>` -- 'repo.merge', 'deploy.production'.
    O formato nao e estilo: e o que permite a policy raciocinar sobre familias de
    acao sem conhecer nenhum adapter.
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

    def como_dict(self) -> dict[str, str]:
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
    nome: str
    efeito: str
    match: dict[str, Any] = field(default_factory=dict)
    motivo: str = ""

    def casa(self, ctx: dict[str, str]) -> bool:
        """Todo criterio declarado precisa casar. Criterio ausente e curinga."""
        for campo, esperado in self.match.items():
            valor = ctx.get(campo, "")
            padroes = esperado if isinstance(esperado, (list, tuple)) else [esperado]
            if not any(_casa_um(valor, str(p)) for p in padroes):
                return False
        return True


def _casa_um(valor: str, padrao: str) -> bool:
    if padrao == "*":
        return True
    v, p = valor.lower(), padrao.lower()
    if "*" in p or "?" in p:
        return fnmatch.fnmatch(v, p)
    return v == p


@dataclass(frozen=True, slots=True)
class Decision:
    efeito: str
    motivo: str
    regra: str | None = None
    #: Todas as regras que casaram, na ordem em que foram avaliadas. O dono
    #: precisa ver por que uma acao foi barrada mesmo quando outra regra a
    #: liberava -- sem isso, afrouxar uma policy vira tentativa e erro.
    casadas: tuple[str, ...] = ()

    @property
    def permitido(self) -> bool:
        return self.efeito == Effect.ALLOW

    @property
    def precisa_humano(self) -> bool:
        return self.efeito == Effect.HUMAN_APPROVAL


def nivel_exigido(kind: str) -> AutonomyLevel:
    """Casa do prefixo mais especifico para o mais generico.

    Acao desconhecida cai no teto maximo de proposito: capacidade nova nasce
    exigindo o nivel mais alto, e alguem precisa baixa-la conscientemente.
    """
    melhor: AutonomyLevel | None = None
    tamanho = -1
    for prefixo, nivel in NIVEL_EXIGIDO.items():
        if (kind == prefixo or kind.startswith(prefixo + ".")) and len(prefixo) > tamanho:
            melhor, tamanho = nivel, len(prefixo)
    return melhor if melhor is not None else AutonomyLevel.L4


@dataclass(slots=True)
class PolicyEngine:
    regras: tuple[Rule, ...] = ()

    @classmethod
    def de_config(cls, brutas: list[dict[str, Any]] | None) -> PolicyEngine:
        regras = []
        for i, b in enumerate(brutas or []):
            efeito = str(b["efeito"]).strip().upper()
            if efeito not in _SEVERIDADE:
                raise ValueError(f"efeito desconhecido em policy: {efeito!r}")
            regras.append(Rule(
                nome=b.get("nome") or f"regra_{i}",
                efeito=efeito,
                match={k: v for k, v in (b.get("match") or {}).items()},
                motivo=b.get("motivo", ""),
            ))
        return cls(regras=tuple(regras))

    def decide(self, ctx: PolicyContext) -> Decision:
        plano = ctx.como_dict()
        casadas = [r for r in self.regras if r.casa(plano)]

        if not casadas:
            return Decision(
                efeito=Effect.DENY,
                motivo=f"nenhuma regra permite '{ctx.action.kind}' em '{ctx.action.resource}'",
                casadas=(),
            )

        # Invariante 2: vence o mais restritivo, nao a primeira nem a ultima.
        vencedora = max(casadas, key=lambda r: _SEVERIDADE[r.efeito])
        nomes = tuple(r.nome for r in casadas)

        # Teto de autonomia so aperta: nunca transforma DENY em ALLOW.
        exigido = nivel_exigido(ctx.action.kind)
        if vencedora.efeito == Effect.ALLOW and ctx.autonomy < exigido:
            return Decision(
                efeito=Effect.HUMAN_APPROVAL,
                motivo=(f"'{ctx.action.kind}' exige autonomia {exigido.name} e "
                        f"este escopo vai ate {ctx.autonomy.name}"),
                regra="teto_de_autonomia",
                casadas=nomes,
            )

        return Decision(
            efeito=vencedora.efeito,
            motivo=vencedora.motivo or f"regra '{vencedora.nome}'",
            regra=vencedora.nome,
            casadas=nomes,
        )
