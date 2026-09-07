# -*- coding: utf-8 -*-
"""Administracao de acesso: conceder, revogar, listar -- e nada por atalho.

Este e o unico lugar por onde uma concessao entra ou sai do motor. A API e a
tela sao transporte; o terminal chama daqui tambem. Nao existe
`ui_grant_access`, pela mesma razao de sempre: duas funcoes divergem, e a que
diverge e a que tem menos verificacoes.

As barreiras, na ordem, e nenhuma pulavel:

    autenticacao   quem e voce?               sem `method`, ninguem provou nada
    autoridade     voce pode CONCEDER aqui?   uma concessao viva com a capacidade
    escopo         o workspace e seu?         verificado antes de qualquer leitura
    policy         esta acao e permitida?     independente da autoridade acima
    alvo           a quem?                    provedor + sujeito, nunca um nome
    estado         ja existe concessao viva?  conflito, nao sobrescrita
    auditoria      ator E alvo, separados

**Ator e alvo nao sao a mesma coluna.** `alice concede a bob` e `bob concede a
bob` sao fatos diferentes, e o segundo e recusado. Guardar so "quem foi afetado"
apagaria justamente a pergunta que uma auditoria de acesso existe para
responder.

**Papel e entrada da policy, nunca substituto dela.** Nao existe
`if principal.is_admin: allow()` aqui nem em lugar nenhum: ter a capacidade e
uma condicao, e a policy continua sendo consultada depois.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Mapping

from ..core import ids
from ..core.access import Ability, AccessGrant, PrincipalRef, abilities_of
from ..core.model import Event, now
from ..core.policy import (Action, AutonomyLevel, Effect, PolicyContext,
                           PolicyEngine)
from ..core.principal import Principal
from ..ports.store import Store

#: O que a auditoria de acesso registra. Dois verbos, e o bootstrap a parte --
#: porque a primeira concessao de um sistema vazio tem uma procedencia
#: diferente de todas as outras, e apaga-la seria esconder de onde tudo veio.
GRANTED = "acesso_concedido"
REVOKED = "acesso_revogado"
BOOTSTRAPPED = "acesso_inicial"


class Refusal(str, Enum):
    """Por que uma operacao de acesso nao aconteceu. Vocabulario fechado."""
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    INVALID = "INVALID"
    POLICY_DENIED = "POLICY_DENIED"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class AccessOutcome:
    accepted: bool
    reason: str
    refusal: Refusal | None = None
    grant: AccessGrant | None = None
    #: Quem executou e quem sofreu, separados -- inclusive na resposta.
    actor: str = ""
    target: str = ""


def _no(refusal: Refusal, reason: str, **kw) -> AccessOutcome:
    return AccessOutcome(accepted=False, refusal=refusal, reason=reason, **kw)


@dataclass(slots=True)
class AccessService:
    """Concede, revoga e lista. Tudo escopado, tudo auditado."""

    store: Store
    policy: PolicyEngine
    clock: Callable[[], datetime] = now
    organization: str = "*"
    client: str = "*"
    workspace_name: str = "*"
    environment: str = "staging"

    # ------------------------------------------------------------------
    # O alcance de quem acabou de se autenticar
    # ------------------------------------------------------------------
    def abilities_for(self, ref: PrincipalRef
                      ) -> Mapping[str, frozenset[Ability]]:
        """O que esta identidade pode, por workspace, segundo o que foi gravado.

        Le concessoes VIVAS. Uma revogada nao aparece -- e por isso revogar
        fecha a porta na proxima montagem de principal, sem depender de a tela
        esconder um botao.
        """
        found: dict[str, frozenset[Ability]] = {}
        for grant in self.store.grants_of(ref.key):
            # A consulta ja filtra revogadas. Perguntar de novo ao proprio
            # objeto e a segunda linha de defesa -- e foi o sweep de mutacao
            # que mostrou que ela nao existia: `allows` era codigo morto, e
            # remover a checagem de revogacao dele nao mudava nada. Codigo
            # morto que parece autoritativo e pior que codigo ausente.
            vivas = frozenset(a for a in grant.abilities if grant.allows(a))
            if not vivas:
                continue
            found[grant.workspace_id] = (found.get(grant.workspace_id,
                                                   frozenset()) | vivas)
        return found

    def authorize(self, who: Principal) -> Principal:
        """O principal com o que as concessoes disserem, e nada alem.

        Chamado DEPOIS de autenticar, nunca junto: um provedor de identidade
        sabe quem voce e e nao faz ideia de quais workspaces deste motor sao
        seus. Enquanto as duas coisas vinham do mesmo objeto, quem editava a
        configuracao concedia a si mesmo autoridade de escrita.
        """
        if not who.authenticated:
            return who
        return who.with_abilities(self.abilities_for(who.ref))

    # ------------------------------------------------------------------
    # Conceder
    # ------------------------------------------------------------------
    def grant(self, actor: Principal, workspace_id: str, target: PrincipalRef,
              role: str, note: str = "") -> AccessOutcome:
        guard = self._may(actor, workspace_id, Ability.GRANT)
        if guard is not None:
            return guard

        # Conceder a si mesmo e recusado mesmo com autoridade para conceder.
        # Nao e desconfianca do administrador: e que uma concessao so vale como
        # prova se houver DUAS pessoas na linha. Um sistema em que o ator e o
        # alvo se confundem nao consegue responder quem autorizou quem.
        if target.key == actor.ref.key:
            return _no(Refusal.FORBIDDEN,
                       "conceder acesso a si mesmo nao e concessao; peca a "
                       "outra pessoa com autoridade",
                       actor=actor.label, target=target.key)

        abilities = abilities_of(role)
        if not abilities:
            return _no(Refusal.INVALID,
                       f"papel desconhecido: {role!r}",
                       actor=actor.label, target=target.key)

        return self._open(actor, workspace_id, target, abilities, note,
                          kind=GRANTED)

    def bootstrap(self, actor: Principal, workspace_id: str,
                  note: str = "") -> AccessOutcome:
        """A primeira concessao de um workspace sem nenhuma.

        O problema do ovo e da galinha: ninguem pode conceder sem ter recebido,
        e ninguem recebeu ainda. A saida honesta e uma porta estreita, nomeada e
        auditada de forma diferente:

        * so funciona quando **nao existe nenhuma concessao viva** no workspace;
        * so pelo terminal -- quem roda o processo ja controla o banco e o
          arquivo de configuracao, entao isto nao concede nada que essa pessoa
          nao pudesse fazer com um editor de SQL;
        * fica registrado como `acesso_inicial`, e nao como concessao comum,
          porque a procedencia dele e realmente outra.

        Depois da primeira, esta porta fecha: qualquer nova concessao passa pelo
        caminho normal, com duas pessoas na linha.
        """
        if not actor.authenticated:
            return _no(Refusal.UNAUTHENTICATED, "esta sessao nao foi autenticada")
        # O PROVEDOR, e nao o metodo: e ele que forma a chave da identidade, e
        # conferir os dois com `and` deixava passar quem tivesse so um deles.
        if actor.provider != "os-account":
            return _no(Refusal.FORBIDDEN,
                       "a concessao inicial so pode partir de uma identidade do "
                       "sistema operacional, no terminal desta maquina",
                       actor=actor.label)
        if self.store.workspace(workspace_id) is None:
            return _no(Refusal.NOT_FOUND, "workspace nao encontrado")
        if self.store.grants(workspace_id):
            return _no(Refusal.CONFLICT,
                       "este workspace ja tem concessoes; a partir daqui use o "
                       "caminho normal, com quem tem autoridade para conceder",
                       actor=actor.label)

        return self._open(actor, workspace_id, actor.ref,
                          abilities_of("owner"), note, kind=BOOTSTRAPPED)

    def _open(self, actor: Principal, workspace_id: str, target: PrincipalRef,
              abilities: frozenset[Ability], note: str,
              kind: str) -> AccessOutcome:
        workspace = self.store.workspace(workspace_id)
        if workspace is None:
            return _no(Refusal.NOT_FOUND, "workspace nao encontrado")

        grant = AccessGrant(
            id=ids.new_id(ids.GRANT), client_id=workspace.client_id,
            workspace_id=workspace_id, principal=target, abilities=abilities,
            granted_by=actor.ref.key, granted_at=self.clock(), note=note)

        if not self.store.open_grant(grant):
            # O indice unico recusou: ja ha concessao viva para esta pessoa.
            # Sobrescrever apagaria de quem veio a primeira.
            return _no(Refusal.CONFLICT,
                       "esta pessoa ja tem concessao viva neste workspace; "
                       "revogue antes de conceder outra",
                       actor=actor.label, target=target.key)

        self._audit(kind, actor, workspace_id, workspace.client_id, target,
                    abilities, grant.id, note)
        return AccessOutcome(accepted=True, grant=grant, actor=actor.label,
                             target=target.key,
                             reason=f"acesso concedido a {target.key}")

    # ------------------------------------------------------------------
    # Revogar
    # ------------------------------------------------------------------
    def revoke(self, actor: Principal, workspace_id: str,
               target: PrincipalRef) -> AccessOutcome:
        guard = self._may(actor, workspace_id, Ability.REVOKE)
        if guard is not None:
            return guard

        workspace = self.store.workspace(workspace_id)
        vivos = self.store.grants(workspace_id, principal_key=target.key)
        if not vivos:
            return _no(Refusal.NOT_FOUND,
                       "nao ha concessao viva para esta pessoa neste workspace",
                       actor=actor.label, target=target.key)

        if not self.store.revoke_grant(workspace_id, target.key,
                                       revoked_by=actor.ref.key,
                                       when=self.clock()):
            return _no(Refusal.CONFLICT,
                       "outra revogacao chegou primeiro",
                       actor=actor.label, target=target.key)

        self._audit(REVOKED, actor, workspace_id, workspace.client_id, target,
                    vivos[-1].abilities, vivos[-1].id, "")
        return AccessOutcome(accepted=True, actor=actor.label,
                             target=target.key,
                             reason=f"acesso de {target.key} revogado")

    # ------------------------------------------------------------------
    # Listar
    # ------------------------------------------------------------------
    def listing(self, actor: Principal, workspace_id: str,
                include_revoked: bool = True) -> AccessOutcome | list[AccessGrant]:
        guard = self._may(actor, workspace_id, Ability.LIST)
        if guard is not None:
            return guard
        return self.store.grants(workspace_id, include_revoked=include_revoked)

    # ------------------------------------------------------------------
    def _may(self, actor: Principal, workspace_id: str,
             ability: Ability) -> AccessOutcome | None:
        """As barreiras comuns. `None` significa que pode seguir.

        A ordem nao e arbitraria. O escopo e conferido ANTES de o workspace ser
        lido, e a recusa por falta de autoridade responde `NOT_FOUND` -- porque
        distinguir "nao existe" de "existe e nao e seu" confirma a existencia de
        um workspace alheio a quem tentou adivinhar.
        """
        if not actor.authenticated:
            return _no(Refusal.UNAUTHENTICATED,
                       "esta requisicao nao foi autenticada")
        if not actor.can(workspace_id, ability):
            return _no(Refusal.NOT_FOUND, "recurso nao encontrado neste escopo",
                       actor=actor.label)
        if self.store.workspace(workspace_id) is None:
            return _no(Refusal.NOT_FOUND, "recurso nao encontrado neste escopo",
                       actor=actor.label)

        decision = self.policy.decide(PolicyContext(
            action=Action(kind=ability.value, resource=workspace_id,
                          environment=self.environment),
            organization=self.organization, client=self.client,
            workspace=self.workspace_name, agent=actor.label,
            autonomy=AutonomyLevel.L4))
        if decision.effect is Effect.DENY:
            # Ter a capacidade e uma condicao; a policy continua sendo outra.
            # Um papel que dispensasse esta linha seria uma segunda autoridade.
            return _no(Refusal.POLICY_DENIED, f"policy DENY: {decision.reason}",
                       actor=actor.label)
        return None

    def _audit(self, kind: str, actor: Principal, workspace_id: str,
               client_id: str, target: PrincipalRef,
               abilities: frozenset[Ability], grant_id: str,
               note: str) -> None:
        """A trilha de acesso. Ator e alvo em campos separados.

        `note` nao entra literal: e texto livre de quem concede, e texto livre e
        onde uma credencial colada por engano viraria registro permanente.
        """
        self.store.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id=workspace_id, kind=kind,
            actor=actor.label,
            summary=(f"{actor.label} {'concedeu' if kind != REVOKED else 'revogou'} "
                     f"{sorted(a.value for a in abilities)} "
                     f"{'a' if kind != REVOKED else 'de'} {target.key}"),
            data={
                "grant_id": grant_id,
                "actor": actor.ref.key,
                "actor_method": actor.method,
                "target": target.key,
                "target_provider": target.provider,
                "client_id": client_id,
                "workspace_id": workspace_id,
                "abilities": sorted(a.value for a in abilities),
                "result": "ACCEPTED",
                "note_length": len(note),
            }))
