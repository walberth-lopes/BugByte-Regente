# -*- coding: utf-8 -*-
"""Isolated work areas on the local disk.

Two implementations behind the same port:

- `IsolatedDirectory`: one folder per run. Enough when the work does not require
  a clone -- analysis, documentation, artefact generation.
- `GitWorktree`: one git worktree per run. This is the real isolation for code
  work: each worker has its own tree and its own branch, and the classic failure
  mode of two agents switching branches in the same clone does not exist.

`discard` is idempotent in both: cleanup happens after a crash, when whoever
created the area no longer exists, so "it is not there any more" is success.
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


class GitWorktree(WorkspaceProvider):
    name = "worktree"

    def __init__(self, clones: dict[str, str], root: str | Path):
        #: repo -> path of the main clone
        self.clones = {k: Path(v) for k, v in clones.items()}
        self.root = Path(root)

    def verify(self) -> None:
        for repo, path in self.clones.items():
            if not (path / ".git").exists():
                raise AdapterError(f"clone of {repo} is not a git repository: {path}")

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
            raise AdapterError(f"no clone configured for '{repo}'")
        clone = self.clones[repo]
        destination = self.root / key
        nome_branch = branch or f"regente/{key}"
        # Resuming: the worktree already exists and carries the previous
        # attempt's WIP commits. Recreating it would lose the work -- it is
        # returned as it is.
        if (destination / ".git").exists():
            return WorkArea(id=key, path=str(destination), branch=nome_branch, repo=repo)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Fetch before deriving: a worktree created from a stale base produces a
        # PR full of conflicts nobody asked for.
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
                pass   # worktree already removed or clone gone: fall back to brute force
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
    sources: dict[str, str] = field(default_factory=dict)
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
        head = self._git("rev-parse", "HEAD", cwd=destination).strip()
        return WorkArea(id=key, path=str(destination), branch=work_branch, repo=repo,
                        data={"source": source, "base": base or "", "head_at_clone": head})

    def discard(self, area: WorkArea) -> None:
        shutil.rmtree(area.path, ignore_errors=True)

    def list_areas(self) -> list[WorkArea]:
        if not self.root.is_dir():
            return []
        return [WorkArea(id=p.name, path=str(p)) for p in self.root.iterdir() if p.is_dir()]
