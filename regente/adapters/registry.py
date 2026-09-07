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
    from ..core.credential import Use
    from .tasks.jira import JiraTasks
    from .tasks.transport import HttpTransport, SnapshotTransport

    observer = o.get("observer")
    modo = o.get("transport", "http")
    if modo == "snapshot":
        from pathlib import Path
        transport = SnapshotTransport(
            directory=Path(o["snapshots"]), observer=observer)
    elif modo == "http":
        site = o["site"].rstrip("/")
        # A CREDENCIAL vem do caminho governado, e nada mais.
        #
        # Antes, a composicao injetava um `SecretProvider` e este arquivo
        # montava um `lambda` que resolvia uma referencia escrita no YAML. Nao
        # havia identidade, concessao, capacidade, validade nem revogacao -- e
        # funcionava perfeitamente, que e o que tornava o defeito duravel.
        #
        # O usuario NAO passa por aqui: um email e um identificador, nao um
        # segredo, e trata-lo como segredo esconderia o que e realmente secreto.
        broker = o["credentials"]
        usuario = str(o.get("user") or "")
        transport = HttpTransport(
            base_url=site,
            credencial=lambda: (usuario, broker.material(Use.TASK_READ)),
            timeout=int(o.get("timeout", 30)),
            max_attempts=int(o.get("max_tentativas", 3)),
            observer=observer)
    else:
        raise KeyError(f"transport desconhecido para jira: {modo!r}. "
                       f"Use http ou snapshot")

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


def _agent_headless(o: dict[str, Any]) -> Port:
    """Any headless agent that speaks JSON on stdin/stdout, sandboxed.

    This is the factory for an agent reached over an API or through a corporate
    gateway: the process is whatever the client wrote, and the engine resolves
    the credential it needs and hands it over in a composed environment.
    """
    from .runner.headless import HeadlessAgent, SandboxProfile

    mode, env, variaveis, broker = _auth({**o, "auth": o.get("auth", "none")})
    return HeadlessAgent(
        command=list(o["command"]),
        auth_mode=mode,
        sandbox=SandboxProfile(
            allowed_tools=tuple(o.get("tools", ())),
            denied_tools=tuple(o.get("denied_tools", ())),
            allow_command_execution=bool(o.get("allow_command_execution", False)),
            allow_network=bool(o.get("allow_network", False)),
            env=env,
            credential_env=variaveis, broker=broker,
            max_cost_usd=float(o.get("max_cost_usd", 2.0))))


def _auth(o: dict[str, Any]) -> tuple[Any, dict[str, str], tuple[str, ...], Any]:
    """Resolve whatever THIS workspace's agent authenticates with.

    Five shapes, and the engine picks none of them: the workspace's
    configuration does. A client with a corporate coding-agent subscription
    writes `auth: session` and the engine resolves nothing. A client with a key
    writes `auth: resolved_secret` and names the reference. A client behind an
    internal gateway writes `auth: gateway` and names its own variables.

    The credential values never appear here -- only references, resolved through
    the workspace's own SecretProvider, which is what keeps one client's
    credential out of another client's agent.
    """
    from ..ports.agent import AuthMode

    mode = AuthMode(str(o.get("auth", "session")).upper())
    broker = o.get("credentials")
    env: dict[str, str] = {}

    # `agent_env` names the variables the agent expects. O VALOR nao e resolvido
    # aqui: cada nome vira uma promessa que o adapter cumpre NO MOMENTO DE
    # RODAR, pelo caminho governado.
    #
    # Antes, isto resolvia o material durante a CONSTRUCAO do adapter -- antes
    # de existir identidade, antes de a policy ser consultada, antes de qualquer
    # decisao sobre se aquele uso era autorizado. Um objeto construido carregava
    # o segredo em memoria pelo resto do processo, e ninguem havia autorizado
    # nada.
    variaveis = tuple(str(v) for v in (o.get("agent_env") or ()))

    if mode in (AuthMode.RESOLVED_SECRET, AuthMode.GATEWAY) and not variaveis:
        raise KeyError(
            f"auth mode '{mode.value}' needs an `agent_env` list of "
            f"variable -> secret reference; none was configured")
    return mode, env, variaveis, broker


