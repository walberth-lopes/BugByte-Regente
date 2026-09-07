# -*- coding: utf-8 -*-
"""O motor tem identidade, e ela e concedida como a de qualquer pessoa.

Um tick roda de madrugada, sem ninguem olhando. Ate aqui a autoridade do motor
era **implicita**: ele agia porque tinha sido construido, e ninguem havia
registrado isso em lugar nenhum.

Quando as credenciais passaram a exigir identidade, concessao e policy, havia
duas saidas. A primeira era abrir uma excecao -- "o motor dispensa as barreiras"
-- e ela recriaria exatamente a segunda autoridade que este marco existe para
eliminar. A segunda e esta: **o motor tambem e um principal**, com concessao
gravada, revogavel por quem administra acesso.

A consequencia e desconfortavel e correta: se ninguem conceder acesso ao motor,
ele nao usa credencial nenhuma. Um sistema em que o processo automatico e o
unico que nao precisa de autorizacao e um sistema em que a autorizacao e
decorativa.

Nao ha segredo aqui. A identidade do motor nao e provada por credencial: ela e
o proprio processo, que so alcanca este codigo por ter sido montado por quem
controla a configuracao e o banco. O que a torna util nao e a prova -- e o
registro: uma concessao nomeada, com autor, data e revogacao.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...core.principal import Principal
from ...ports import Capability
from ...ports.identity import Identity, IdentityProvider


@dataclass(slots=True)
class EngineServiceIdentity(IdentityProvider):
    """O motor deste workspace, como principal de servico."""

    capability = Capability.IDENTITY
    name: str = "engine"
    #: NAO e de desenvolvimento: e o processo real agindo em nome do workspace.
    development_only: bool = False

    #: Qual workspace este motor serve. Faz parte do sujeito de proposito: dois
    #: workspaces sao dois motores, e uma concessao a um nao vale ao outro.
    workspace_id: str = ""
    #: Como a maquina se chama. Diagnostico -- nao entra na chave, porque mover
    #: o motor de maquina nao deve exigir reconceder acesso.
    host: str = ""

    def verify(self) -> None:
        if not self.workspace_id:
            raise ValueError(
                "um motor de servico precisa saber a que workspace serve; sem "
                "isso a concessao dele nao teria escopo")

    def describe(self) -> str:
        return (f"motor de servico do workspace {self.workspace_id}"
                + (f" em {self.host}" if self.host else ""))

    def authenticate(self, credential: str | None) -> Identity | None:
        """Nao ha credencial a apresentar, e isso e uma afirmacao.

        Quem chega aqui e o proprio processo. Aceitar um segredo daria a
        impressao de uma prova que nao existe -- e a prova nao e o ponto: o
        ponto e que a autoridade dele esta REGISTRADA e pode ser tirada.
        """
        if not self.workspace_id:
            return None
        return Identity(subject=self.workspace_id, display="motor",
                        method=self.name, provider=self.name,
                        issuer=self.host or "processo local")

    def principal(self, identity: Identity) -> Principal:
        """Identidade, e nenhuma autoridade -- igual a todos os outros.

        `abilities` vazio. O que o motor pode vem de uma concessao gravada,
        lida pelo `AccessService` a cada montagem, exatamente como para uma
        pessoa.
        """
        return Principal(subject=identity.subject, display=identity.display,
                         method=identity.method, provider=identity.provider,
                         issuer=identity.issuer,
                         authenticated_at=identity.authenticated_at,
                         workspaces=frozenset({self.workspace_id}))
