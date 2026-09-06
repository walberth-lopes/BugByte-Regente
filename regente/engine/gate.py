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
from ..core.errors import PolicyDenied, HumanApprovalRequired
from ..core.model import ActionRecord
from ..core.policy import Action, AutonomyLevel, Decision, Effect, PolicyContext, PolicyEngine
from ..core.risk import RiskAssessment, RiskEngine
from ..ports.store import Store


@dataclass(frozen=True, slots=True)
class Scope:
    """Quem esta agindo e sob qual tenancy. Montado pelo motor, nunca pelo agente."""
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
    #: Quando um HUMAN_APPROVAL aparece, quem transforma isso em item da fila.
    #: Injetado para que o portao nao conheca a fila -- ele so sabe barrar.
    ao_precisar_humano: Callable[[Scope, Action, Verdicts], None] | None = None
    _fatos_extra: dict[str, Any] = field(default_factory=dict)

    def assess(self, escopo: Scope, action: Action, fatos: dict[str, Any] | None = None) -> Verdicts:
        """Julga sem executar. Usado por quem quer saber antes de tentar."""
        contexto_risco = {
            "action": action.kind,
            "environment": action.environment,
            "category": action.kind.split(".", 1)[0],
            "paths": (fatos or {}).get("paths", ()),
            "lines": (fatos or {}).get("lines", 0),
            "files": (fatos or {}).get("files", 0),
            **self._fatos_extra,
        }
        assessment = self.risk.assess(contexto_risco)
        decision = self.policy.decide(PolicyContext(
            action=action,
            organization=escopo.organization, client=escopo.client,
            workspace=escopo.workspace, project=escopo.project,
            agent=escopo.agent, risk=assessment.level.name, autonomy=escopo.autonomy,
        ))
        return Verdicts(decision=decision, risk=assessment)

    def execute(self, escopo: Scope, action: Action, operation: Callable[[], Any],
                fatos: dict[str, Any] | None = None) -> Any:
        """Julga e, se permitido, executa. Toda passagem vira ActionRecord.

        `operacao` e um callable sem argumentos justamente para que o portao nao
        precise conhecer a assinatura de nenhum adapter -- ele autoriza a *acao*
        declarada, nao a funcao.
        """
        v = self.assess(escopo, action, fatos)
        inicio = time.monotonic()

        if v.decision.effect == Effect.DENY:
            self._register_call(escopo, action, v, "negado", 0)
            raise PolicyDenied(v.decision.reason, v.decision.rule)

        if v.decision.effect == Effect.HUMAN_APPROVAL:
            self._register_call(escopo, action, v, "aguardando humano", 0)
            if self.ao_precisar_humano:
                self.ao_precisar_humano(escopo, action, v)
            raise HumanApprovalRequired(v.decision.reason, v.decision.rule)

        try:
            resultado = operation()
        except Exception as e:
            self._register_call(escopo, action, v, f"error: {type(e).__name__}: {e}"[:300],
                           int((time.monotonic() - inicio) * 1000))
            raise
        self._register_call(escopo, action, v, "ok", int((time.monotonic() - inicio) * 1000))
        return resultado

    def _register_call(self, escopo: Scope, action: Action, v: Verdicts,
                  resultado: str, duration_ms: int) -> None:
        self.store.record_action(ActionRecord(
            id=ids.new_id(ids.ACTION), workspace_id=escopo.workspace_id,
            agent=escopo.agent, action=action.kind, resource=action.resource,
            effect=v.decision.effect, risk=v.risk.level.name,
            task_id=escopo.task_id, run_id=escopo.run_id,
            rule=v.decision.rule,
            reason="; ".join([v.decision.reason, *v.risk.reasons])[:500],
            resultado=resultado, duration_ms=duration_ms))