def _agent_claude_code(o: dict[str, Any]) -> Port:
    """One headless coding-agent CLI. The vendor's name stops at this factory."""
    from .runner.vendors.claude_code import ClaudeCodeAgent, restricted_profile

    mode, env, variaveis, broker = _auth(o)
    return ClaudeCodeAgent(
        cli_path=o["cli"],
        model=str(o.get("model", "sonnet")),
        auth_mode=mode,
        sandbox=restricted_profile(
            max_cost_usd=float(o.get("max_cost_usd", 2.0)), env=env,
            credential_env=variaveis, broker=broker),
        extra_args=tuple(o.get("extra_args", ())))


def _agent_codex_cli(o: dict[str, Any]) -> Port:
    """A second headless coding-agent CLI, to prove the swap is a config line.

    Identical shape to the factory above and to nothing above `adapters/`. If
    adding this had required a change in `core/`, `ports/` or `engine/`, the
    abstraction would have failed and the boundary test would say so.
    """
    from .runner.vendors.codex_cli import CodexCliAgent, restricted_profile

    mode, env, variaveis, broker = _auth(o)
    return CodexCliAgent(
        cli_path=o["cli"],
        model=str(o.get("model", "")),
        auth_mode=mode,
        sandbox=restricted_profile(
            max_cost_usd=float(o.get("max_cost_usd", 2.0)), env=env,
            credential_env=variaveis, broker=broker),
        extra_args=tuple(o.get("extra_args", ())))


def _agent_deterministic(o: dict[str, Any]) -> Port:
    from .runner.external import DeterministicAgent
    return DeterministicAgent(
        script=dict(o.get("script", {})),
        fallback=dict(o.get("fallback", {})) or {
            "status": "NO_PROGRESS",
            "summary": "no edit declared for this task"})


def _repos_github_write(o: dict[str, Any]) -> Port:
    from .repos.github_write import GitHubWrite
    return GitHubWrite(org=o["org"], cli_path=o.get("cli", "gh"),
                       observer=o.get("observer"))


def _cicd_github(o: dict[str, Any]) -> Port:
    from .cicd.github_checks import GitHubChecks
    return GitHubChecks(org=o["org"], cli_path=o.get("cli", "gh"),
                        observer=o.get("observer"))


def _scoped_secrets(o: dict[str, Any]) -> Port:
    """DESREGISTRADO no marco 16. Mantido como funcao, sem porta.

    Enquanto esta fabrica estava registrada, qualquer composicao podia pedir um
    `SecretProvider` inteiro pelo nome e entrega-lo a um adapter -- que e
    exatamente o caminho legado que este marco eliminou. Quem monta a fonte de
    segredo agora e o `CredentialService`, e ele e o unico.
    """
    from .secrets import ScopedSecrets
    return ScopedSecrets(allowed_from=frozenset(o.get("allowed", ())),
                    workspace=o.get("workspace", "?"))


def _workspace_clone(o: dict[str, Any]) -> Port:
    from .workspace.local import GitClone
    return GitClone(root=o["root"], sources=o.get("sources", {}),
                    remotes=o.get("remotes", {}))


def _notify_console(o: dict[str, Any]) -> Port:
    from .notify.console import Console
    return Console(journal=o.get("journal"))


def _runner_script(o: dict[str, Any]) -> Port:
    from .runner.scripted import ScriptedAgent
    return ScriptedAgent(
        script=o.get("script", {}),
        default_value=o.get("fallback", {
            "status": "NO_PROGRESS", "summary": "no outcome declared"}),
        leave_trace=bool(o.get("leave_trace", True)))


register(Capability.TASKS, "filesystem", _tasks_filesystem)
register(Capability.TASKS, "jira", _tasks_jira)
register(Capability.REPOSITORY, "git-local", _repos_git_local)
register(Capability.REPOSITORY, "github", _repos_github)
register(Capability.REPOSITORY, "github-write", _repos_github_write)
register(Capability.CICD, "github-checks", _cicd_github)
register(Capability.WORKSPACE, "directory", _workspace_directory)
register(Capability.WORKSPACE, "worktree", _workspace_worktree)
register(Capability.WORKSPACE, "clone", _workspace_clone)
register(Capability.NOTIFICATION, "console", _notify_console)
register(Capability.RUNNER, "script", _runner_script)
register(Capability.RUNNER, "headless-agent", _agent_headless)
register(Capability.RUNNER, "claude-code", _agent_claude_code)
register(Capability.RUNNER, "codex-cli", _agent_codex_cli)
register(Capability.RUNNER, "deterministic-agent", _agent_deterministic)
