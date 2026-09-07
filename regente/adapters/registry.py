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

    Two transports behind one adapter: `http` talks to the real site,
    `snapshot` replays real responses already captured. The adapter is identical
    in both cases -- which is why the contract test exercises the same code that
    runs against the network.
    """
    from .tasks.jira import JiraTasks
    from .tasks.transport import HttpTransport, SnapshotTransport

    observer = o.get("observer")
    mode = o.get("transport", "http")
    if mode == "snapshot":
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
        raise KeyError(f"unknown transport for jira: {mode!r}. Use http or snapshot")

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

    mode, env = _auth({**o, "auth": o.get("auth", "none")})
    return HeadlessAgent(
        command=list(o["command"]),
        auth_mode=mode,
        sandbox=SandboxProfile(
            allowed_tools=tuple(o.get("tools", ())),
            denied_tools=tuple(o.get("denied_tools", ())),
            allow_command_execution=bool(o.get("allow_command_execution", False)),
            allow_network=bool(o.get("allow_network", False)),
            env=env,
            max_cost_usd=float(o.get("max_cost_usd", 2.0))))


def _auth(o: dict[str, Any]) -> tuple[Any, dict[str, str]]:
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
    secrets = o.get("secrets")
    env: dict[str, str] = {}

    # `credentials` maps the variable the agent expects -> the reference the
    # engine resolves. Both sides are the workspace's choice; neither is
    # hard-coded, because hard-coding either one is what made the engine look
    # like it only supported a single vendor's API key.
    for variable, reference in (o.get("credentials") or {}).items():
        if secrets is None:
            raise KeyError(
                f"'{variable}' must be resolved through a SecretProvider and "
                f"none was supplied to this adapter")
        env[str(variable)] = secrets.resolve(str(reference))

    if mode in (AuthMode.RESOLVED_SECRET, AuthMode.GATEWAY) and not env:
        raise KeyError(
            f"auth mode '{mode.value}' needs a `credentials` mapping of "
            f"variable -> secret reference; none was configured")
    return mode, env


def _agent_claude_code(o: dict[str, Any]) -> Port:
    """One headless coding-agent CLI. The vendor's name stops at this factory."""
    from .runner.vendors.claude_code import ClaudeCodeAgent, restricted_profile

    mode, env = _auth(o)
    return ClaudeCodeAgent(
        cli_path=o["cli"],
        model=str(o.get("model", "sonnet")),
        auth_mode=mode,
        sandbox=restricted_profile(
            max_cost_usd=float(o.get("max_cost_usd", 2.0)), env=env),
        extra_args=tuple(o.get("extra_args", ())))


def _agent_codex_cli(o: dict[str, Any]) -> Port:
    """A second headless coding-agent CLI, to prove the swap is a config line.

    Identical shape to the factory above and to nothing above `adapters/`. If
    adding this had required a change in `core/`, `ports/` or `engine/`, the
    abstraction would have failed and the boundary test would say so.
    """
    from .runner.vendors.codex_cli import CodexCliAgent, restricted_profile

    mode, env = _auth(o)
    return CodexCliAgent(
        cli_path=o["cli"],
        model=str(o.get("model", "")),
        auth_mode=mode,
        sandbox=restricted_profile(
            max_cost_usd=float(o.get("max_cost_usd", 2.0)), env=env),
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
register(Capability.SECRETS, "scoped", _scoped_secrets)
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
