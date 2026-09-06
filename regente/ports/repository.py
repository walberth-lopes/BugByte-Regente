# -*- coding: utf-8 -*-
"""RepositoryProvider: onde o codigo vive.

Duas decisoes sustentam esta porta:

**1. Nome nao e identidade.** Um repositorio se identifica por
`(provedor, chave)`, e o motor ainda escopa isso pelo workspace antes de usar
como chave de trava ou de estado. Nome nu falha de tres formas ja observadas no
ambiente real: o diretorio local pode nao bater com o repositorio remoto; dois
clientes podem ter repositorios homonimos; e o mesmo repositorio pode ser visto
por dois provedores diferentes ao mesmo tempo.

**2. Poder e permissao sao coisas separadas.** `capacidades` diz o que o adapter
CONSEGUE fazer; o Policy Engine diz o que ele PODE. Um adapter montado so para
leitura declara poucas capacidades e a policy nem chega a ser consultada; um
adapter completo declara muitas e a policy continua sendo quem barra. Misturar as
duas nocoes produz o pior dos casos: um `if pode_escrever` espalhado pelo codigo,
que ninguem consegue auditar num lugar so.

Escrita existe aqui apenas como CONTRATO. As assinaturas estao declaradas para
que o desenho futuro seja visivel e criticavel agora; nenhuma implementacao deste
marco as executa.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import Capability, Port


class RepoCapability(str, Enum):
    """O que um adapter consegue fazer com um repositorio.

    Declarado pelo adapter, consultado pelo motor antes de propor trabalho.
    Propor uma acao que o adapter nao implementa e desperdicio de um ciclo
    inteiro -- e, pior, uma escalonada ao humano por um motivo que o motor
    poderia ter previsto sozinho.
    """
    LER_METADADOS = "read_metadata"
    LER_ARQUIVOS = "read_files"
    LER_HISTORICO = "read_history"
    LER_BRANCHES = "read_branches"
    LER_PULL_REQUESTS = "read_pull_requests"
    CLONAR = "clone"
    # As de escrita existem no vocabulario para que a policy e a UI possam
    # raciocinar sobre elas antes de qualquer implementacao existir.
    CRIAR_BRANCH = "create_branch"
    COMMITAR = "commit"
    EMPURRAR = "push"
    ABRIR_PR = "open_pr"
    REVISAR = "review"
    MERGEAR = "merge"


READ_CAPS: frozenset[RepoCapability] = frozenset({
    RepoCapability.LER_METADADOS, RepoCapability.LER_ARQUIVOS,
    RepoCapability.LER_HISTORICO, RepoCapability.LER_BRANCHES,
    RepoCapability.LER_PULL_REQUESTS, RepoCapability.CLONAR,
})

WRITE_CAPS: frozenset[RepoCapability] = frozenset(RepoCapability) - READ_CAPS


@dataclass(frozen=True, slots=True)
class RepoRef:
    """Identidade de um repositorio NO PROVEDOR.

    Nao carrega workspace de proposito: o adapter nao deve precisar conhecer a
    tenancy para responder o que sabe. Quem compoe a identidade completa e o
    motor, com `escopado_em()` -- e e essa forma composta, nunca a chave nua,
    que vira trava, recurso ou linha de estado.
    """
    provider: str
    key: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.key}"

    def scoped_to(self, workspace_id: str) -> str:
        """A identidade que o motor usa. Unica no universo de um deployment."""
        return f"{workspace_id}/{self.provider}/{self.key}"

    def resource(self, workspace_id: str) -> str:
        """Chave de exclusao mutua para o scheduler e para o lease."""
        return f"repo:{self.scoped_to(workspace_id)}"


@dataclass(frozen=True, slots=True)
class RepoInfo:
    """Um repositorio como o provedor o descreve."""
    ref: RepoRef
    name: str
    #: Branch de integracao REAL, lida do provedor.
    #:
    #: Nunca presumir 'main'. Derivar branch de trabalho da base errada produz um
    #: PR cheio de conflito que ninguem pediu, e o error so aparece depois do
    #: push -- quando ja custou o trabalho inteiro.
    base_branch: str = ""
    #: De onde clonar. Pode ser URL remota ou caminho local.
    clone_origin: str | None = None
    #: Onde um humano ve este repositorio.
    url: str | None = None
    archived: bool = False
    private: bool | None = None
    capabilities: frozenset[RepoCapability] = field(default_factory=frozenset)
    #: True quando o registro veio de uma LISTAGEM, com campos enxutos --
    #: mesma distincao que vale para tasks: "nao veio" nao e "esta vazio".
    partial: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def can(self, c: RepoCapability) -> bool:
        return c in self.capabilities

    @property
    def anomalies(self) -> tuple[str, ...]:
        findings = []
        if not self.partial and not self.base_branch:
            findings.append("sem branch base -- derivar trabalho daqui e chute")
        if not self.name.strip():
            findings.append("sem nome legivel")
        if self.archived:
            findings.append("arquivado: nao aceita trabalho novo")
        return tuple(findings)

    @property
    def usable(self) -> bool:
        """Da para trabalhar aqui? Arquivado e sem base nao dao."""
        return bool(self.base_branch) and not self.archived


@dataclass(frozen=True, slots=True)
class Branch:
    name: str
    sha: str = ""
    #: True quando e a branch de integracao do repositorio.
    e_base: bool = False
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    additions: int = 0
    deletions: int = 0
    status: str = "modified"


@dataclass(frozen=True, slots=True)
class PullRequest:
    numero: int
    repo: RepoRef
    title: str
    url: str
    state: str = "OPEN"
    #: SHA exato do head. Sem ele e impossivel distinguir parecer vigente de
    #: parecer vencido -- e um parecer vencido descreve um codigo e aparece
    #: grudado noutro.
    head_sha: str = ""
    branch: str = ""
    base: str = ""
    rascunho: bool = False
    autor: str = ""
    additions: int = 0
    deletions: int = 0
    files: tuple[FileChange, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Review:
    autor: str
    veredito: str          # APPROVED | CHANGES_REQUESTED | COMMENTED
    commit_sha: str = ""
    body: str = ""
    id: str = ""


class RepositoryProvider(Port):
    capability = Capability.REPOSITORY

    #: O que este adapter, como esta montado, consegue fazer.
    capabilities: frozenset[RepoCapability] = READ_CAPS

    # ---- descoberta e leitura -------------------------------------------

    @abstractmethod
    def list_repositories(self, filtro: dict[str, Any] | None = None) -> list[RepoInfo]:
        """Repositorios visiveis. Erro sobe como AdapterErro, nunca lista vazia."""

    @abstractmethod
    def get_repository(self, key: str) -> RepoInfo:
        """Detalhe completo de um repositorio, pela chave do provedor."""

    def list_branches(self, key: str, filtro: dict[str, Any] | None = None) -> list[Branch]:
        return []

    def read_file(self, key: str, path: str, ref: str | None = None) -> str:
        raise NotImplementedError

    def list_pull_requests(self, filtro: dict[str, Any] | None = None) -> list[PullRequest]:
        return []

    def get_pull_request(self, key: str, numero: int) -> PullRequest:
        raise NotImplementedError

    def list_reviews(self, key: str, numero: int) -> list[Review]:
        return []

    # ---- escrita: contrato declarado, nada implementado -----------------
    #
    # As assinaturas existem para que o desenho futuro seja visivel e criticavel
    # agora. Cada uma tem a forma que impede um defeito ja conhecido -- e por
    # isso vale escreve-las antes, e nao depois de o defeito acontecer.

    def create_branch(self, key: str, name: str, a_partir_de: str) -> Branch:
        """`a_partir_de` e obrigatorio: derivar da base implicita e o caminho
        curto para um PR nascido de codigo velho."""
        raise NotImplementedError

    def create_commit(self, key: str, branch: str, message: str,
                      files: dict[str, str]) -> str:
        raise NotImplementedError

    def push(self, key: str, branch: str, esperado_sha: str | None = None) -> None:
        """`esperado_sha` permite recusar o push se a branch andou sob os pes."""
        raise NotImplementedError

    def create_pull_request(self, key: str, branch: str, base: str,
                            title: str, body: str) -> PullRequest:
        raise NotImplementedError

    def submit_review(self, key: str, numero: int, head_sha: str,
                      body: str, veredito: str) -> Review:
        """`head_sha` e obrigatorio na assinatura para que nenhum adapter possa
        publicar 'no head que existir agora'. O adapter deve reler o head e
        abortar se mudou: parecer que nasce vencido e pior que parecer ausente."""
        raise NotImplementedError

    def merge_pull_request(self, key: str, numero: int, metodo: str = "squash",
                           esperado_sha: str | None = None) -> None:
        raise NotImplementedError
