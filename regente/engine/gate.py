# -*- coding: utf-8 -*-
"""O portao. Toda acao de agente sobre o mundo passa por aqui.

    Agent -> ToolRequest -> [risco] -> [policy] -> ALLOW/DENY/HUMAN_APPROVAL -> Tool

Tres propriedades que fazem disto um portao e nao uma funcao de conveniencia:

1. **Nao existe caminho alternativo.** Nenhum agente recebe adapter de escrita
   direto; ele recebe este objeto. Um agente convencido por um comentario de PR
   ainda esbarra na policy, que nao le texto do agente.

2. **Ele refaz o julgamento do zero.** O portao nao aceita risco, justificativa
   nem veredito calculado por quem chama. Recebe fatos da acao e reavalia. Aceitar
   um risco pre-calculado seria deixar o modelo escolher o proprio limite.

3. **Registra o que negou.** A negativa e o registro mais valioso do motor: e ela
   que prova, semanas depois, que o portao estava vivo -- e e ela que mostra qual
   regra afrouxar quando ele apertou demais.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core import ids
from ..core.errors import PolicyNegou, PrecisaDeHumano
from ..core.model import ActionRecord
from ..core.policy import Action, AutonomyLevel, Decision, Effect, PolicyContext, PolicyEngine
from ..core.risk import RiskAssessment, RiskEngine
from ..ports.store import Store


@dataclass(frozen=True, slots=True)
class Escopo:
    """Quem esta agindo e sob qual tenancy. Montado pelo motor, nunca pelo agente."""
    workspace_id: str
    organization: str = "*"
    client: str = "*"
    workspace: str = "*"
    project: str = "*"
    autonomia: AutonomyLevel = AutonomyLevel.L2
    agente: str = "engine"
    task_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class Vereditos:
    decisao: Decision
    risco: RiskAssessment


@dataclass(slots=True)
class Gate:
    store: Store
    policy: PolicyEngine
    risco: RiskEngine
    #: Quando um HUMAN_APPROVAL aparece, quem transforma isso em item da fila.
    #: Injetado para que o portao nao conheca a fila -- ele so sabe barrar.
    ao_precisar_humano: Callable[[Escopo, Action, Vereditos], None] | None = None
    _fatos_extra: dict[str, Any] = field(default_factory=dict)

    def avalia(self, escopo: Escopo, acao: Action, fatos: dict[str, Any] | None = None) -> Vereditos:
        """Julga sem executar. Usado por quem quer saber antes de tentar."""
        contexto_risco = {
            "acao": acao.kind,
            "ambiente": acao.environment,
            "categoria": acao.kind.split(".", 1)[0],
            "caminhos": (fatos or {}).get("caminhos", ()),
            "linhas": (fatos or {}).get("linhas", 0),
            "arquivos": (fatos or {}).get("arquivos", 0),
            **self._fatos_extra,
        }
        avaliacao = self.risco.avalia(contexto_risco)
        decisao = self.policy.decide(PolicyContext(
            action=acao,
            organization=escopo.organization, client=escopo.client,
            workspace=escopo.workspace, project=escopo.project,
            agent=escopo.agente, risk=avaliacao.nivel.name, autonomy=escopo.autonomia,
        ))
        return Vereditos(decisao=decisao, risco=avaliacao)

    def executa(self, escopo: Escopo, acao: Action, operacao: Callable[[], Any],
                fatos: dict[str, Any] | None = None) -> Any:
        """Julga e, se permitido, executa. Toda passagem vira ActionRecord.

        `operacao` e um callable sem argumentos justamente para que o portao nao
        precise conhecer a assinatura de nenhum adapter -- ele autoriza a *acao*
        declarada, nao a funcao.
        """
        v = self.avalia(escopo, acao, fatos)
        inicio = time.monotonic()

        if v.decisao.efeito == Effect.DENY:
            self._registra(escopo, acao, v, "negado", 0)
            raise PolicyNegou(v.decisao.motivo, v.decisao.regra)

        if v.decisao.efeito == Effect.HUMAN_APPROVAL:
            self._registra(escopo, acao, v, "aguardando humano", 0)
            if self.ao_precisar_humano:
                self.ao_precisar_humano(escopo, acao, v)
            raise PrecisaDeHumano(v.decisao.motivo, v.decisao.regra)

        try:
            resultado = operacao()
        except Exception as e:
            self._registra(escopo, acao, v, f"erro: {type(e).__name__}: {e}"[:300],
                           int((time.monotonic() - inicio) * 1000))
            raise
        self._registra(escopo, acao, v, "ok", int((time.monotonic() - inicio) * 1000))
        return resultado

    def _registra(self, escopo: Escopo, acao: Action, v: Vereditos,
                  resultado: str, duracao_ms: int) -> None:
        self.store.registra_acao(ActionRecord(
            id=ids.novo(ids.ACTION), workspace_id=escopo.workspace_id,
            agente=escopo.agente, acao=acao.kind, recurso=acao.resource,
            efeito=v.decisao.efeito, risco=v.risco.nivel.name,
            task_id=escopo.task_id, run_id=escopo.run_id,
            regra=v.decisao.regra,
            motivo="; ".join([v.decisao.motivo, *v.risco.motivos])[:500],
            resultado=resultado, duracao_ms=duracao_ms))
