# -*- coding: utf-8 -*-
"""Areas de trabalho isoladas no disco local.

Duas implementacoes atras da mesma porta:

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
from pathlib import Path

from ...ports import AdapterError
from ...ports.workspace import WorkArea, WorkspaceProvider


class IsolatedDirectory(WorkspaceProvider):
    name = "diretorio"

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
