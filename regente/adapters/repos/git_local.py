# -*- coding: utf-8 -*-
"""RepositoryProvider over git clones on disk. READ ONLY.

A real provider, not a rehearsal: anyone working with local clones has a
functioning engine here without depending on the network or on credentials. And
it serves as the second implementation against which the contract is proved --
deliberately of the most different kind possible from the other adapter: a local
process against a remote API.

**Read-only by construction.** Every invocation goes through `_git`, which
refuses any subcommand outside a closed read list. It is not an `if dry_run`:
`git commit` never even gets assembled.

Two facts about the real disk that the design respects:

1. **The directory name is not the repository name.** Measured: the directory
   `scamchecker-legado` points at `silverguard-br/scamchecker`. The identity
   comes from the remote when there is one; from the path only when there is no
   remote.
2. **The current branch is almost never the base.** Of the 12 clones examined, 11
   were on a work branch. Reading `HEAD` as the base would derive new work from
   somebody else's half-finished code.
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


#: Order in which to try to discover the integration branch. The first that
#: exists wins -- and none of them is `HEAD`, which points at the current work.
BASE_BRANCH_CANDIDATES = ("origin/HEAD", "origin/main", "origin/master",
                   "origin/develop", "main", "master")


@dataclass(slots=True)
class GitLocal(RepositoryProvider):
    """Reads git repositories from a directory of clones."""

    root: Path
    name: str = "git-local"
    #: Reflects what the adapter can do: reads everything from disk except pull
    #: requests, which do not exist in plain git -- they are a concept belonging
    #: to the hosting service.
    capabilities: frozenset[RepoCapability] = field(
        default_factory=lambda: READ_CAPS - {RepoCapability.READ_PULL_REQUESTS})
    observer: Observer | None = None
    timeout: int = 60

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "modo": "somente-leitura", "raiz": str(self.root)}

    def verify(self) -> None:
        if not self.root.is_dir():
            raise AdapterError(f"clone root does not exist: {self.root}")

    # ---- running git -----------------------------------------------------

    def _git(self, cwd: Path, *args: str) -> str:
        # The check is per WHOLE INVOCATION, not per verb. `git remote get-url`
        # reads; `git remote set-url` writes, and both start with `remote`.
        ok, reason = git_e_leitura(args)
        if not ok:
            raise ReadOnlyRefused(
                f"'git {' '.join(args)}' refused: {reason}. "
                f"No mutation is executable in this milestone.")
        import time
        inicio = time.monotonic()
        try:
            p = subprocess.run(
                ["git", *args], cwd=str(cwd), capture_output=True,
                # `encoding` is mandatory on Windows: without it, a branch name
                # with an accent turns into mojibake and the key comparison fails
                # silently.
                encoding="utf-8", errors="replace", timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            self._notify_observer(args, inicio, False, f"timeout after {self.timeout}s")
            raise AdapterError(f"git {args[0]} timed out after {self.timeout}s in {cwd}") from e
        except FileNotFoundError as e:
            self._notify_observer(args, inicio, False, "git not found")
            raise AdapterError("git is not on the PATH") from e
        if p.returncode != 0:
            error = (p.stderr or "").strip()[:300]
            self._notify_observer(args, inicio, False, error)
            raise AdapterError(f"git {args[0]} rc={p.returncode} in {cwd.name}: {error}")
        self._notify_observer(args, inicio, True, "")
        return p.stdout

    def _notify_observer(self, args: tuple[str, ...], inicio: float, ok: bool, error: str) -> None:
        if not self.observer:
            return
        import time
        self.observer(Call(
            operation="git", path=args[0] if args else "?",
            duration_ms=int((time.monotonic() - inicio) * 1000),
            success=ok, status=0 if ok else None, error=error[:200]))

    # ---- discovery -------------------------------------------------------

    def _clone_dirs(self) -> list[Path]:
        if not self.root.is_dir():
            raise AdapterError(f"clone root does not exist: {self.root}")
        return sorted(p for p in self.root.iterdir()
                      if p.is_dir() and (p / ".git").exists())

    def _key_of(self, path: Path) -> str:
        """Identity: 'org/repo' from the remote; relative path when there is none.

        Reading from the remote is what stops the directory `scamchecker-legado`
        being mistaken for a repository called `scamchecker-legado`, which does
        not exist.
        """
        try:
            url = self._git(path, "remote", "get-url", "origin").strip()
        except AdapterError:
            return f"local/{path.name}"
        return _org_repo(url) or f"local/{path.name}"

    def _base_branch_of(self, path: Path) -> str:
        """The real integration branch. Absence is absence, never 'main'."""
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
            # The directory name differing from the repository is not an error --
            # it is a fact of the disk, and the engine has to be able to see it
            # without investigating.
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
        # `completo=True` even when listing: discovering the base costs two local
        # commands, and a listing without a base does not answer the question one
        # asks of a repository listing. On a network provider the sum would be
        # different.
        return [self._build(c, completo=True) for c in self._clone_dirs()]

    def get_repository(self, key: str) -> RepoInfo:
        for path in self._clone_dirs():
            if self._key_of(path) == key or path.name == key:
                return self._build(path, completo=True)
        raise AdapterError(f"no clone in {self.root} matches '{key}'")

    def _path_for(self, key: str) -> Path:
        for path in self._clone_dirs():
            if self._key_of(path) == key or path.name == key:
                return path
        raise AdapterError(f"no clone in {self.root} matches '{key}'")

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
            # The local and remote of the same branch are the same branch. The
            # first one seen wins; duplicating would make the engine think there
            # are two.
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
            raise AdapterError(f"{caminho_arquivo} does not exist in {key}@{ref or 'base'}") from e


def _org_repo(url: str) -> str:
    """Extracts 'org/repo' from any form of git URL.

    Covers https, ssh and the `git@host:org/repo.git` form -- which is not a
    valid URL and therefore escapes any URL parser.
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
