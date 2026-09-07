# -*- coding: utf-8 -*-
"""De onde vem uma identidade humana.

A porta existe para que a resposta a "quem e voce?" possa ser trocada sem tocar
em nada acima dela. Hoje ha uma implementacao de desenvolvimento; amanha pode
haver OIDC, SSO corporativo ou um proxy que ja autentica. Nenhuma dessas trocas
pode exigir mudanca na API, no motor ou na tela.

Duas regras que valem para qualquer implementacao:

**O cliente nunca declara quem e.** Ele apresenta uma credencial, e o provedor
decide o que ela prova. `{"principal": "walberth"}` no corpo de um POST nao e
identidade -- e um campo de texto. A diferenca entre apresentar um segredo que o
servidor emitiu e digitar um nome e a diferenca entre autenticar e acreditar.

**Falhar e responder, nao levantar.** `authenticate` devolve `None` quando a
credencial nao prova nada. Uma excecao aqui viraria 500, e um 500 numa fronteira
de autenticacao e indistinguivel de um bug -- exatamente onde a distincao entre
"nao autenticado" e "quebrou" precisa ser nitida.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from ..core.principal import Principal
from . import Capability, Port


@dataclass(frozen=True, slots=True)
class Identity:
    """Quem o provedor diz que e, e como ele sabe.

    `method` nao e decorativo: e o que a auditoria registra ao lado do sujeito.
    Uma decisao tomada com token de desenvolvimento e uma decisao tomada por SSO
    corporativo nao podem aparecer iguais no historico.
    """
    subject: str
    display: str = ""
    method: str = ""


class IdentityProvider(Port):
    """Transforma uma credencial apresentada numa identidade, ou em nada."""

    capability = Capability.IDENTITY

    #: Como este provedor se chama num diagnostico. Nao e o nome do fornecedor:
    #: e o nome do MECANISMO, porque e isso que muda o que a auditoria significa.
    name: str = "desconhecido"

    #: True quando este mecanismo NAO serve para valer.
    #:
    #: Existe para que `doctor` e `health` possam dize-lo em voz alta. Um
    #: mecanismo de desenvolvimento que ninguem consegue distinguir de um real e
    #: pior que nenhum: cria a sensacao de que ha autenticacao.
    development_only: bool = False

    @abstractmethod
    def authenticate(self, credential: str | None) -> Identity | None:
        """Devolve a identidade provada, ou `None`. Nunca levanta por recusa."""

    @abstractmethod
    def principal(self, identity: Identity) -> Principal:
        """O alcance concedido a esta identidade.

        Separado de `authenticate` porque autenticar e autorizar sao perguntas
        diferentes, e quem responde uma raramente deveria responder a outra: um
        provedor de identidade corporativo sabe quem voce e e nao faz ideia de
        quais workspaces deste motor sao seus.
        """
