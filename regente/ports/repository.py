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


class CapacidadeRepo(str, Enum):
    """O que um adapter consegue fazer com um repositorio.

    Declarado pelo adapter, consultado pelo motor antes de propor trabalho.
    Propor uma acao que o adapter nao implementa e desperdicio de um ciclo
    inteiro -- e, pior, uma escalonada ao humano por um motivo que o motor
    poderia ter previsto sozinho.
    """
    LER_METADADOS = "ler_metadados"
    LER_ARQUIVOS = "ler_arquivos"
    LER_HISTORICO = "ler_historico"
    LER_BRANCHES = "ler_branches"
    LER_PULL_REQUESTS = "ler_pull_requests"
    CLONAR = "clonar"
    # As de escrita existem no vocabulario para que a policy e a UI possam
    # raciocinar sobre elas antes de qualquer implementacao existir.
    CRIAR_BRANCH = "criar_branch"
    COMMITAR = "commitar"
    EMPURRAR = "empurrar"
    ABRIR_PR = "abrir_pr"
    REVISAR = "revisar"
    MERGEAR = "mergear"


LEITURA: frozenset[CapacidadeRepo] = frozenset({
    CapacidadeRepo.LER_METADADOS, CapacidadeRepo.LER_ARQUIVOS,
    CapacidadeRepo.LER_HISTORICO, CapacidadeRepo.LER_BRANCHES,
    CapacidadeRepo.LER_PULL_REQUESTS, CapacidadeRepo.CLONAR,
})

ESCRITA: frozenset[CapacidadeRepo] = frozenset(CapacidadeRepo) - LEITURA


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

    def escopado_em(self, workspace_id: str) -> str:
        """A identidade que o motor usa. Unica no universo de um deployment."""
        return f"{workspace_id}/{self.provider}/{self.key}"

    def recurso(self, workspace_id: str) -> str:
        """Chave de exclusao mutua para o scheduler e para o lease."""
        return f"repo:{self.escopado_em(workspace_id)}"


@dataclass(frozen=True, slots=True)
class RepoInfo:
    """Um repositorio como o provedor o descreve."""
    ref: RepoRef
    nome: str
    #: Branch de integracao REAL, lida do provedor.
    #:
    #: Nunca presumir 'main'. Derivar branch de trabalho da base errada produz um
    #: PR cheio de conflito que ninguem pediu, e o erro so aparece depois do
    #: push -- quando ja custou o trabalho inteiro.
    branch_base: str = ""
    #: De onde clonar. Pode ser URL remota ou caminho local.
    origem_de_clone: str | None = None
    #: Onde um humano ve este repositorio.
    url: str | None = None
    arquivado: bool = False
    privado: bool | None = None
    capacidades: frozenset[CapacidadeRepo] = field(default_factory=frozenset)
    #: True quando o registro veio de uma LISTAGEM, com campos enxutos --
    #: mesma distincao que vale para tasks: "nao veio" nao e "esta vazio".
    parcial: bool = False
    dados: dict[str, Any] = field(default_factory=dict)

    def pode(self, c: CapacidadeRepo) -> bool:
        return c in self.capacidades

    @property
    def anomalias(self) -> tuple[str, ...]:
        achados = []
        if not self.parcial and not self.branch_base:
            achados.append("sem branch base -- derivar trabalho daqui e chute")
        if not self.nome.strip():
            achados.append("sem nome legivel")
        if self.arquivado:
            achados.append("arquivado: nao aceita trabalho novo")
        return tuple(achados)

    @property
    def utilizavel(self) -> bool:
        """Da para trabalhar aqui? Arquivado e sem base nao dao."""
        return bool(self.branch_base) and not self.arquivado


@dataclass(frozen=True, slots=True)
class Branch:
    nome: str
    sha: str = ""
    #: True quando e a branch de integracao do repositorio.
    e_base: bool = False
    atualizada_em: str = ""


@dataclass(frozen=True, slots=True)
class FileChange:
    caminho: str
    adicoes: int = 0
    remocoes: int = 0
    status: str = "modified"


@dataclass(frozen=True, slots=True)
class PullRequest:
    numero: int
    repo: RepoRef
    titulo: str
    url: str
    estado: str = "OPEN"
    #: SHA exato do head. Sem ele e impossivel distinguir parecer vigente de
    #: parecer vencido -- e um parecer vencido descreve um codigo e aparece
    #: grudado noutro.
    head_sha: str = ""
    branch: str = ""
    base: str = ""
    rascunho: bool = False
    autor: str = ""
    adicoes: int = 0
    remocoes: int = 0
    arquivos: tuple[FileChange, ...] = ()
    dados: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Review:
    autor: str
    veredito: str          # APPROVED | CHANGES_REQUESTED | COMMENTED
    commit_sha: str = ""
    corpo: str = ""
    id: str = ""


class RepositoryProvider(Port):
    capability = Capability.REPOSITORY

    #: O que este adapter, como esta montado, consegue fazer.
    capacidades: frozenset[CapacidadeRepo] = LEITURA

    # ---- descoberta e leitura -------------------------------------------

    @abstractmethod
    def list_repositories(self, filtro: dict[str, Any] | None = None) -> list[RepoInfo]:
        """Repositorios visiveis. Erro sobe como AdapterErro, nunca lista vazia."""

    @abstractmethod
    def get_repository(self, key: str) -> RepoInfo:
        """Detalhe completo de um repositorio, pela chave do provedor."""

    def list_branches(self, key: str, filtro: dict[str, Any] | None = None) -> list[Branch]:
        return []

    def read_file(self, key: str, caminho: str, ref: str | None = None) -> str:
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

    def create_branch(self, key: str, nome: str, a_partir_de: str) -> Branch:
        """`a_partir_de` e obrigatorio: derivar da base implicita e o caminho
        curto para um PR nascido de codigo velho."""
        raise NotImplementedError

    def create_commit(self, key: str, branch: str, mensagem: str,
                      arquivos: dict[str, str]) -> str:
        raise NotImplementedError

    def push(self, key: str, branch: str, esperado_sha: str | None = None) -> None:
        """`esperado_sha` permite recusar o push se a branch andou sob os pes."""
        raise NotImplementedError

    def create_pull_request(self, key: str, branch: str, base: str,
                            titulo: str, corpo: str) -> PullRequest:
        raise NotImplementedError

    def submit_review(self, key: str, numero: int, head_sha: str,
                      corpo: str, veredito: str) -> Review:
        """`head_sha` e obrigatorio na assinatura para que nenhum adapter possa
        publicar 'no head que existir agora'. O adapter deve reler o head e
        abortar se mudou: parecer que nasce vencido e pior que parecer ausente."""
        raise NotImplementedError

    def merge_pull_request(self, key: str, numero: int, metodo: str = "squash",
                           esperado_sha: str | None = None) -> None:
        raise NotImplementedError
