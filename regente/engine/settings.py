# -*- coding: utf-8 -*-
"""Configurar o workspace pela tela -- pelo mesmo caminho de todo o resto.

    identidade -> concessao -> capacidade -> policy -> comando
               -> persistencia -> auditoria

Irmao de `OperationService`, `AccessService` e `CredentialService`, e existe pelo
mesmo motivo: a acao precisa de um lugar so, que a CLI e a tela chamem igual.

**Configurar nao e operar, e nao e administrar credencial.** Quem liga e desliga
o processamento nao deveria, por tabela, poder apontar o motor para outro board
-- por isso `workspace.settings.write` e uma capacidade propria, e nao um bonus
de `operator`.

**O que pode ser sobreposto e uma lista fechada.** Uma sobreposicao que
aceitasse qualquer chave viraria um segundo formato de configuracao, sem
validacao e sem revisao; e o primeiro uso seria sobrepor `policies` pela tela,
que e exatamente a autoridade que a tela nao tem.

**Toda escrita e VALIDADA com o mesmo codigo que o motor usa para ler.** Um
`status_map` malformado gravado pela tela quebraria o proximo tick, longe de
quem o escreveu -- entao ele e recusado agora, com a mensagem que diz o que
existe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from ..core import ids
from ..core.access import Ability
from ..core.model import Event, now
from ..core.policy import (Action, AutonomyLevel, Effect, PolicyContext,
                           PolicyEngine)
from ..core.principal import Principal
from ..core.selection import rules_from
from ..core.settings import OVERRIDABLE, Overlay
from ..ports.store import Store
from ..ports.tasks import status_map_from

#: A acao que a policy avalia. Mesmo nome da capacidade, como sempre.
ACTION = "workspace.settings.write"


@dataclass(frozen=True, slots=True)
class Outcome:
    accepted: bool
    key: str = ""
    refusal: str = ""
    reason: str = ""
    detail: str = ""


def _no(refusal: str, reason: str) -> Outcome:
    return Outcome(False, refusal=refusal, reason=reason)


def validate(key: str, value: Any) -> str:
    """Devolve "" quando o valor serve, ou o motivo pelo qual nao serve.

    Usa o MESMO codigo que o motor usa para ler -- `status_map_from` e
    `rules_from`. Uma segunda validacao aqui divergiria da leitura, e a que
    diverge e sempre a que aceita o que quebra depois.
    """
    if key == "status_map":
        if not isinstance(value, dict):
            return "o mapeamento de status precisa ser um objeto"
        try:
            status_map_from(value)
        except ValueError as e:
            return str(e)
        return ""

    if key == "selection":
        if not isinstance(value, list):
            return "as regras precisam ser uma lista"
        try:
            rules_from(value)
        except ValueError as e:
            return str(e)
        return ""

    if key == "providers":
        if not isinstance(value, dict) or not value:
            return "providers precisa ser um objeto com ao menos um provider"
        for nome, conf in value.items():
            if not isinstance(conf, dict) or not str(conf.get("name") or "").strip():
                return f"'{nome}' precisa de uma chave 'name' com o adapter"
            # A tela nao escolhe policy, nem ator, nem escopo. Um provider que
            # trouxesse esses campos os injetaria na composicao pela porta dos
            # fundos.
            for proibido in ("credentials", "observer", "policies", "actor",
                             "workspace_id", "client_id"):
                if proibido in conf:
                    return (f"'{nome}.{proibido}' nao vem da configuracao: "
                            f"quem injeta isso e a composicao")
        return ""

    return f"'{key}' nao e sobreponivel"


@dataclass(slots=True)
class SettingsService:
    """Le e escreve a sobreposicao de configuracao de um workspace."""

    store: Store
    policy: PolicyEngine
    clock: Callable[[], datetime] = now
    organization: str = "*"
    client: str = "*"
    workspace_name: str = "*"
    environment: str = "staging"

    # ------------------------------------------------------------------
    def overlay(self, workspace_id: str) -> Overlay:
        """A sobreposicao gravada. Leitura livre -- ver nao e agir."""
        return self.store.settings(workspace_id)

    # ------------------------------------------------------------------
    def _allowed(self, actor: Principal, workspace_id: str) -> Outcome | None:
        if not actor.authenticated:
            return _no("UNAUTHENTICATED", "esta requisicao nao foi autenticada")
        if not actor.can(workspace_id, Ability.SETTINGS_WRITE):
            # `NOT_FOUND`, e nao `FORBIDDEN`: quem nao tem acesso a este
            # workspace nao descobre que ele existe pela mensagem de erro.
            return _no("NOT_FOUND", "recurso nao encontrado neste escopo")
        decision = self.policy.decide(PolicyContext(
            action=Action(kind=ACTION, resource=f"workspace:{workspace_id}",
                          environment=self.environment),
            organization=self.organization, client=self.client,
            workspace=self.workspace_name, agent=actor.label,
            autonomy=AutonomyLevel.L4))
        if decision.effect is not Effect.ALLOW:
            return _no("POLICY_DENIED",
                       f"policy {decision.effect}: {decision.reason}")
        return None

    # ------------------------------------------------------------------
    def put(self, actor: Principal, workspace_id: str, key: str,
            value: Any) -> Outcome:
        """Grava UMA sobreposicao. Toda barreira, e a validacao no fim."""
        recusa = self._allowed(actor, workspace_id)
        if recusa is not None:
            return recusa

        if key not in OVERRIDABLE:
            return _no("INVALID",
                       f"'{key}' nao pode ser configurado pela tela. "
                       f"Sobreponiveis: {', '.join(OVERRIDABLE)}")

        problema = validate(key, value)
        if problema:
            # Recusado AGORA, e nao no proximo tick. Uma configuracao invalida
            # gravada quebra o motor longe de quem a escreveu.
            return _no("INVALID", problema)

        anterior = self.store.settings(workspace_id).get(key)
        self.store.save_setting(workspace_id, key, value, actor.label,
                                self.clock())
        self.store.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id=workspace_id,
            kind="configuracao", actor=actor.label,
            summary=f"{key} definido pela interface",
            data={"chave": key, "por": actor.label,
                  "provedor_identidade": actor.provider,
                  "tinha_antes": anterior is not None}))
        return Outcome(True, key=key,
                       detail=("substituida" if anterior is not None
                               else "gravada"))

    # ------------------------------------------------------------------
    def clear(self, actor: Principal, workspace_id: str, key: str) -> Outcome:
        """Remove uma sobreposicao. O arquivo volta a valer naquele ponto."""
        recusa = self._allowed(actor, workspace_id)
        if recusa is not None:
            return recusa
        if key not in OVERRIDABLE:
            return _no("INVALID", f"'{key}' nao e sobreponivel")

        havia = self.store.clear_setting(workspace_id, key)
        if not havia:
            return _no("NOT_FOUND",
                       f"nao havia sobreposicao de '{key}'; o regente.yaml ja "
                       f"era quem mandava")
        self.store.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id=workspace_id,
            kind="configuracao", actor=actor.label,
            summary=f"{key} devolvido ao regente.yaml",
            data={"chave": key, "por": actor.label, "removida": True}))
        return Outcome(True, key=key, detail="removida; o arquivo volta a valer")
