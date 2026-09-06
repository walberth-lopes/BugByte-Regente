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

from ...ports import AdapterError, ReadOnlyRefused
from ...ports.repository import (READ_CAPS, Branch, RepoCapability, RepoInfo, RepoRef,
                                 RepositoryProvider)
from ..tasks.transport import Call, Observer
from .readonly import git_e_leitura


#: Ordem de tentativa para descobrir a branch de integracao. A primeira que
#: existir vence -- e nenhuma delas e `HEAD`, que aponta para o trabalho atual.
BASE_BRANCH_CANDIDATES = ("origin/HEAD", "origin/main", "origin/master",
                   "origin/develop", "main", "master")


@dataclass(slots=True)
class GitLocal(RepositoryProvider):
    """Le repositorios git de um diretorio de clones."""

    root: Path
    name: str = "git-local"
    #: Reflete o que o adapter consegue: le tudo do disco, menos pull requests,
    #: que nao existem em git puro -- sao conceito do servico de hospedagem.
    capabilities: frozenset[RepoCapability] = field(
        default_factory=lambda: READ_CAPS - {RepoCapability.LER_PULL_REQUESTS})
    observador: Observer | None = None
    timeout: int = 60

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "modo": "somente-leitura", "raiz": str(self.root)}

    def verify(self) -> None:
        if not self.root.is_dir():
            raise AdapterError(f"raiz de clones nao existe: {self.root}")

    # ---- execucao de git -------------------------------------------------

    def _git(self, cwd: Path, *args: str) -> str:
        # A checagem e por INVOCACAO INTEIRA, nao por verbo. `git remote get-url`
        # le; `git remote set-url` grava, e os dois comecam com `remote`.
        ok, reason = git_e_leitura(args)
        if not ok:
            raise ReadOnlyRefused(
                f"'git {' '.join(args)}' recusado: {reason}. "
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
            self._notify_observer(args, inicio, False, f"timeout apos {self.timeout}s")
            raise AdapterError(f"git {args[0]} estourou {self.timeout}s em {cwd}") from e
        except FileNotFoundError as e:
            self._notify_observer(args, inicio, False, "git nao encontrado")
            raise AdapterError("git nao esta no PATH") from e
        if p.returncode != 0:
            error = (p.stderr or "").strip()[:300]
            self._notify_observer(args, inicio, False, error)
            raise AdapterError(f"git {args[0]} rc={p.returncode} em {cwd.name}: {error}")
        self._notify_observer(args, inicio, True, "")
        return p.stdout

    def _notify_observer(self, args: tuple[str, ...], inicio: float, ok: bool, error: str) -> None:
        if not self.observador:
            return
        import time
        self.observador(Call(
            operation="git", path=args[0] if args else "?",
            duration_ms=int((time.monotonic() - inicio) * 1000),
            success=ok, status=0 if ok else None, error=error[:200]))

    # ---- descoberta ------------------------------------------------------

    def _clone_dirs(self) -> list[Path]:
        if not self.root.is_dir():
            raise AdapterError(f"raiz de clones nao existe: {self.root}")
        return sorted(p for p in self.root.iterdir()
                      if p.is_dir() and (p / ".git").exists())

    def _key_of(self, path: Path) -> str:
        """Identidade: 'org/repo' do remoto; caminho relativo quando nao ha remoto.

        Ler do remoto e o que impede o diretorio `scamchecker-legado` de ser
        confundido com um repositorio chamado `scamchecker-legado`, que nao
        existe.
        """
        try:
            url = self._git(path, "remote", "get-url", "origin").strip()
        except AdapterError:
            return f"local/{path.name}"
        return _org_repo(url) or f"local/{path.name}"

    def _base_branch_of(self, path: Path) -> str:
        """Branch de integracao real. Ausencia e ausencia, nunca 'main'."""
        for candidata in BASE_BRANCH_CANDIDATES:
            try:
                if candidata == "origin/HEAD":
                    output = self._git(path, "symbolic-ref",
                                      "refs/remotes/origin/HEAD").strip()
                    if output:
                        return output.rsplit("/", 1)[-1]
                else:
                    self._git(path, "rev-parse", "--verify", "--quiet", candidata)
                    return candidata.split("/", 1)[-1] if "/" in candidata else candidata
            except AdapterError:
                continue
        return ""

    def _build(self, path: Path, completo: bool) -> RepoInfo:
        key = self._key_of(path)
        source = None
        try:
            source = self._git(path, "remote", "get-url", "origin").strip() or None
        except AdapterError:
            source = str(path)

        data: dict[str, Any] = {"path": str(path), "directory": path.name}
        base = ""
        if completo:
            base = self._base_branch_of(path)
            try:
                data["branch_corrente"] = self._git(
                    path, "symbolic-ref", "--short", "HEAD").strip()
            except AdapterError:
                data["branch_corrente"] = "(destacado)"
            # O nome do diretorio divergir do repositorio nao e error -- e um
            # fato do disco, e o motor precisa poder ve-lo sem investigar.
            if key.rsplit("/", 1)[-1] != path.name:
                data["diretorio_diverge_do_repo"] = True

        return RepoInfo(
            ref=RepoRef(provider=self.name, key=key),
            name=key.rsplit("/", 1)[-1],
            base_branch=base,
            clone_origin=source,
            url=_web_url(source) if source else None,
            capabilities=self.capabilities,
            partial=not completo,
            data=data)

    def list_repositories(self, filtro: dict[str, Any] | None = None) -> list[RepoInfo]:
        # `completo=True` mesmo na listagem: descobrir a base custa dois comandos
        # locais, e uma listagem sem base nao responde a pergunta que se faz de
        # uma listagem de repositorios. Num provedor de rede a conta seria outra.
        return [self._build(c, completo=True) for c in self._clone_dirs()]

    def get_repository(self, key: str) -> RepoInfo:
        for path in self._clone_dirs():
            if self._key_of(path) == key or path.name == key:
                return self._build(path, completo=True)
        raise AdapterError(f"nenhum clone em {self.root} corresponde a '{key}'")

    def _path_for(self, key: str) -> Path:
        for path in self._clone_dirs():
            if self._key_of(path) == key or path.name == key:
                return path
        raise AdapterError(f"nenhum clone em {self.root} corresponde a '{key}'")

    def list_branches(self, key: str, filtro: dict[str, Any] | None = None) -> list[Branch]:
        path = self._path_for(key)
        base = self._base_branch_of(path)
        default_value = (filtro or {}).get("padrao", "")
        output = self._git(path, "for-each-ref",
                          "--format=%(refname:short)%09%(objectname)%09%(committerdate:iso8601)",
                          "refs/heads", "refs/remotes/origin")
        vistos: dict[str, Branch] = {}
        for linha in output.splitlines():
            partes = linha.split("\t")
            if len(partes) < 2:
                continue
            name = partes[0]
            if name.startswith("origin/"):
                name = name.split("/", 1)[1]
            if name in ("HEAD", "") or (default_value and default_value.lower() not in name.lower()):
                continue
            # Local e remoto da mesma branch sao a mesma branch. O primeiro
            # visto vence; duplicar faria o motor achar que ha duas.
            vistos.setdefault(name, Branch(name=name, sha=partes[1],
                                           e_base=(name == base),
                                           updated_at=partes[2] if len(partes) > 2 else ""))
        return sorted(vistos.values(), key=lambda b: b.name)

    def read_file(self, key: str, caminho_arquivo: str, ref: str | None = None) -> str:
        path = self._path_for(key)
        target = f"{ref or self._base_branch_of(path) or 'HEAD'}:{caminho_arquivo}"
        try:
            return self._git(path, "show", target)
        except AdapterError as e:
            raise AdapterError(f"{caminho_arquivo} nao existe em {key}@{ref or 'base'}") from e


def _org_repo(url: str) -> str:
    """Extrai 'org/repo' de qualquer forma de URL git.

    Cobre https, ssh e o formato `git@host:org/repo.git` -- que nao e uma URL
    valida e por isso escapa de qualquer parser de URL.
    """
    u = url.strip().removesuffix(".git")
    for mark in ("github.com", "gitlab.com", "bitbucket.org", "dev.azure.com"):
        if mark in u:
            resto = u.split(mark, 1)[1].lstrip(":/")
            partes = [p for p in resto.split("/") if p]
            if len(partes) >= 2:
                return "/".join(partes[-2:])
    partes = [p for p in u.replace("\\", "/").split("/") if p]
    return "/".join(partes[-2:]) if len(partes) >= 2 else ""


def _web_url(source: str) -> str | None:
    if "github.com" in source:
        return "https://github.com/" + _org_repo(source)
    return None
