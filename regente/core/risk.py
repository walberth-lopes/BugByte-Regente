# -*- coding: utf-8 -*-
"""Risk Engine: mede o risco de uma acao proposta.

**Risco e rigor, nao fila de espera.** Esta e a distincao que define o motor:

- A *policy* decide **autoridade**: quem pode fazer. Merge em producao exige
  humano porque a organizacao decidiu assim, e nenhum grau de confianca do
  modelo muda isso.
- O *risco* decide **rigor**: quanta prova a acao exige antes de acontecer.
  Risco alto compra leitura adversarial, segunda passada, teste extra --
  compra *trabalho*, nao espera.

Se risco alto virasse "espera o humano assinar", o motor devolveria ao dono
exatamente o gargalo que ele existe para eliminar: trabalho bom parado numa fila.
Quem para o trabalho e a policy, e ela para por regra escrita, nao por hesitacao.

O Risk Engine e uma funcao pura de sinais declarados. Ele nao chama LLM: um
julgamento de risco que depende do modelo nao serve de portao contra o modelo.
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
    """Um fator de risco que disparou, com a evidencia que o disparou.

    A evidencia e obrigatoria porque um risco sem evidencia nao e auditavel --
    e sem auditoria o dono nao consegue afrouxar uma regra com seguranca.
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
        """HIGH e CRITICAL nao esperam humano: eles exigem releitura adversarial."""
        return self.level >= RiskLevel.HIGH

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(f"{s.name}: {s.evidence}" for s in self.signals)


@dataclass(frozen=True, slots=True)
class Factor:
    """Regra declarativa de risco, vinda de configuracao.

    `campo` e lido do contexto da acao; `casa` e uma lista de padroes glob
    (para texto) ou um limiar numerico (para `maior_que`).
    """
    name: str
    level: RiskLevel
    field: str
    matches: tuple[str, ...] = ()
    greater_than: float | None = None
    equal_to: Any = None


#: Base minima que vale para qualquer cliente. O arquivo de configuracao SOMA
#: fatores; ele nao substitui estes, porque sao os que descrevem dano fisico ao
#: mundo (producao, dado, credencial) e nao preferencia de time.
BASE_FACTORS: tuple[Factor, ...] = (
    Factor("producao", RiskLevel.HIGH, "environment", ("prod", "production", "producao")),
    Factor("destrutivo", RiskLevel.CRITICAL, "action",
          ("*.delete", "*.drop", "*.destroy", "*.purge", "*.truncate", "*.rollback")),
    Factor("migration", RiskLevel.HIGH, "paths",
          ("*migrations/*", "*alembic/*", "*.sql", "*schema*")),
    Factor("infraestrutura", RiskLevel.HIGH, "paths",
          ("*terraform/*", "*dockerfile*", "*workflows/*", "*pipelines/*",
           "*deploy/*", "*infra/*", "*chart/*")),
    Factor("credencial", RiskLevel.CRITICAL, "paths",
          ("*secret*", "*credential*", "*.env*", "*iam*", "*token*")),
    Factor("autenticacao", RiskLevel.HIGH, "paths", ("*auth*", "*login*", "*session*", "*permission*")),
    Factor("pagamento", RiskLevel.HIGH, "paths", ("*payment*", "*billing*", "*invoice*", "*checkout*")),
    Factor("api_publica", RiskLevel.MEDIUM, "paths", ("*api/*", "*routes/*", "*openapi*", "*proto*")),
    Factor("diff_grande", RiskLevel.MEDIUM, "lines", greater_than=600),
    Factor("muitos_arquivos", RiskLevel.MEDIUM, "files", greater_than=25),
    Factor("banco", RiskLevel.HIGH, "category", ("database",)),
)


def _values_for(contexto: dict[str, Any], field: str) -> list[str]:
    v = contexto.get(field)
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v]
    return [str(v)]


def _fires(fator: Factor, contexto: dict[str, Any]) -> str | None:
    """Devolve a evidencia se o fator disparou, ou None."""
    if fator.greater_than is not None:
        bruto = contexto.get(fator.field)
        try:
            n = float(bruto)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return f"{fator.field}={bruto} > {fator.greater_than:g}" if n > fator.greater_than else None

    if fator.equal_to is not None:
        return f"{fator.field}={fator.equal_to}" if contexto.get(fator.field) == fator.equal_to else None

    for valor in _values_for(contexto, fator.field):
        target = valor.lower()
        for default_value in fator.matches:
            p = default_value.lower()
            # Padrao sem curinga casa por substring: 'auth' precisa pegar
            # 'src/auth/handler.py' sem que cada regra vire '*auth*'.
            bateu = fnmatch.fnmatch(target, p) if ("*" in p or "?" in p) else (p in target)
            if bateu:
                return f"{fator.field}={valor}"
    return None


@dataclass(slots=True)
class RiskEngine:
    factors: tuple[Factor, ...] = field(default=BASE_FACTORS)
    floor: RiskLevel = RiskLevel.LOW

    @classmethod
    def from_config(cls, extras: list[dict[str, Any]] | None = None) -> RiskEngine:
        """Fatores do cliente SOMAM aos da base -- nunca a substituem."""
        adicionais: list[Factor] = []
        for raw in extras or []:
            adicionais.append(Factor(
                name=raw["name"],
                level=RiskLevel[str(raw.get("level", "MEDIUM")).upper()],
                field=raw.get("field", "paths"),
                matches=tuple(raw.get("matches", ()) or ()),
                greater_than=raw.get("greater_than"),
                equal_to=raw.get("equal_to"),
            ))
        return cls(factors=BASE_FACTORS + tuple(adicionais))

    def assess(self, contexto: dict[str, Any]) -> RiskAssessment:
        """`contexto` traz: acao, ambiente, categoria, caminhos, linhas, arquivos.

        Ausencia de informacao nunca reduz risco -- ela so nao aumenta. Quem
        chama e responsavel por preencher `caminhos`; um snapshot truncado deve
        ser declarado como sinal proprio por quem o produziu.
        """
        signals: list[Signal] = []
        for fator in self.factors:
            evidence = _fires(fator, contexto)
            if evidence:
                signals.append(Signal(fator.name, fator.level, evidence))

        level = max((s.level for s in signals), default=self.floor)
        return RiskAssessment(level=level, signals=tuple(signals))
