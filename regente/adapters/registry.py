# -*- coding: utf-8 -*-
"""Adapter registry: name in the configuration -> factory.

This is the **only** module in the engine that imports adapters, and it sits
outside the Core and the Engine on purpose. Adding a new provider means adding an
entry here; if it ever becomes necessary to touch `core/` or `engine/` to do
that, the abstraction has failed -- and the boundary test says so.

The import is late (inside the factory) so that an adapter with a heavy
dependency is not imposed on those who do not use it: the engine has to start in
an environment with no cloud SDK at all.
"""

from __future__ import annotations

from typing import Any, Callable

from ..ports import Capability, Port

Factory = Callable[[dict[str, Any]], Port]
_REGISTRY: dict[tuple[Capability, str], Factory] = {}


def register(cap: Capability, name: str, factory: Factory) -> None:
    _REGISTRY[(cap, name)] = factory


def create(cap: Capability, name: str, options: dict[str, Any] | None = None) -> Port:
    key = (cap, name)
    if key not in _REGISTRY:
        available = sorted(n for (c, n) in _REGISTRY if c == cap)
        raise KeyError(
            f"there is no adapter '{name}' for {cap.value}. "
            f"Available: {', '.join(available) or 'none'}")
    return _REGISTRY[key](options or {})


def available(cap: Capability | None = None) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    for (c, n) in sorted(_REGISTRY, key=lambda k: (k[0].value, k[1])):
        if cap is None or c == cap:
            output.setdefault(c.value, []).append(n)
    return output


# ---- built-in factories -------------------------------------------------

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
    """Jira Cloud, read only.

    Two transports through the same adapter: `http` talks to the real site,
    `instantaneo` replays real recorded responses. The adapter is identical in
    both cases -- which is why the contract test exercises the same code that
    runs against the network. The transport names are configuration values and
    stay as they are.
    """
    from .tasks.jira import JiraTasks
    from .tasks.transport import HttpTransport, SnapshotTransport

    observer = o.get("observer")
    mode = o.get("transport", "http")
    if mode == "instantaneo":
        from pathlib import Path
        transport = SnapshotTransport(
            directory=Path(o["snapshots"]), observer=observer)
    elif mode == "http":
        site = o["site"].rstrip("/")
        secrets = o["secrets"]            # SecretProvider, injected by the composition
        ref_user = o.get("user_ref") or "env:JIRA_EMAIL"
        ref_token = o.get("token_ref") or "env:JIRA_API_TOKEN"
        transport = HttpTransport(
            base_url=site,
            credential=lambda: (secrets.resolve(ref_user), secrets.resolve(ref_token)),
            timeout=int(o.get("timeout", 30)),
            max_attempts=int(o.get("max_attempts", 3)),
            observer=observer)
    else:
        raise KeyError(f"unknown transport for jira: {mode!r}. Use http or instantaneo")

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


def _agent_external(o: dict[str, Any]) -> Port:
    from .runner.external import ExternalAgent
    return ExternalAgent(command=list(o["command"]), env=dict(o.get("env", {})))


def _agent_deterministic(o: dict[str, Any]) -> Port:
    from .runner.external import DeterministicAgent
    return DeterministicAgent(script=dict(o.get("script", {})),
                              fallback=dict(o.get("fallback", {})) or None
                              or {"outcome": "NO_PROGRESS",
                                  "summary": "no edit declared for this task"})


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
    return ScriptedRunner(script=o.get("script", {}), default_value=o.get("fallback", {"ok": True, "summary": "no change"}))


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
register(Capability.RUNNER, "external-agent", _agent_external)
register(Capability.RUNNER, "deterministic-agent", _agent_deterministic)
