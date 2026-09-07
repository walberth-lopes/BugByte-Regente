# -*- coding: utf-8 -*-
"""Quem esta no terminal.

O terminal ja e uma fronteira de autenticacao: quem consegue rodar `regente`
nesta maquina ja provou ao sistema operacional que e esta conta. Este provedor
so traduz esse fato para o vocabulario do motor -- nao autentica nada por conta
propria, e nao finge autenticar.

A mudanca que ele traz e outra, e e a razao de existir: **o terminal deixou de
declarar quem e**. Ate aqui, `regente decide --por fulano` gravava na auditoria
o texto que a pessoa digitasse, e um campo de texto nao e identidade. Agora o
sujeito vem da conta que esta rodando o processo.
"""

from __future__ import annotations

import getpass
from dataclasses import dataclass, field

from ...core.principal import Principal
from ...ports import Capability
from ...ports.identity import Identity, IdentityProvider


def current_user() -> str:
    """A conta do sistema operacional, ou a admissao de que nao da para saber.

    `getpass` consulta o ambiente antes do sistema, e variavel de ambiente e
    editavel por quem roda o processo. Isso e aceitavel aqui e nao seria na
    rede: quem edita o proprio ambiente ja e a pessoa cuja conta esta em jogo.
    Fora do terminal, este provedor nao serve -- e por isso ele nao e oferecido
    a nenhuma superficie remota.
    """
    try:
        return getpass.getuser() or "desconhecido"
    except Exception:      # noqa: BLE001 - sem conta legivel; dizer, nao adivinhar
        return "desconhecido"


@dataclass(slots=True)
class LocalTerminalIdentity(IdentityProvider):
    """A conta que roda o processo, com o alcance concedido pela composicao."""

    capability = Capability.IDENTITY
    name: str = "terminal"
    development_only: bool = False

    reads: frozenset[str] | None = None
    decides: frozenset[str] = field(default_factory=frozenset)

    def verify(self) -> None:
        return None

    def describe(self) -> str:
        return (f"terminal como '{current_user()}'; decide em "
                f"{len(self.decides)} workspace(s)")

    def authenticate(self, credential: str | None) -> Identity | None:
        """A credencial e ignorada de proposito: quem prova e o sistema.

        Aceitar um segredo aqui daria a impressao de que existe um segundo
        fator. Nao existe. O que existe e o fato de o processo estar rodando
        sob esta conta.
        """
        user = current_user()
        return Identity(subject=user, display=user, method=self.name)

    def principal(self, identity: Identity) -> Principal:
        return Principal(subject=identity.subject, display=identity.display,
                         method=identity.method, workspaces=self.reads,
                         decides=self.decides)
