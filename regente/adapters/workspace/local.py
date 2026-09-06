# -*- coding: utf-8 -*-
"""Areas de trabalho isoladas no disco local.

Duas implementacoes atras da mesma port:

- `DiretorioIsolado`: uma pasta por run. Suficiente quando o trabalho nao exige
  um clone -- analise, documentacao, geracao de artefato.
- `GitWorktree`: um worktree do git por run. E o isolamento real para trabalho de
  codigo: cada worker tem sua propria arvore e sua propria branch, e nao existe
  o modo de falha classico de dois agentes trocando de branch no mesmo clone.

`discard` e idempotente nas duas: limpeza acontece depois de crash, quando quem
criou a area ja nao existe, entao "ja nao esta la" e success.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ...ports import AdapterError
from ...ports.workspace import WorkArea, WorkspaceProvider


class IsolatedDirectory(WorkspaceProvider):
    name = "directory"

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def verify(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare(self, key: str, repo: str | None = None,
                branch: str | None = None, base: str | None = None) -> WorkArea:
        path = self.root / key
        path.mkdir(parents=True, exist_ok=True)
        return WorkArea(id=key, path=str(path), branch=branch, repo=repo)

    def discard(self, area: WorkArea) -> None:
        shutil.rmtree(area.path, ignore_errors=True)

    def list_areas(self) -> list[WorkArea]:
        if not self.root.is_dir():
            return []
        return [WorkArea(id=p.name, path=str(p)) for p in self.root.iterdir() if p.is_dir()]

    # ---- writing history, inside the area only --------------------------

    def head(self, area: WorkArea) -> str:
        return self._git("rev-parse", "HEAD", cwd=Path(area.path)).strip()

    def is_dirty(self, area: WorkArea) -> bool:
        return bool(self._git("status", "--porcelain", cwd=Path(area.path)).strip())

    def commit(self, area: WorkArea, message: str,
               author: tuple[str, str] | None = None) -> str:
        path = Path(area.path)
        current = self._git("rev-parse", "--abbrev-ref", "HEAD", cwd=path).strip()

        # Refusing here rather than trusting the caller: the engine builds the
        # work branch, so being on anything else means something upstream went
        # wrong, and a commit is a terrible place to find that out.
        if current in ("HEAD", "main", "master", "develop"):
            raise AdapterError(
                f"refused to commit on '{current}': the isolated area must be on "
                f"its own work branch, never on an integration branch")
        if area.branch and current != area.branch:
            raise AdapterError(
                f"refused to commit: the area is on '{current}' and the run owns "
                f"'{area.branch}'")

        if not self.is_dirty(area):
            raise AdapterError("nothing to commit: the area has no changes")

        # `--no-verify` is deliberately NOT used: a repository's own hooks are
        # part of its rules, and an engine that skips them is writing history
        # the team did not agree to.
        name, email = author or ("Regente", "regente@localhost.invalid")
        self._git("add", "-A", cwd=path)
        self._git("-c", f"user.name={name}", "-c", f"user.email={email}",
                  "commit", "-q", "-m", message, cwd=path)
        return self.head(area)


class GitWorktree(WorkspaceProvider):
    name = "worktree"

    def __init__(self, clones: dict[str, str], root: str | Path):
        #: repo -> caminho do clone principal
        self.clones = {k: Path(v) for k, v in clones.items()}
        self.root = Path(root)

    def verify(self) -> None:
        for repo, path in self.clones.items():
            if not (path / ".git").exists():
                raise AdapterError(f"clone de {repo} nao e um repositorio git: {path}")

    def _git(self, cwd: Path, *args: str) -> str:
        p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                           encoding="utf-8", errors="replace", timeout=300)
        if p.returncode != 0:
            raise AdapterError(f"git {' '.join(args[:3])} rc={p.returncode}: "
                              f"{(p.stderr or '').strip()[:300]}")
        return p.stdout

    def prepare(self, key: str, repo: str | None = None,
                branch: str | None = None, base: str | None = None) -> WorkArea:
        if repo not in self.clones:
            raise AdapterError(f"nenhum clone configurado para '{repo}'")
        clone = self.clones[repo]
        destination = self.root / key
        nome_branch = branch or f"regente/{key}"
        # Retomada: o worktree ja existe e carrega os commits WIP da tentativa
        # anterior. Recria-lo perderia o trabalho -- devolve-se como esta.
        if (destination / ".git").exists():
            return WorkArea(id=key, path=str(destination), branch=nome_branch, repo=repo)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Busca antes de derivar: worktree criado a partir de base velha produz
        # PR cheio de conflito que ninguem pediu.
        self._git(clone, "fetch", "--quiet", "origin")
        self._git(clone, "worktree", "add", "-b", nome_branch, str(destination),
                  base or "origin/HEAD")
        return WorkArea(id=key, path=str(destination), branch=nome_branch, repo=repo)

    def discard(self, area: WorkArea) -> None:
        clone = self.clones.get(area.repo or "")
        if clone:
            try:
                self._git(clone, "worktree", "remove", "--force", area.path)
                return
            except AdapterError:
                pass   # worktree ja removido ou clone sumiu: seguimos na unha
        shutil.rmtree(area.path, ignore_errors=True)

    def list_areas(self) -> list[WorkArea]:
        if not self.root.is_dir():
            return []
        return [WorkArea(id=p.name, path=str(p)) for p in self.root.iterdir() if p.is_dir()]


@dataclass(slots=True)
class GitClone(WorkspaceProvider):
    """Clones a repository into an isolated area. Never touches the source.

    Chosen over a worktree for this milestone for one reason: `git worktree add`
    WRITES into the source repository -- it creates a branch ref and a
    `.git/worktrees` entry. On this machine two of the real clones carry
    uncommitted work, and an engine that promises to touch nothing must also not
    touch the checkout someone is in the middle of using.

    A clone is strictly more expensive and strictly safer. When the source is a
    local path, `--no-hardlinks` keeps the copy independent: with hardlinks, a
    later `git gc` in the clone can reach objects the original still needs.
    """

    root: Path
    #: repo key -> where to clone FROM. May be a fast local path.
    sources: dict[str, str] = field(default_factory=dict)
    #: repo key -> where a push may go. The real remote, and nothing else.
    #:
    #: Separate from `sources` on purpose. Cloning from a local path is a speed
    #: decision; pushing to it is a mutation of somebody's checkout. Conflating
    #: the two is what made the dangerous configuration the default one: git
    #: sets `origin` to whatever it cloned from, so an area cloned locally comes
    #: pre-aimed at the source.
    remotes: dict[str, str] = field(default_factory=dict)
    name: str = "clone"
    timeout: int = 600

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "root": str(self.root)}

    def verify(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        p = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                           capture_output=True, encoding="utf-8",
                           errors="replace", timeout=self.timeout)
        if p.returncode != 0:
            raise AdapterError(f"git {args[0]} rc={p.returncode}: "
                               f"{(p.stderr or '').strip()[:300]}")
        return p.stdout

    def prepare(self, key: str, repo: str | None = None,
                branch: str | None = None, base: str | None = None) -> WorkArea:
        source = self.sources.get(repo or "", repo or "")
        if not source:
            raise AdapterError(f"no clone source configured for '{repo}'")
        destination = self.root / key
        work_branch = branch or f"regente/{key}"

        if (destination / ".git").exists():
            # Resuming: the area already carries the previous attempt's WIP.
            # Recreating it would throw away exactly what recovery exists to save.
            return WorkArea(id=key, path=str(destination), branch=work_branch,
                            repo=repo, data={"source": source, "resumed": True})

        destination.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--no-hardlinks", "--quiet"]
        if base:
            args += ["--branch", base]
        args += [source, str(destination)]
        self._git(*args)

        # The work branch is created here, never checked out from the base by
        # name: the agent must not be able to land on the integration branch by
        # accident, and a branch that already exists must fail loudly.
        self._git("checkout", "-q", "-b", work_branch, cwd=destination)
        self._aim_remote(destination, repo)
        head = self._git("rev-parse", "HEAD", cwd=destination).strip()
        return WorkArea(id=key, path=str(destination), branch=work_branch, repo=repo,
                        data={"source": source, "base": base or "", "head_at_clone": head,
                              "push_target": self.push_target_of(destination) or ""})

    def _aim_remote(self, destination: Path, repo: str | None) -> None:
        """Point `origin` at the real remote, or remove it entirely.

        There is no third option on purpose. Leaving `origin` on the local source
        would make the safest-looking configuration the one that writes into
        somebody's checkout, and leaving a half-configured remote would fail at
        push time -- after the work, when failing is most expensive.

        Absence of configuration must make a push IMPOSSIBLE, never accidentally
        local. That is why the fallback deletes the remote rather than keeping it.
        """
        remote = self.remotes.get(repo or "")
        if remote:
            self._git("remote", "set-url", "origin", remote, cwd=destination)
            self._git("remote", "set-url", "--push", "origin", remote, cwd=destination)
            return
        try:
            self._git("remote", "remove", "origin", cwd=destination)
        except AdapterError:
            pass   # no remote to remove is the state we wanted anyway

    def push_target_of(self, path: Path | str) -> str | None:
        """Where a push from this area would actually go. `None` when nowhere."""
        try:
            return self._git("remote", "get-url", "--push", "origin",
                             cwd=Path(path)).strip() or None
        except AdapterError:
            return None

    def push_target(self, area: WorkArea) -> str | None:
        return self.push_target_of(area.path)

    def discard(self, area: WorkArea) -> None:
        shutil.rmtree(area.path, ignore_errors=True)

    def list_areas(self) -> list[WorkArea]:
        if not self.root.is_dir():
            return []
        return [WorkArea(id=p.name, path=str(p)) for p in self.root.iterdir() if p.is_dir()]

    # ---- writing history, inside the area only --------------------------

    def head(self, area: WorkArea) -> str:
        return self._git("rev-parse", "HEAD", cwd=Path(area.path)).strip()

    def is_dirty(self, area: WorkArea) -> bool:
        return bool(self._git("status", "--porcelain", cwd=Path(area.path)).strip())

    def commit(self, area: WorkArea, message: str,
               author: tuple[str, str] | None = None) -> str:
        path = Path(area.path)
        current = self._git("rev-parse", "--abbrev-ref", "HEAD", cwd=path).strip()

        # Refusing here rather than trusting the caller: the engine builds the
        # work branch, so being on anything else means something upstream went
        # wrong, and a commit is a terrible place to find that out.
        if current in ("HEAD", "main", "master", "develop"):
            raise AdapterError(
                f"refused to commit on '{current}': the isolated area must be on "
                f"its own work branch, never on an integration branch")
        if area.branch and current != area.branch:
            raise AdapterError(
                f"refused to commit: the area is on '{current}' and the run owns "
                f"'{area.branch}'")

        if not self.is_dirty(area):
            raise AdapterError("nothing to commit: the area has no changes")

        # `--no-verify` is deliberately NOT used: a repository's own hooks are
        # part of its rules, and an engine that skips them is writing history
        # the team did not agree to.
        name, email = author or ("Regente", "regente@localhost.invalid")
        self._git("add", "-A", cwd=path)
        self._git("-c", f"user.name={name}", "-c", f"user.email={email}",
                  "commit", "-q", "-m", message, cwd=path)
        return self.head(area)
