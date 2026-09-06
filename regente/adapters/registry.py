# -*- coding: utf-8 -*-
"""Registro de adapters: nome na configuracao -> fabrica.

Este e o **unico** modulo do motor que importa adapters, e ele fica fora do Core
e do Engine de proposito. Adicionar um provedor novo e adicionar uma entrada
aqui; se algum dia for preciso mexer em `core/` ou `engine/` para isso, a
abstracao falhou -- e o teste de fronteira acusa.

O import e tardio (dentro da fabrica) para que um adapter com dependencia pesada
nao seja exigido de quem nao o usa: o motor precisa subir num ambiente sem SDK
de nuvem nenhum.
"""

from __future__ import annotations

from typing import Any, Callable

from ..ports import Capability, Port

Fabrica = Callable[[dict[str, Any]], Port]
_REGISTRO: dict[tuple[Capability, str], Fabrica] = {}


def register(cap: Capability, name: str, fabrica: Fabrica) -> None:
    _REGISTRO[(cap, name)] = fabrica


def create(cap: Capability, name: str, options: dict[str, Any] | None = None) -> Port:
    key = (cap, name)
    if key not in _REGISTRO:
        available = sorted(n for (c, n) in _REGISTRO if c == cap)
        raise KeyError(
            f"nao existe adapter '{name}' para {cap.value}. "
            f"Disponiveis: {', '.join(available) or 'nenhum'}")
    return _REGISTRO[key](options or {})


def available(cap: Capability | None = None) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    for (c, n) in sorted(_REGISTRO, key=lambda k: (k[0].value, k[1])):
        if cap is None or c == cap:
            output.setdefault(c.value, []).append(n)
    return output


# ---- fabricas embutidas -------------------------------------------------

def _tasks_filesystem(o: dict[str, Any]) -> Port:
    from .tasks.filesystem import FilesystemTasks
    return FilesystemTasks(o["directory"])


def _workspace_directory(o: dict[str, Any]) -> Port:
    from .workspace.local import IsolatedDirectory
    return IsolatedDirectory(o["root"])


def _workspace_worktree(o: dict[str, Any]) -> Port:
    from .workspace.local import GitWorktree
    return GitWorktree(clones=o.get("clones", {}), root=o["root"])


def _tasks_jira(o: dict[str, Any]) -> Port:
    """Jira Cloud, somente leitura.

    Dois transportes pelo mesmo adapter: `http` fala com o site de verdade,
    `instantaneo` reproduz respostas reais gravadas. O adapter e identico nos
    dois casos -- e por isso o teste de contrato exercita o mesmo codigo que
    roda contra a rede.
    """
    from .tasks.jira import JiraTasks
    from .tasks.transport import HttpTransport, SnapshotTransport

    observer = o.get("observer")
    modo = o.get("transport", "http")
    if modo == "instantaneo":
        from pathlib import Path
        transport = SnapshotTransport(
            directory=Path(o["snapshots"]), observer=observer)
    elif modo == "http":
        site = o["site"].rstrip("/")
        secrets = o["segredos"]           # SecretProvider, injetado pela composicao
        ref_usuario = o.get("user_ref") or "env:JIRA_EMAIL"
        ref_token = o.get("token_ref") or "env:JIRA_API_TOKEN"
        transport = HttpTransport(
            base_url=site,
            credencial=lambda: (secrets.resolve(ref_usuario), secrets.resolve(ref_token)),
            timeout=int(o.get("timeout", 30)),
            max_attempts=int(o.get("max_tentativas", 3)),
            observer=observer)
    else:
        raise KeyError(f"transport desconhecido para jira: {modo!r}. Use http ou instantaneo")

    return JiraTasks(
        transport=transport,
        jql=o.get("jql") or "statusCategory != Done ORDER BY updated DESC",
        resources_by=o.get("resources_by", "parent"),
        max_pages=int(o.get("max_pages", 10)),
        per_page=int(o.get("per_page", 100)),
        site=o.get("site", ""))


def _repos_git_local(o: dict[str, Any]) -> Port:
    from .repos.git_local import GitLocal
    return GitLocal(root=o["root"], observer=o.get("observer"),
                    timeout=int(o.get("timeout", 60)))


def _repos_github(o: dict[str, Any]) -> Port:
    from .repos.github import GitHubRepos
    return GitHubRepos(org=o["org"], cli_path=o.get("cli", "gh"),
                       observer=o.get("observer"),
                       timeout=int(o.get("timeout", 60)),
                       list_limit=int(o.get("limit", 200)))


def _scoped_secrets(o: dict[str, Any]) -> Port:
    from .secrets import ScopedSecrets
    return ScopedSecrets(allowed_from=frozenset(o.get("allowed", ())),
                    workspace=o.get("workspace", "?"))


def _workspace_clone(o: dict[str, Any]) -> Port:
    from .workspace.local import GitClone
    return GitClone(root=o["root"], sources=o.get("sources", {}))


def _notify_console(o: dict[str, Any]) -> Port:
    from .notify.console import Console
    return Console(journal=o.get("journal"))


def _runner_script(o: dict[str, Any]) -> Port:
    from .runner.scripted import ScriptedRunner
    return ScriptedRunner(script=o.get("script", {}), default_value=o.get("fallback", {"ok": True, "resumo": "sem alteracao"}))


def _runner_command(o: dict[str, Any]) -> Port:
    from .runner.scripted import CommandRunner
    return CommandRunner(command=list(o["command"]))


register(Capability.TASKS, "filesystem", _tasks_filesystem)
register(Capability.TASKS, "jira", _tasks_jira)
register(Capability.SECRETS, "scoped", _scoped_secrets)
register(Capability.REPOSITORY, "git-local", _repos_git_local)
register(Capability.REPOSITORY, "github", _repos_github)
register(Capability.WORKSPACE, "directory", _workspace_directory)
register(Capability.WORKSPACE, "worktree", _workspace_worktree)
register(Capability.WORKSPACE, "clone", _workspace_clone)
register(Capability.NOTIFICATION, "console", _notify_console)
register(Capability.RUNNER, "script", _runner_script)
register(Capability.RUNNER, "command", _runner_command)
