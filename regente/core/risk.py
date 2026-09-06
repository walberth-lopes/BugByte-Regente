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

    @property
    def nome(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class Signal:
    """Um fator de risco que disparou, com a evidencia que o disparou.

    A evidencia e obrigatoria porque um risco sem evidencia nao e auditavel --
    e sem auditoria o dono nao consegue afrouxar uma regra com seguranca.
    """
    nome: str
    nivel: RiskLevel
    evidencia: str


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    nivel: RiskLevel
    sinais: tuple[Signal, ...] = ()

    @property
    def exige_segunda_passada(self) -> bool:
        """HIGH e CRITICAL nao esperam humano: eles exigem releitura adversarial."""
        return self.nivel >= RiskLevel.HIGH

    @property
    def motivos(self) -> tuple[str, ...]:
        return tuple(f"{s.nome}: {s.evidencia}" for s in self.sinais)


@dataclass(frozen=True, slots=True)
class Fator:
    """Regra declarativa de risco, vinda de configuracao.

    `campo` e lido do contexto da acao; `casa` e uma lista de padroes glob
    (para texto) ou um limiar numerico (para `maior_que`).
    """
    nome: str
    nivel: RiskLevel
    campo: str
    casa: tuple[str, ...] = ()
    maior_que: float | None = None
    igual_a: Any = None


#: Base minima que vale para qualquer cliente. O arquivo de configuracao SOMA
#: fatores; ele nao substitui estes, porque sao os que descrevem dano fisico ao
#: mundo (producao, dado, credencial) e nao preferencia de time.
FATORES_BASE: tuple[Fator, ...] = (
    Fator("producao", RiskLevel.HIGH, "ambiente", ("prod", "production", "producao")),
    Fator("destrutivo", RiskLevel.CRITICAL, "acao",
          ("*.delete", "*.drop", "*.destroy", "*.purge", "*.truncate", "*.rollback")),
    Fator("migration", RiskLevel.HIGH, "caminhos",
          ("*migrations/*", "*alembic/*", "*.sql", "*schema*")),
    Fator("infraestrutura", RiskLevel.HIGH, "caminhos",
          ("*terraform/*", "*dockerfile*", "*workflows/*", "*pipelines/*",
           "*deploy/*", "*infra/*", "*chart/*")),
    Fator("credencial", RiskLevel.CRITICAL, "caminhos",
          ("*secret*", "*credential*", "*.env*", "*iam*", "*token*")),
    Fator("autenticacao", RiskLevel.HIGH, "caminhos", ("*auth*", "*login*", "*session*", "*permission*")),
    Fator("pagamento", RiskLevel.HIGH, "caminhos", ("*payment*", "*billing*", "*invoice*", "*checkout*")),
    Fator("api_publica", RiskLevel.MEDIUM, "caminhos", ("*api/*", "*routes/*", "*openapi*", "*proto*")),
    Fator("diff_grande", RiskLevel.MEDIUM, "linhas", maior_que=600),
    Fator("muitos_arquivos", RiskLevel.MEDIUM, "arquivos", maior_que=25),
    Fator("banco", RiskLevel.HIGH, "categoria", ("database",)),
)


def _valores(contexto: dict[str, Any], campo: str) -> list[str]:
    v = contexto.get(campo)
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v]
    return [str(v)]


def _dispara(fator: Fator, contexto: dict[str, Any]) -> str | None:
    """Devolve a evidencia se o fator disparou, ou None."""
    if fator.maior_que is not None:
        bruto = contexto.get(fator.campo)
        try:
            n = float(bruto)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return f"{fator.campo}={bruto} > {fator.maior_que:g}" if n > fator.maior_que else None

    if fator.igual_a is not None:
        return f"{fator.campo}={fator.igual_a}" if contexto.get(fator.campo) == fator.igual_a else None

    for valor in _valores(contexto, fator.campo):
        alvo = valor.lower()
        for padrao in fator.casa:
            p = padrao.lower()
            # Padrao sem curinga casa por substring: 'auth' precisa pegar
            # 'src/auth/handler.py' sem que cada regra vire '*auth*'.
            bateu = fnmatch.fnmatch(alvo, p) if ("*" in p or "?" in p) else (p in alvo)
            if bateu:
                return f"{fator.campo}={valor}"
    return None


@dataclass(slots=True)
class RiskEngine:
    fatores: tuple[Fator, ...] = field(default=FATORES_BASE)
    piso: RiskLevel = RiskLevel.LOW

    @classmethod
    def de_config(cls, extras: list[dict[str, Any]] | None = None) -> RiskEngine:
        """Fatores do cliente SOMAM aos da base -- nunca a substituem."""
        adicionais: list[Fator] = []
        for bruto in extras or []:
            adicionais.append(Fator(
                nome=bruto["nome"],
                nivel=RiskLevel[str(bruto.get("nivel", "MEDIUM")).upper()],
                campo=bruto.get("campo", "caminhos"),
                casa=tuple(bruto.get("casa", ()) or ()),
                maior_que=bruto.get("maior_que"),
                igual_a=bruto.get("igual_a"),
            ))
        return cls(fatores=FATORES_BASE + tuple(adicionais))

    def avalia(self, contexto: dict[str, Any]) -> RiskAssessment:
        """`contexto` traz: acao, ambiente, categoria, caminhos, linhas, arquivos.

        Ausencia de informacao nunca reduz risco -- ela so nao aumenta. Quem
        chama e responsavel por preencher `caminhos`; um snapshot truncado deve
        ser declarado como sinal proprio por quem o produziu.
        """
        sinais: list[Signal] = []
        for fator in self.fatores:
            evidencia = _dispara(fator, contexto)
            if evidencia:
                sinais.append(Signal(fator.nome, fator.nivel, evidencia))

        nivel = max((s.nivel for s in sinais), default=self.piso)
        return RiskAssessment(nivel=nivel, sinais=tuple(sinais))
