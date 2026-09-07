# -*- coding: utf-8 -*-
"""Quem esta agindo, com que procedencia, e ate onde.

Cinco perguntas diferentes, e a confusao entre elas e como uma escrita escapa:

    Autenticacao  quem e voce?              um `Identity`, provado por um provedor
    Identidade    qual chave estavel?       `provider` + `subject`, nunca o nome
    Acesso        voce recebeu concessao?   um `AccessGrant`, com autor e data
    Policy        esta acao e permitida?    o arquivo de regras, independente
    Transicao     este estado permite?      a maquina de estados

Nenhuma responde pela outra. Um principal autenticado nao esta autorizado; um
autorizado nao venceu a policy; e uma policy que permite nao reabre uma
aprovacao ja decidida.

Dois campos carregam o peso.

`method` separa **autenticado** de **afirmado**: vazio significa que ninguem
provou nada. E a diferenca entre "o servidor verificou um segredo que ele
proprio emitiu" e "o navegador digitou um nome".

`abilities` separa **autorizado** de **autenticado**. Ele vem de concessoes
PERSISTIDAS, com autor e data -- nao de um campo de configuracao. Ate o marco
anterior, quem editava o arquivo concedia a si mesmo autoridade de escrita e
nada guardava esse fato.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from .access import Ability, PrincipalRef


@dataclass(frozen=True, slots=True)
class Principal:
    """Uma identidade autenticada, com a procedencia e o alcance que tem.

    Construir isto NAO autentica ninguem e NAO concede nada. Quem autentica e um
    `IdentityProvider`; quem concede e um `AccessGrant` guardado. Esta classe so
    carrega o resultado das duas coisas, e por isso ambos os campos que importam
    comecam vazios.
    """
    subject: str = "anonimo"
    display: str = ""
    #: Como esta identidade foi provada. Vazio = nao foi.
    method: str = ""
    #: QUEM provou. Faz parte da identidade interna: sem ele, duas fontes que
    #: usem o mesmo sujeito viram a mesma pessoa dentro do motor.
    provider: str = ""
    #: Quem emitiu a identidade, como o provedor o nomeia -- a maquina, o
    #: dominio, o issuer. Diagnostico e auditoria; nao entra na chave.
    issuer: str = ""
    #: Quando esta identidade foi provada. `None` para anonimo.
    authenticated_at: datetime | None = None
    #: Workspaces que pode LER. `None` = todos os que o store guarda.
    #: Ver e concessao de composicao; agir nao.
    workspaces: frozenset[str] | None = None
    #: O que pode FAZER, por workspace. Vem de concessoes persistidas.
    #: Vazio por default: ninguem age sem concessao explicita e atribuivel.
    abilities: Mapping[str, frozenset[Ability]] = field(
        default_factory=lambda: MappingProxyType({}))

    @property
    def authenticated(self) -> bool:
        return bool(self.method)

    @property
    def ref(self) -> PrincipalRef:
        """A identidade interna. Levanta para um principal nao autenticado.

        De proposito: um anonimo nao tem chave, e inventar uma faria a
        auditoria registrar concessoes para ninguem.
        """
        return PrincipalRef(provider=self.provider or self.method,
                            subject=self.subject)

    @property
    def label(self) -> str:
        """Como esta identidade aparece na auditoria.

        Carrega o provedor junto do sujeito porque "quem" e "como provamos que
        era essa pessoa" sao coisas que um leitor de auditoria precisa ver na
        mesma linha. `dev-token:walberth` e uma frase honesta; `walberth`
        sozinho esconde que o token era de desenvolvimento.
        """
        if not self.authenticated:
            return self.subject
        return f"{self.provider or self.method}:{self.subject}"

    # ---- leitura -----------------------------------------------------
    def may_read(self, workspace_id: str) -> bool:
        """Ver. Concessao de composicao, ou concessao persistida.

        Quem recebeu uma capacidade num workspace precisa enxergar o workspace,
        senao a concessao seria inutil -- mas o contrario nao vale, e e isso que
        as duas linhas separadas garantem.
        """
        if self.workspaces is None:
            return True
        return workspace_id in self.workspaces or bool(
            self.abilities.get(workspace_id))

    # ---- acao --------------------------------------------------------
    def can(self, workspace_id: str, ability: Ability) -> bool:
        """A unica pergunta de autoridade, e ela e sempre por workspace.

        Autenticado nao basta. Ter capacidade noutro workspace nao basta. Um
        `if principal.is_admin` que dispensasse esta pergunta seria uma segunda
        autoridade, e a segunda autoridade e sempre a que esquece alguma coisa.
        """
        return (self.authenticated
                and ability in self.abilities.get(workspace_id, frozenset()))

    def may_decide(self, workspace_id: str) -> bool:
        return self.can(workspace_id, Ability.DECIDE)

    @property
    def decides(self) -> frozenset[str]:
        """Onde pode decidir. Derivado das capacidades, nunca o contrario."""
        return frozenset(w for w, a in self.abilities.items()
                         if Ability.DECIDE in a)

    def with_abilities(self, abilities: Mapping[str, frozenset[Ability]]
                       ) -> "Principal":
        """O mesmo principal, com o que as concessoes disserem.

        Existe para que a composicao monte a identidade primeiro e as
        capacidades depois -- que e a ordem real dos fatos, e a que impede um
        provedor de identidade de conceder autoridade de passagem.
        """
        return Principal(
            subject=self.subject, display=self.display, method=self.method,
            provider=self.provider, issuer=self.issuer,
            authenticated_at=self.authenticated_at,
            workspaces=self.workspaces,
            abilities=MappingProxyType(dict(abilities)))


#: Ninguem. O default de toda requisicao que ainda nao foi autenticada.
ANONYMOUS = Principal(subject="anonimo", display="nao autenticado",
                      workspaces=frozenset())
