# -*- coding: utf-8 -*-
"""RepositoryProvider: codigo, branches, commits e pull requests."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from . import Capability, Port


@dataclass(frozen=True, slots=True)
class RepoInfo:
    nome: str
    branch_base: str = "main"
    url: str | None = None


@dataclass(frozen=True, slots=True)
class FileChange:
    caminho: str
    adicoes: int = 0
    remocoes: int = 0
    status: str = "modified"


@dataclass(frozen=True, slots=True)
class PullRequest:
    numero: int
    repo: str
    titulo: str
    url: str
    estado: str = "OPEN"
    #: SHA exato do head revisado. Sem isto e impossivel distinguir parecer
    #: vigente de parecer vencido -- e um parecer vencido descreve um codigo e
    #: aparece grudado em outro.
    head_sha: str = ""
    branch: str = ""
    base: str = "main"
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
    #: Commit que a review de fato cobre.
    commit_sha: str = ""
    corpo: str = ""
    id: str = ""


class RepositoryProvider(Port):
    capability = Capability.REPOSITORY

    @abstractmethod
    def list_repositories(self) -> list[RepoInfo]: ...

    @abstractmethod
    def read_file(self, repo: str, caminho: str, ref: str | None = None) -> str: ...

    @abstractmethod
    def get_pull_request(self, repo: str, numero: int) -> PullRequest: ...

    def list_pull_requests(self, filtro: dict[str, Any] | None = None) -> list[PullRequest]:
        return []

    def list_reviews(self, repo: str, numero: int) -> list[Review]:
        return []

    # ---- escrita ---------------------------------------------------------

    def create_branch(self, repo: str, nome: str, a_partir_de: str | None = None) -> None:
        raise NotImplementedError

    def write_file(self, repo: str, caminho: str, conteudo: str) -> None:
        raise NotImplementedError

    def create_commit(self, repo: str, mensagem: str, arquivos: list[str] | None = None) -> str:
        raise NotImplementedError

    def push(self, repo: str, branch: str) -> None:
        raise NotImplementedError

    def create_pull_request(self, repo: str, branch: str, titulo: str,
                            corpo: str, base: str | None = None) -> PullRequest:
        raise NotImplementedError

    def request_review(self, repo: str, numero: int, revisores: list[str]) -> None:
        raise NotImplementedError

    def submit_review(self, repo: str, numero: int, head_sha: str,
                      corpo: str, veredito: str) -> Review:
        """Publica parecer FIXADO no commit revisado.

        `head_sha` e obrigatorio na assinatura para que nenhum adapter possa
        publicar "no head que existir agora". O adapter deve reler o head e
        abortar se mudou: parecer que nasce vencido e pior que parecer ausente.
        """
        raise NotImplementedError

    def merge_pull_request(self, repo: str, numero: int, metodo: str = "squash") -> None:
        raise NotImplementedError
