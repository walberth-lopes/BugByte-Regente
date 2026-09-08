# -*- coding: utf-8 -*-
"""Ligar, pausar e parar o processamento -- pelo mesmo caminho de todo o resto.

    identidade -> concessao gravada -> capacidade -> policy -> comando
               -> persistencia -> auditoria

Nenhuma linha nova de autoridade. Este servico e irmao de `DecisionService` e
`AccessService`, e existe pelo mesmo motivo: a acao precisa de um lugar so, que
a CLI e a tela chamem igual.

**A tela nao inicia processo nenhum.** Ela grava uma INTENCAO. Um botao que
subisse um processo daria a uma pagina web o poder de criar processos no
computador de alguem -- que e exatamente a autoridade paralela que os marcos 13
a 16 existiram para eliminar. Quem executa e um `regente run` que uma pessoa
iniciou; se nao houver nenhum, a leitura mostra `DEGRADED` em vez de fingir.

O heartbeat NAO passa por aqui. Ele e afirmacao de um processo sobre si mesmo,
nao um pedido: exigir autoridade para dizer "estou vivo" faria o motor precisar
de permissao para ser observado.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from ..core.access import Ability
from ..core.model import Event, now
from ..core.operation import Heartbeat, Intent, Operation, Phase
from ..core.policy import (Action, AutonomyLevel, Effect, PolicyContext,
                           PolicyEngine)
from ..core.principal import Principal
from ..core import ids
from ..ports.store import Store

#: A acao que a policy avalia. O mesmo nome da capacidade, pelo motivo de
#: sempre: uma segunda nomenclatura precisaria de traducao, e e na traducao que
#: as duas divergem.
ACTION = "workspace.engine.control"

#: Limites do intervalo entre ciclos. Nao e opiniao sobre ritmo: um intervalo de
#: zero vira laco quente que consome CPU e teto diario em segundos, e um de um
#: dia faz a tela parecer travada. Quem quiser outro ritmo mexe no numero; quem
#: digitar `0` por engano nao derruba a maquina.
MIN_INTERVAL = 5
MAX_INTERVAL = 3600


@dataclass(frozen=True, slots=True)
class Outcome:
    """Aceito, ou recusado com motivo. Nunca as duas coisas."""
    accepted: bool
    intent: Intent | None = None
    refusal: str = ""
    reason: str = ""
    detail: str = ""

    @property
    def denial(self) -> str:
        return self.refusal


def _no(refusal: str, reason: str) -> Outcome:
    return Outcome(False, refusal=refusal, reason=reason)


@dataclass(slots=True)
class OperationService:
    """Le e escreve a intencao de operacao de um workspace."""

    store: Store
    policy: PolicyEngine
    clock: Callable[[], datetime] = now
    organization: str = "*"
    client: str = "*"
    workspace_name: str = "*"
    environment: str = "staging"

    # ------------------------------------------------------------------
    def state(self, workspace_id: str) -> tuple[Operation, Heartbeat | None, Phase]:
        """O que pediram, quem esta trabalhando, e o que isso significa.

        Leitura livre de autoridade -- ver nao e agir, e a mesma separacao que
        `Ability` faz ao nao ter capacidade de leitura. Quem pode chegar ao
        read model ja atravessou o escopo do tenant.
        """
        op = self.store.operation(workspace_id)
        beat = self.store.heartbeat(workspace_id)
        return op, beat, op.phase(beat, self.clock())

    # ------------------------------------------------------------------
    def set_intent(self, actor: Principal, workspace_id: str, intent: Intent,
                   note: str = "", interval_seconds: int | None = None) -> Outcome:
        """A unica escrita. Toda barreira, na ordem, e nenhuma pulavel."""
        at = self.clock()

        if not actor.authenticated:
            return _no("UNAUTHENTICATED", "esta requisicao nao foi autenticada")

        if not actor.can(workspace_id, Ability.ENGINE_CONTROL):
            # `NOT_FOUND`, e nao `FORBIDDEN`: quem nao tem acesso a este
            # workspace nao deve descobrir que ele existe pela mensagem de erro.
            return _no("NOT_FOUND", "recurso nao encontrado neste escopo")

        decision = self.policy.decide(PolicyContext(
            action=Action(kind=ACTION, resource=f"workspace:{workspace_id}",
                          environment=self.environment),
            organization=self.organization, client=self.client,
            workspace=self.workspace_name, agent=actor.label,
            autonomy=AutonomyLevel.L4))
        if decision.effect is not Effect.ALLOW:
            return _no("POLICY_DENIED", f"policy {decision.effect}: {decision.reason}")

        atual = self.store.operation(workspace_id)
        intervalo = atual.interval_seconds if interval_seconds is None \
            else int(interval_seconds)
        if not MIN_INTERVAL <= intervalo <= MAX_INTERVAL:
            return _no("INVALID",
                       f"intervalo de {intervalo}s fora de "
                       f"[{MIN_INTERVAL}, {MAX_INTERVAL}]")

        novo = Operation(workspace_id=workspace_id, intent=intent,
                         changed_by=actor.label, changed_at=at, note=note[:300],
                         interval_seconds=intervalo)
        self.store.save_operation(novo)

        # Auditado SEMPRE, inclusive quando nada muda. "Alguem pediu de novo" e
        # um fato que a investigacao de um incidente quer ver -- e uma escrita
        # silenciosa porque o valor coincidia e um buraco na trilha.
        self.store.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id=workspace_id,
            kind="operacao", actor=actor.label,
            summary=f"{atual.intent.value} -> {intent.value}",
            data={"de": atual.intent.value, "para": intent.value,
                  "intervalo_s": intervalo, "por": actor.label,
                  "provedor_identidade": actor.provider, "nota": note[:300]}))
        return Outcome(True, intent=intent,
                       detail=f"{atual.intent.value} -> {intent.value}")

    # ------------------------------------------------------------------
    def beat(self, workspace_id: str, pid: int, host: str, ticks: int,
             detail: str = "") -> None:
        """Um processo afirma que esta vivo. Sem autoridade, de proposito."""
        self.store.beat(workspace_id, Heartbeat(
            at=self.clock(), pid=pid, host=host, ticks=ticks, detail=detail))

    def stood_down(self, workspace_id: str) -> None:
        """Desligamento limpo: o sinal de vida some agora, e nao por expirar.

        Sem isto, um processo que terminou de forma ordeira continuaria
        parecendo vivo ate o prazo de graca passar, e a tela mostraria
        `STOPPING` para algo que ja acabou.
        """
        self.store.clear_heartbeat(workspace_id)
