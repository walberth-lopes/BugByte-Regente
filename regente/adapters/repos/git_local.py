# -*- coding: utf-8 -*-
"""RepositoryProvider sobre clones git no disco. SOMENTE LEITURA.

Provedor de verdade, nao ensaio: quem trabalha com clones locais tem aqui um
motor funcional sem depender de rede nem de credencial. E serve de segunda
implementacao contra a qual o contrato e provado -- deliberadamente do tipo mais
diferente possivel do outro adapter: processo local contra API remota.

**Somente leitura por construcao.** Toda invocacao passa por `_git`, que recusa
qualquer subcomando fora de uma lista fechada de leitura. Nao e um `if dry_run`:
`git commit` nem chega a ser montado.

Dois fatos do disco real que o desenho respeita:

1. **O nome do diretorio nao e o nome do repositorio.** Medido: o diretorio
   `scamchecker-legado` aponta para `silverguard-br/scamchecker`. A identidade
   vem do remoto quando existe; do caminho so quando nao ha remoto.
2. **A branch corrente quase nunca e a base.** Dos 12 clones examinados, 11
   estavam numa branch de trabalho. Ler `HEAD` como base derivaria trabalho novo
   de codigo pela metade de outra pessoa.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...ports import AdapterErro, SomenteLeitura
from ...ports.repository import (LEITURA, Branch, CapacidadeRepo, RepoInfo, RepoRef,
                                 RepositoryProvider)
from ..tasks.transporte import Chamada, Observador
from .leitura import git_e_leitura


#: Ordem de tentativa para descobrir a branch de integracao. A primeira que
#: existir vence -- e nenhuma delas e `HEAD`, que aponta para o trabalho atual.
CANDIDATAS_BASE = ("origin/HEAD", "origin/main", "origin/master",
                   "origin/develop", "main", "master")


@dataclass(slots=True)
class GitLocal(RepositoryProvider):
    """Le repositorios git de um diretorio de clones."""

    raiz: Path
    nome: str = "git-local"
    #: Reflete o que o adapter consegue: le tudo do disco, menos pull requests,
    #: que nao existem em git puro -- sao conceito do servico de hospedagem.
    capacidades: frozenset[CapacidadeRepo] = field(
        default_factory=lambda: LEITURA - {CapacidadeRepo.LER_PULL_REQUESTS})
    observador: Observador | None = None
    timeout: int = 60

    def __post_init__(self) -> None:
        self.raiz = Path(self.raiz)

    def descreve(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.nome,
                "modo": "somente-leitura", "raiz": str(self.raiz)}

    def verifica(self) -> None:
        if not self.raiz.is_dir():
            raise AdapterErro(f"raiz de clones nao existe: {self.raiz}")

    # ---- execucao de git -------------------------------------------------

    def _git(self, cwd: Path, *args: str) -> str:
        # A checagem e por INVOCACAO INTEIRA, nao por verbo. `git remote get-url`
        # le; `git remote set-url` grava, e os dois comecam com `remote`.
        ok, motivo = git_e_leitura(args)
        if not ok:
            raise SomenteLeitura(
                f"'git {' '.join(args)}' recusado: {motivo}. "
                f"Nenhuma mutacao e executavel neste marco.")
        import time
        inicio = time.monotonic()
        try:
            p = subprocess.run(
                ["git", *args], cwd=str(cwd), capture_output=True,
                # `encoding` obrigatorio no Windows: sem isso, nome de branch com
                # acento vira mojibake e a comparacao de chave falha em silencio.
                encoding="utf-8", errors="replace", timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            self._avisa(args, inicio, False, f"timeout apos {self.timeout}s")
            raise AdapterErro(f"git {args[0]} estourou {self.timeout}s em {cwd}") from e
        except FileNotFoundError as e:
            self._avisa(args, inicio, False, "git nao encontrado")
            raise AdapterErro("git nao esta no PATH") from e
        if p.returncode != 0:
            erro = (p.stderr or "").strip()[:300]
            self._avisa(args, inicio, False, erro)
            raise AdapterErro(f"git {args[0]} rc={p.returncode} em {cwd.name}: {erro}")
        self._avisa(args, inicio, True, "")
        return p.stdout

    def _avisa(self, args: tuple[str, ...], inicio: float, ok: bool, erro: str) -> None:
        if not self.observador:
            return
        import time
        self.observador(Chamada(
            operacao="git", caminho=args[0] if args else "?",
            duracao_ms=int((time.monotonic() - inicio) * 1000),
            sucesso=ok, status=0 if ok else None, erro=erro[:200]))

    # ---- descoberta ------------------------------------------------------

    def _clones(self) -> list[Path]:
        if not self.raiz.is_dir():
            raise AdapterErro(f"raiz de clones nao existe: {self.raiz}")
        return sorted(p for p in self.raiz.iterdir()
                      if p.is_dir() and (p / ".git").exists())

    def _chave(self, caminho: Path) -> str:
        """Identidade: 'org/repo' do remoto; caminho relativo quando nao ha remoto.

        Ler do remoto e o que impede o diretorio `scamchecker-legado` de ser
        confundido com um repositorio chamado `scamchecker-legado`, que nao
        existe.
        """
        try:
            url = self._git(caminho, "remote", "get-url", "origin").strip()
        except AdapterErro:
            return f"local/{caminho.name}"
        return _org_repo(url) or f"local/{caminho.name}"

    def _base(self, caminho: Path) -> str:
        """Branch de integracao real. Ausencia e ausencia, nunca 'main'."""
        for candidata in CANDIDATAS_BASE:
            try:
                if candidata == "origin/HEAD":
                    saida = self._git(caminho, "symbolic-ref",
                                      "refs/remotes/origin/HEAD").strip()
                    if saida:
                        return saida.rsplit("/", 1)[-1]
                else:
                    self._git(caminho, "rev-parse", "--verify", "--quiet", candidata)
                    return candidata.split("/", 1)[-1] if "/" in candidata else candidata
            except AdapterErro:
                continue
        return ""

    def _monta(self, caminho: Path, completo: bool) -> RepoInfo:
        chave = self._chave(caminho)
        origem = None
        try:
            origem = self._git(caminho, "remote", "get-url", "origin").strip() or None
        except AdapterErro:
            origem = str(caminho)

        dados: dict[str, Any] = {"caminho": str(caminho), "diretorio": caminho.name}
        base = ""
        if completo:
            base = self._base(caminho)
            try:
                dados["branch_corrente"] = self._git(
                    caminho, "symbolic-ref", "--short", "HEAD").strip()
            except AdapterErro:
                dados["branch_corrente"] = "(destacado)"
            # O nome do diretorio divergir do repositorio nao e erro -- e um
            # fato do disco, e o motor precisa poder ve-lo sem investigar.
            if chave.rsplit("/", 1)[-1] != caminho.name:
                dados["diretorio_diverge_do_repo"] = True

        return RepoInfo(
            ref=RepoRef(provider=self.nome, key=chave),
            nome=chave.rsplit("/", 1)[-1],
            branch_base=base,
            origem_de_clone=origem,
            url=_url_web(origem) if origem else None,
            capacidades=self.capacidades,
            parcial=not completo,
            dados=dados)

    def list_repositories(self, filtro: dict[str, Any] | None = None) -> list[RepoInfo]:
        # `completo=True` mesmo na listagem: descobrir a base custa dois comandos
        # locais, e uma listagem sem base nao responde a pergunta que se faz de
        # uma listagem de repositorios. Num provedor de rede a conta seria outra.
        return [self._monta(c, completo=True) for c in self._clones()]

    def get_repository(self, key: str) -> RepoInfo:
        for caminho in self._clones():
            if self._chave(caminho) == key or caminho.name == key:
                return self._monta(caminho, completo=True)
        raise AdapterErro(f"nenhum clone em {self.raiz} corresponde a '{key}'")

    def _caminho_de(self, key: str) -> Path:
        for caminho in self._clones():
            if self._chave(caminho) == key or caminho.name == key:
                return caminho
        raise AdapterErro(f"nenhum clone em {self.raiz} corresponde a '{key}'")

    def list_branches(self, key: str, filtro: dict[str, Any] | None = None) -> list[Branch]:
        caminho = self._caminho_de(key)
        base = self._base(caminho)
        padrao = (filtro or {}).get("padrao", "")
        saida = self._git(caminho, "for-each-ref",
                          "--format=%(refname:short)%09%(objectname)%09%(committerdate:iso8601)",
                          "refs/heads", "refs/remotes/origin")
        vistos: dict[str, Branch] = {}
        for linha in saida.splitlines():
            partes = linha.split("\t")
            if len(partes) < 2:
                continue
            nome = partes[0]
            if nome.startswith("origin/"):
                nome = nome.split("/", 1)[1]
            if nome in ("HEAD", "") or (padrao and padrao.lower() not in nome.lower()):
                continue
            # Local e remoto da mesma branch sao a mesma branch. O primeiro
            # visto vence; duplicar faria o motor achar que ha duas.
            vistos.setdefault(nome, Branch(nome=nome, sha=partes[1],
                                           e_base=(nome == base),
                                           atualizada_em=partes[2] if len(partes) > 2 else ""))
        return sorted(vistos.values(), key=lambda b: b.nome)

    def read_file(self, key: str, caminho_arquivo: str, ref: str | None = None) -> str:
        caminho = self._caminho_de(key)
        alvo = f"{ref or self._base(caminho) or 'HEAD'}:{caminho_arquivo}"
        try:
            return self._git(caminho, "show", alvo)
        except AdapterErro as e:
            raise AdapterErro(f"{caminho_arquivo} nao existe em {key}@{ref or 'base'}") from e


def _org_repo(url: str) -> str:
    """Extrai 'org/repo' de qualquer forma de URL git.

    Cobre https, ssh e o formato `git@host:org/repo.git` -- que nao e uma URL
    valida e por isso escapa de qualquer parser de URL.
    """
    u = url.strip().removesuffix(".git")
    for marca in ("github.com", "gitlab.com", "bitbucket.org", "dev.azure.com"):
        if marca in u:
            resto = u.split(marca, 1)[1].lstrip(":/")
            partes = [p for p in resto.split("/") if p]
            if len(partes) >= 2:
                return "/".join(partes[-2:])
    partes = [p for p in u.replace("\\", "/").split("/") if p]
    return "/".join(partes[-2:]) if len(partes) >= 2 else ""


def _url_web(origem: str) -> str | None:
    if "github.com" in origem:
        return "https://github.com/" + _org_repo(origem)
    return None
