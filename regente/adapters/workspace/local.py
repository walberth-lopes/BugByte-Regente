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

from ...core.credential import Use
from ...ports import AdapterError
from ...ports.support import CredentialBroker
from ...ports.workspace import WorkArea, WorkspaceProvider
from . import git_process


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

    # ---- escrever historia: esta area nao sabe, e diz isso -------------
    #
    # Ate o marco 6, esta classe tinha `head`, `is_dirty`, `commit` e `push`
    # copiados do provedor de clone. Os quatro chamavam `self._git`, que NAO
    # EXISTE aqui -- e este e o provedor PADRAO, o que a configuracao de exemplo
    # traz. Executados, davam `AttributeError`, que nao e uma recusa: e um
    # defeito com cara de bug do motor.
    #
    # E funcionar nem seria o certo. Uma pasta nao e um clone; nao ha historia
    # para escrever nem remoto para onde empurrar. O que faltava aqui nao era
    # implementacao, era uma recusa nomeada.

    def _sem_historia(self, verbo: str):
        return AdapterError(
            f"esta area e uma pasta, nao um clone git: nao ha como {verbo}. "
            f"Para entregar codigo, configure `workspace_provider: clone`")

    def head(self, area: WorkArea) -> str:
        raise self._sem_historia("ler o commit corrente")

    def is_dirty(self, area: WorkArea) -> bool:
        raise self._sem_historia("dizer se ha mudanca pendente")

    def commit(self, area: WorkArea, message: str,
               author: tuple[str, str] | None = None) -> str:
        raise self._sem_historia("escrever um commit")

    def push(self, area: WorkArea, expected_sha: str,
             branch: str | None = None) -> str:
        raise self._sem_historia("empurrar")


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
    #: A porta governada do marco 16, ja presa a quem age, a que workspace e a
    #: que provider. `None` significa: este provedor nao empurra -- e recusa,
    #: em vez de deixar o `git` procurar credencial sozinho.
    credentials: CredentialBroker | None = None
    #: O nome de usuario do par HTTP Basic. Nao e segredo: para um token de
    #: hospedagem qualquer nome serve, e o que autentica e a senha. Configuravel
    #: porque o valor esperado varia por fornecedor.
    credential_user: str = "x-access-token"

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "root": str(self.root)}

    def verify(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _git(self, *args: str, cwd: Path | None = None,
             remote: str = "") -> str:
        """Roda `git` com ambiente CLASSIFICADO, nunca herdado.

        Sem `remote`, a invocacao e local -- `rev-parse`, `status`, `commit` --
        e nao recebe credencial nenhuma. Com `remote`, o material vem do broker
        pelo caminho governado, e so entao.

        Herdar o ambiente era o que deixava o `credential.helper` global do
        usuario autenticar em nome do motor, sem passar por lugar nenhum.
        """
        ambiente = git_process.child_environment(
            remote, self.credentials, self.credential_user)
        launch = ambiente.launch(Use.REPO_PUSH) if remote else ambiente.plain()
        p = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                           capture_output=True, encoding="utf-8",
                           errors="replace", timeout=self.timeout,
                           env=launch.env)
        if p.returncode != 0:
            # Limpo ANTES de truncar: cortar primeiro deixa metade do cabecalho,
            # e metade de um base64 ainda e a parte que ninguem deveria escrever.
            erro = launch.scrub((p.stderr or "").strip())[:300]
            raise AdapterError(f"git {args[0]} rc={p.returncode}: {erro}")
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

    #: Branch names a push may never target, whatever the caller asks for.
    INTEGRATION_BRANCHES = frozenset({"main", "master", "develop", "dev",
                                      "release", "staging", "production", "HEAD"})

    def push(self, area: WorkArea, expected_sha: str,
             branch: str | None = None) -> str:
        """Publish the work branch. Five refusals, checked in this order.

        Order matters: the cheapest and most dangerous checks run first, so a
        misconfigured call never reaches the network.
        """
        path = Path(area.path)
        target_branch = branch or area.branch
        if not target_branch:
            raise AdapterError("refused: the area has no work branch")

        # 1. Never an integration branch, whoever asked.
        if target_branch in self.INTEGRATION_BRANCHES:
            raise AdapterError(
                f"refused to push '{target_branch}': it is an integration branch")

        # 2. Never a branch this run does not own.
        current = self._git("rev-parse", "--abbrev-ref", "HEAD", cwd=path).strip()
        if current != target_branch:
            raise AdapterError(
                f"refused to push: the area is on '{current}' and the push asks "
                f"for '{target_branch}'")

        # 3. Never to a local path. The whole point of separating `sources` from
        #    `remotes` is that a local target means somebody's checkout.
        destination = self.push_target_of(path)
        if not destination:
            raise AdapterError(
                "refused to push: this area has no push target. Absence of "
                "configuration means a push is impossible, not local")
        if _looks_local(destination):
            raise AdapterError(
                f"refused to push to a local path: {destination}")

        # 4. Never a target whose authentication the engine cannot govern. An
        #    `ssh://` remote would work -- through the user's agent, which is
        #    authority nobody granted the engine and nobody can revoke from it.
        git_process.refuse_unless_governable(destination)

        # 5. Never work nobody verified. Between validation and push the branch
        #    may have moved, and pushing then vouches for an unseen commit.
        actual = self.head(area)
        if actual != expected_sha:
            raise AdapterError(
                f"refused to push: expected {expected_sha[:12]} and the branch "
                f"is at {actual[:12]}; the work changed after it was validated")

        # 6. Never rewrite history. `--force-with-lease` is still a force, and
        #    the engine has no business overwriting anyone's refs.
        #
        # `remote=` is what turns this single invocation into the one that
        # carries a credential. Every other `git` call in this class runs
        # without one, and none of them can acquire one by being edited: the
        # material only exists inside `launch()`.
        self._git("push", "--set-upstream", "origin",
                  f"{target_branch}:{target_branch}", cwd=path,
                  remote=destination)
        return actual

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


def _looks_local(destination: str) -> bool:
    """True quando o alvo e um caminho no disco, e nao um servidor.

    **Esta funcao nao existia.** Era chamada em dois lugares e definida em
    nenhum, e `GitClone.push` levantava `NameError` em toda execucao real --
    por cinco marcos, porque a entrega era provada contra um dublê que nao tem
    esta linha e o unico teste do alvo de push nunca chamou `push`.

    A guarda existe porque `git` aponta `origin` para o que foi clonado: uma
    area clonada de um caminho local ja nasce mirando o checkout de alguem, e a
    configuracao que parece mais segura -- clonar local, e mais rapido -- e a
    que escreve no repositorio de trabalho de outra pessoa.

    Reconhece esquema de rede primeiro e trata todo o resto como local. Na
    duvida, LOCAL: recusar um push legitimo custa uma mensagem legivel; aceitar
    um push para o disco de alguem custa o trabalho dessa pessoa.
    """
    alvo = (destination or "").strip()
    if not alvo:
        return True
    baixo = alvo.lower()
    if baixo.startswith(("https://", "http://", "ssh://", "git://", "ftp://",
                         "ftps://")):
        return False
    # `git@host:org/repo` -- scp-like, que nao e URL e escapa de qualquer parser.
    antes_da_barra = baixo.split("/", 1)[0]
    if "@" in antes_da_barra and ":" in antes_da_barra:
        return False
    return True
