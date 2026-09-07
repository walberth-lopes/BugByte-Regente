# -*- coding: utf-8 -*-
"""A decisao humana, com todas as barreiras, num caminho so.

Este e o unico lugar por onde uma decisao humana entra no motor. O terminal
passa por aqui; o navegador passa por aqui. Nao existe `ui_decide_approval`, e a
razao nao e estilo: duas funcoes de decisao divergem, e a que diverge e sempre a
que tem menos verificacoes.

    autenticacao   quem e voce?              nao autenticado nao decide
    autorizacao    voce manda NESTE workspace?
    escopo         a aprovacao e deste workspace?
    policy         esta acao e permitida aqui?
    estado         esta aprovacao ainda esta aberta?
    transicao      a escolha esta entre as opcoes oferecidas?
    auditoria      quem, onde, o que, quando, e a partir de que estado

Cada linha responde uma pergunta diferente e nenhuma responde pela outra. Um
principal autenticado nao esta autorizado; um autorizado nao venceu a policy; e
uma policy que permite nao reabre uma aprovacao ja decidida.

**A ordem importa.** O escopo e verificado ANTES de a aprovacao ser lida. Buscar
globalmente e conferir depois ja teria lido o dado de outro cliente -- e o codigo
continuaria parecendo certo, porque a resposta ao cliente seria a mesma.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable

from ..core.errors import CorruptedState
from ..core.model import ApprovalState, Event, now
from ..core.policy import (Action, AutonomyLevel, Effect, PolicyContext,
                           PolicyEngine)
from ..core.principal import Principal
from ..core import ids
from ..ports.store import Store

#: A acao que a policy avalia. Nome proprio, e nao reaproveitado de nenhuma acao
#: de agente: uma regra sobre `deploy.*` nao pode, por acidente de nome, decidir
#: se uma pessoa pode responder a uma escalada.
DECIDE_ACTION = "approval.decide"


class Denial(str, Enum):
    """Por que uma decisao nao aconteceu. Vocabulario fechado.

    Fechado porque quem consome precisa distinguir sem ler prosa, e porque a
    diferenca entre estes seis e informacao operacional: `FORBIDDEN` manda
    procurar quem concede acesso, `POLICY_DENIED` manda ler o arquivo de regras,
    `CONFLICT` diz que alguem chegou primeiro e nada se perdeu.
    """
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    INVALID_STATE = "INVALID_STATE"
    POLICY_DENIED = "POLICY_DENIED"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class Decision:
    """O que aconteceu. Aceita ou recusada, sempre com motivo."""
    accepted: bool
    reason: str
    denial: Denial | None = None
    approval_id: str = ""
    task_id: str = ""
    task_key: str = ""
    choice: str = ""
    decided_by: str = ""
    decided_at: datetime | None = None
    #: Estados da APROVACAO, nao da task: e ela que esta transicao move.
    previous_state: str = ""
    new_state: str = ""
    #: Onde a task estava quando a decisao foi tomada. Ela nao se move aqui --
    #: quem a retoma e o proximo tick -- e dizer isso evita que a tela prometa
    #: um movimento que ainda nao aconteceu.
    task_state: str = ""


@dataclass(slots=True)
class DecisionService:
    """Decidir uma escalada. A unica escrita humana que existe no sistema."""

    store: Store
    policy: PolicyEngine
    clock: Callable[[], datetime] = now
    #: Nomes que a policy usa para saber de quem e este motor.
    organization: str = "*"
    client: str = "*"
    workspace_name: str = "*"
    environment: str = "staging"

    # ------------------------------------------------------------------
    def decide(self, who: Principal, workspace_id: str, approval_id: str,
               choice: str, note: str = "") -> Decision:
        """As barreiras, na ordem, e nenhuma pulavel."""

        # 1. Autenticacao. Um principal sem metodo nao foi provado por ninguem.
        if not who.authenticated:
            return _no(Denial.UNAUTHENTICATED,
                       "esta requisicao nao foi autenticada")

        # 2. Autorizacao NESTE workspace. Ler nao concede decidir: derivar uma
        #    da outra faria de todo observador um decisor.
        if not who.may_decide(workspace_id):
            return _no(Denial.NOT_FOUND,
                       "aprovacao nao encontrada neste escopo")

        # 3. O workspace existe? Mesma resposta do caso anterior, de proposito:
        #    distinguir "nao existe" de "nao e seu" confirma a existencia de um
        #    workspace alheio a quem tentou adivinhar.
        if self.store.workspace(workspace_id) is None:
            return _no(Denial.NOT_FOUND,
                       "aprovacao nao encontrada neste escopo")

        # 4. Policy: autoridade independente, e a unica que pode dizer nao
        #    depois de o operador ja ter sido autorizado.
        verdict = self._policy_says(who, workspace_id)
        if verdict is not None:
            return verdict

        # 5. A aprovacao, lida JA ESCOPADA. Nunca globalmente.
        approval = self.store.approval(approval_id, workspace_id)
        if approval is None:
            return _no(Denial.NOT_FOUND,
                       "aprovacao nao encontrada neste escopo")

        # 6. Estado. Uma decisao e uma transicao, nao uma atualizacao de coluna.
        if approval.state is not ApprovalState.OPEN:
            return Decision(
                accepted=False, denial=Denial.CONFLICT,
                reason=(f"esta aprovacao ja foi decidida como "
                        f"'{approval.choice}' por {approval.decided_by}"),
                approval_id=approval_id, task_id=approval.task_id,
                choice=approval.choice or "",
                decided_by=approval.decided_by or "",
                decided_at=approval.decided_at,
                previous_state=ApprovalState.DECIDED.value,
                new_state=ApprovalState.DECIDED.value)

        offered = {o.id for o in approval.options}
        if offered and choice not in offered:
            return _no(Denial.INVALID_STATE,
                       f"'{choice}' nao esta entre as opcoes oferecidas: "
                       f"{', '.join(sorted(offered))}",
                       approval_id=approval_id)

        # 7. A escrita. No Core, na transacao dele, com a guarda de estado
        #    dentro dela -- e por isso duas abas clicando juntas produzem uma
        #    decisao e um CONFLICT, nunca duas decisoes.
        task = self.store.task(approval.task_id, workspace_id)
        try:
            decided = self.store.decide_approval(
                approval_id, choice, per=who.label, note=note,
                workspace_id=workspace_id)
        except CorruptedState as e:
            # A corrida real: outro processo decidiu entre a leitura acima e
            # esta linha. A guarda que vale e a de dentro da transacao; esta
            # traducao existe para que a corrida vire CONFLICT e nao 500.
            return _no(Denial.CONFLICT, str(e), approval_id=approval_id)

        self._audit(who, workspace_id, decided, task, note)
        return Decision(
            accepted=True, reason=f"decisao '{choice}' registrada",
            approval_id=approval_id, task_id=decided.task_id,
            task_key=task.key if task else "", choice=choice,
            decided_by=decided.decided_by or "", decided_at=decided.decided_at,
            previous_state=ApprovalState.OPEN.value,
            new_state=decided.state.value,
            task_state=task.state.value if task else "")

    # ------------------------------------------------------------------
    def _policy_says(self, who: Principal, workspace_id: str) -> Decision | None:
        """A policy pode PROIBIR; nunca pode exigir mais um humano.

        `HUMAN_APPROVAL` significa "isto precisa da assinatura de uma pessoa". O
        ator aqui ja e uma pessoa autenticada respondendo a uma escalada -- pedir
        aprovacao humana para uma aprovacao humana e regresso infinito, e o
        efeito pratico seria travar a fila que este caminho existe para destravar.
        `DENY` continua sendo `DENY`.

        A autonomia tambem nao entra: `AutonomyLevel` mede quanto o MOTOR faz
        sozinho, e nao ha nada de autonomo numa pessoa clicando.
        """
        decision = self.policy.decide(PolicyContext(
            action=Action(kind=DECIDE_ACTION, resource=workspace_id,
                          environment=self.environment),
            organization=self.organization, client=self.client,
            workspace=self.workspace_name, agent=who.label,
            autonomy=AutonomyLevel.L4))
        # `is` funciona aqui porque `Effect` e um enum de verdade. Ate este
        # marco nao era: parecia um, e `is` respondia sempre False.
        if decision.effect is Effect.DENY:
            return _no(Denial.POLICY_DENIED,
                       f"policy DENY: {decision.reason}")
        return None

    def _audit(self, who: Principal, workspace_id: str, approval, task,
               note: str) -> None:
        """A trilha atribuivel.

        O Core ja grava um evento ao decidir, e ele responde o que foi escolhido.
        Este responde **quem**, **por onde** e **a partir de que estado** -- que
        e o que uma auditoria de escrita humana precisa e o outro nao carrega.

        `note` nao entra: e texto livre de quem clicou, e texto livre e onde uma
        credencial colada por engano acabaria virando registro permanente.
        """
        self.store.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id=workspace_id,
            kind="decisao_humana_autenticada",
            task_id=approval.task_id, run_id=approval.run_id,
            actor=who.label,
            summary=(f"{who.label} decidiu '{approval.choice}' em "
                     f"{task.key if task else approval.task_id}"),
            data={
                "approval_id": approval.id,
                "subject": who.subject,
                "method": who.method,
                "workspace_id": workspace_id,
                "client": self.client,
                "organization": self.organization,
                "task_key": task.key if task else "",
                "choice": approval.choice,
                "previous_state": ApprovalState.OPEN.value,
                "new_state": approval.state.value,
                "task_state": task.state.value if task else "",
                "note_length": len(note),
            }))


def _no(denial: Denial, reason: str, **kw) -> Decision:
    return Decision(accepted=False, denial=denial, reason=reason, **kw)
