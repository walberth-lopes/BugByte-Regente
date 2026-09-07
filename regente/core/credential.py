# -*- coding: utf-8 -*-
"""Uma credencial: a AUTORIDADE de usar um segredo, nunca o segredo.

Duas coisas com nomes parecidos e naturezas opostas:

    Credential      quem pode usar o que, onde, ate quando  -- vive no motor
    SecretMaterial  o valor em si                            -- nunca vive aqui

Este arquivo so conhece a primeira. Ele nao tem campo para guardar um token, nao
sabe resolver uma referencia, e nao importa nada que faca I/O. Se um segredo
couber num objeto deste modulo, o modulo esta errado.

A cadeia que o marco 14 estabeleceu para pessoas continua valendo, e ganha mais
um elo:

    Identity -> AccessGrant -> Policy -> Credential -> Provider -> Operacao

Cada elo responde uma pergunta diferente. Uma credencial valida nao autoriza uma
pessoa; uma pessoa autorizada nao ressuscita uma credencial expirada; e nenhuma
das duas dispensa a policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .model import now


class Use(str, Enum):
    """Para que uma credencial pode ser usada.

    Os valores sao os NOMES DAS ACOES que a policy avalia -- pelo mesmo motivo
    de `Ability` no marco 14: uma segunda nomenclatura exigiria uma tabela de
    traducao, e uma tabela de traducao entre capacidade e acao e onde as duas
    divergem em silencio.

    Uma credencial de leitura nao vale para push so porque o provider oferece
    push. `provider capability` e o que a ferramenta sabe fazer; `credential
    capability` e o que ESTA credencial foi autorizada a fazer; `policy
    authority` e o que a organizacao permite. Sao tres perguntas.
    """
    TASK_READ = "task.read"
    TASK_WRITE = "task.write"
    REPO_READ = "repo.read"
    REPO_PUSH = "repo.push"
    REPO_PR = "repo.pr"
    CI_READ = "ci.read"
    AGENT_RUN = "agent.run"


def uses_from(names) -> frozenset[Use]:
    """Converte nomes em capacidades, ignorando o que nao existe.

    Ignorar, e nao levantar: um nome desconhecido numa configuracao vira
    capacidade nenhuma. Levantar transformaria erro de digitacao em queda do
    processo; aceitar transformaria o mesmo erro em escalada de privilegio.
    """
    found = set()
    for name in names or ():
        try:
            found.add(Use(str(name).strip()))
        except ValueError:
            continue
    return frozenset(found)


class Status(str, Enum):
    """O estado de uma credencial AGORA. Sempre derivado, nunca guardado.

    Guardar o status numa coluna criaria duas verdades: a coluna e o relogio. A
    que fica errada e sempre a coluna, porque expirar nao e um evento que alguem
    escreve -- e o tempo passando.
    """
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


@dataclass(frozen=True, slots=True)
class SecretRef:
    """Onde o material vive. NUNCA o material.

    `esquema:resto`. O esquema diz qual fonte sabe resolver; o resto e endereco.
    Nao existe esquema `literal`, e a ausencia e deliberada: se existisse, o
    primeiro segredo de producao apareceria num YAML versionado dentro de uma
    semana.
    """
    scheme: str
    locator: str

    def __post_init__(self) -> None:
        if not self.scheme.strip() or not self.locator.strip():
            raise ValueError(
                f"referencia de segredo precisa de esquema e endereco; "
                f"recebi {self.scheme!r}:{self.locator!r}")
        if self.scheme.strip().lower() == "literal":
            # Recusado no construtor, e nao no resolvedor: uma referencia
            # literal nao deve nem existir como objeto, porque o valor estaria
            # dentro dela e viajaria para o banco e para os eventos.
            raise ValueError(
                "referencia 'literal' nao existe: um segredo escrito na "
                "configuracao vai parar no controle de versao")

    @property
    def text(self) -> str:
        return f"{self.scheme}:{self.locator}"

    @classmethod
    def parse(cls, raw: str) -> "SecretRef":
        scheme, _, locator = str(raw).partition(":")
        return cls(scheme=scheme.strip(), locator=locator.strip())

    def __str__(self) -> str:
        """Seguro para log: e um endereco, nao um valor."""
        return self.text


@dataclass(frozen=True, slots=True)
class Credential:
    """A autoridade de usar um segredo neste workspace, com historia.

    Nao e um par nome/valor. As perguntas que ela existe para responder --
    *quem concedeu*, *para onde*, *para que provider*, *para quais capacidades*,
    *ate quando*, *foi revogada* -- nenhuma tem resposta num `github = true`.
    """
    id: str
    client_id: str
    workspace_id: str
    #: Nome legivel, escolhido por quem concedeu. NUNCA a identidade: dois
    #: workspaces podem ter uma credencial `principal` e sao duas credenciais.
    name: str
    #: Qual provider pode usa-la, no vocabulario da composicao ("repository",
    #: "tasks", "runner"). Uma credencial de tasks nao serve ao repositorio.
    provider: str
    #: Que forma de credencial e -- `token`, `sessao`, `chave`. Diagnostico.
    kind: str
    secret_ref: SecretRef
    capabilities: frozenset[Use]
    granted_by: str
    granted_at: datetime = field(default_factory=now)
    expires_at: datetime | None = None
    revoked_by: str = ""
    revoked_at: datetime | None = None
    note: str = ""

    @property
    def active(self) -> bool:
        """Nao foi revogada. Nao diz nada sobre validade.

        Separado de `status` de proposito: revogacao e uma decisao de alguem e
        nao depende de relogio nenhum, entao pode ser respondida sem `at`.
        """
        return self.revoked_at is None

    def status(self, at: datetime) -> Status:
        """Revogada vence expirada.

        A ordem importa para a auditoria: uma credencial que alguem tirou de
        circulacao e depois passou da validade continua sendo, para quem
        investiga, uma credencial **revogada** -- o fato relevante e a decisao,
        nao o relogio que passou por cima dela.
        """
        if self.revoked_at is not None:
            return Status.REVOKED
        if self.expires_at is not None and at >= self.expires_at:
            # `>=`: no instante exato do vencimento ela JA venceu. O limite
            # pertence ao lado de fora, e essa escolha e explicita porque
            # "exatamente no limite" e onde as duas leituras discordam.
            return Status.EXPIRED
        return Status.ACTIVE

    def allows(self, use: Use, at: datetime) -> bool:
        """Ativa E com a capacidade. Nunca uma sem a outra."""
        return self.status(at) is Status.ACTIVE and use in self.capabilities
