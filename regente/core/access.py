# -*- coding: utf-8 -*-
"""Quem recebeu acesso, a que, de quem, e ate quando.

Ate aqui, acesso era um fato de configuracao: quem editava o arquivo concedia a
si mesmo autoridade de escrita, e nada guardava esse fato. Isto e o registro que
faltava -- e ele tem historia, nao um booleano.

    Identity     quem e voce?              provado por um provedor
    AccessGrant  voce recebeu acesso?      concedido por alguem, e revogavel
    Policy       esta acao e permitida?    independente das duas acima
    Transicao    este estado permite?      a maquina de estados

Nenhuma responde pela outra. Autenticado nao e autorizado; autorizado nao e
permitido; e permitido nao torna legal uma transicao ilegal.

O arquivo e puro: sem I/O, sem fornecedor, sem protocolo. Um `AccessGrant` nao
sabe o que e OIDC nem o que e um token -- ele sabe que **alguem** concedeu
**algo** a **alguem**, e quando.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .model import now


class Ability(str, Enum):
    """O que uma concessao pode conceder.

    Os valores sao os NOMES DAS ACOES que a policy avalia, e nao rotulos
    proprios. Uma segunda nomenclatura exigiria uma tabela de traducao, e uma
    tabela de traducao entre capacidade e acao e onde as duas divergem -- com a
    metade errada sendo sempre a que ninguem lembra de atualizar.

    Nao existe capacidade de LER aqui. Ver e concessao de composicao; agir e
    concessao persistida e atribuivel. Misturar as duas faria de todo observador
    um decisor.
    """
    DECIDE = "approval.decide"
    GRANT = "workspace.access.grant"
    REVOKE = "workspace.access.revoke"
    LIST = "workspace.access.list"
    #: Administrar CREDENCIAIS e uma autoridade separada de administrar
    #: PESSOAS. Quem entra na equipe nao ganha, por tabela, o direito de
    #: apontar para onde as credenciais do cliente vivem.
    #: USAR uma credencial. Separada de administra-la: o motor precisa desta e
    #: de nenhuma outra, e antes ele teria de tomar emprestada uma capacidade
    #: humana -- `approval.decide` -- que nao tem nada a ver com o que ele faz.
    CREDENTIAL_USE = "workspace.credential.use"
    CREDENTIAL_GRANT = "workspace.credential.grant"
    CREDENTIAL_REVOKE = "workspace.credential.revoke"
    CREDENTIAL_LIST = "workspace.credential.list"


#: O conjunto de quem administra acesso. Nomeado porque "administrador" e uma
#: palavra que cada pessoa entende de um jeito, e porque um papel precisa ser
#: uma ENTRADA para a policy, nunca um atalho que a substitua.
ADMIN = frozenset({Ability.GRANT, Ability.REVOKE, Ability.LIST})

#: Quem cuida das credenciais do workspace. Separado de `ADMIN` de proposito:
#: administrar pessoas e administrar segredos sao trabalhos diferentes, e juntar
#: os dois num papel so e como a autoridade cresce sem ninguem decidir isso.
KEEPER = frozenset({Ability.CREDENTIAL_GRANT, Ability.CREDENTIAL_REVOKE,
                    Ability.CREDENTIAL_LIST})

#: Quem so responde a fila humana.
OPERATOR = frozenset({Ability.DECIDE})

#: O motor. Uma capacidade so: usar as credenciais que lhe foram registradas.
#: Ele nao decide escalada, nao concede acesso e nao administra credencial --
#: e um papel estreito porque a autoridade de um processo automatico e o lugar
#: onde "so mais uma" cresce sem ninguem decidir.
SERVICE = frozenset({Ability.CREDENTIAL_USE})

#: Papeis nomeados, para a concessao nao virar uma lista de strings digitadas.
ROLES: dict[str, frozenset[Ability]] = {
    "operator": OPERATOR,
    "admin": ADMIN,
    "keeper": KEEPER,
    "service": SERVICE,
    "owner": ADMIN | OPERATOR | KEEPER | SERVICE,
}


def abilities_of(role: str) -> frozenset[Ability]:
    """As capacidades de um papel, ou vazio para um papel desconhecido.

    Vazio, e nao um erro: um papel que ninguem definiu concede nada, e essa e a
    resposta segura. Levantar excecao aqui transformaria um erro de digitacao na
    configuracao em queda do processo, e devolver tudo transformaria o mesmo
    erro numa escalada de privilegio.
    """
    return ROLES.get(role.strip().lower(), frozenset())


@dataclass(frozen=True, slots=True)
class PrincipalRef:
    """Uma identidade interna, formada por PROVEDOR mais SUJEITO.

    O provedor faz parte da chave de proposito. Sem ele, duas fontes de
    identidade diferentes que por acaso usem o mesmo sujeito -- `walberth` numa
    conta de sistema e `walberth` num diretorio corporativo -- virariam a mesma
    pessoa dentro do motor. Seriam duas pessoas diferentes com a mesma chave, e
    a concessao de uma valeria para a outra.

    O sujeito deve ser o identificador ESTAVEL que o provedor emite, nao o nome
    de exibicao nem o email: os dois mudam, e uma concessao amarrada a algo que
    muda e uma concessao que se transfere sozinha.
    """
    provider: str
    subject: str

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.subject.strip():
            # Um dos dois vazio produziria uma chave ambigua, e uma chave
            # ambigua e uma concessao que casa com quem nao devia.
            raise ValueError(
                "uma identidade interna precisa de provedor E sujeito; "
                f"recebi provider={self.provider!r} subject={self.subject!r}")

    @property
    def key(self) -> str:
        """A forma canonica. E o que a concessao guarda e a auditoria cita."""
        return f"{self.provider}:{self.subject}"

    @classmethod
    def parse(cls, key: str) -> "PrincipalRef":
        """Reconstroi a partir da forma canonica.

        Divide no PRIMEIRO `:` porque um sujeito pode conte-lo -- um SID nao,
        mas um identificador de outro provedor pode, e dividir no ultimo
        quebraria exatamente esses.
        """
        provider, _, subject = key.partition(":")
        return cls(provider=provider, subject=subject)


@dataclass(frozen=True, slots=True)
class AccessGrant:
    """Uma concessao, com quem a deu e quando -- e, se for o caso, quem a tirou.

    Nao e um booleano. O sistema precisa responder *quem recebeu*, *de quem*,
    *quando*, *com o que* e *se foi revogado* -- e nenhuma dessas perguntas tem
    resposta num campo `allowed = true`.
    """
    id: str
    client_id: str
    workspace_id: str
    principal: PrincipalRef
    abilities: frozenset[Ability]
    #: A chave canonica de quem concedeu. Nunca um nome de exibicao.
    granted_by: str
    granted_at: datetime = field(default_factory=now)
    revoked_by: str = ""
    revoked_at: datetime | None = None
    #: Por que foi concedida. Texto de quem concedeu, guardado como texto.
    note: str = ""

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def allows(self, ability: Ability) -> bool:
        """Revogada nao permite nada, por mais capacidades que carregue."""
        return self.active and ability in self.abilities
