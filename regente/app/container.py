# -*- coding: utf-8 -*-
"""Composition root: configuration -> assembled engine.

This is the only module that knows the configuration, the adapter registry and
the Orchestrator at the same time. Everything else receives its dependencies
ready-made -- which is why the Core Engine never needs to know where they came
from.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..adapters import conventions, registry
from ..core import ids
from ..core.model import Event, Project, Workspace
from ..core.policy import PolicyEngine
from ..core.risk import RiskEngine
from ..engine.gate import Gate
from ..engine.target import TargetResolver
from ..engine.orchestrator import Orchestrator
from ..engine.readiness import diagnose as _diagnose_agent
from ..engine import readiness
from ..engine.remote import RemoteDelivery
from ..engine.store_sqlite import SqliteStore
from ..ports import Capability
from ..ports.support import NotificationProvider
from ..core.errors import CapabilityMissing
from ..ports import AdapterError
from ..ports.repository import RepositoryProvider
from ..ports.tasks import TaskProvider
from ..ports.agent import AgentRunner
from ..ports.workspace import WorkspaceProvider
from .config import Config, load_policies


#: Frozen on purpose, and the last pt-BR string in this file. It is the seed for
#: the default project's stable id, so it is baked into every database already
#: written. Translating it would mint a different id and orphan the tasks
#: pointing at the old one. The project's *name* is free to be English; the seed
#: is not.
_DEFAULT_PROJECT_SEED = "padrao"


def _stable_id(prefix: str, *parts: str) -> str:
    """A deterministic id derived from the name.

    Reopening the same workspace has to return the same id; otherwise every
    `regente tick` creates a new workspace and the previous state is orphaned in
    the database.
    """
    import hashlib
    mark = hashlib.sha1("/".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{mark}"


@dataclass(slots=True)
class Engine:
    config: Config
    store: SqliteStore
    workspace: Workspace
    orchestrator: Orchestrator
    gate: Gate
    #: Optional: a workspace may govern tasks without governing code.
    repos: RepositoryProvider | None = None
    areas: object | None = None
    agent: object | None = None
    resolver: TargetResolver | None = None
    policy: PolicyEngine | None = None
    risk: RiskEngine | None = None
    #: Push, pull request and CI observation. `None` when the workspace has no
    #: workspace provider at all -- absent capability, not silent local action.
    delivery: RemoteDelivery | None = None

    def close(self) -> None:
        self.store.close()

    def run_mission(self, execute: bool = False, only: str | None = None):
        """Select one task and, when asked, execute it in isolation.

        Selection always runs; execution is opt-in. That asymmetry is the point:
        seeing what the engine WOULD do must never cost a clone, a branch or a
        test run, so the safe call is also the cheap one.
        """
        from ..engine import mission as mission_mod
        from ..engine.discovery import Investigator
        from ..engine.runner import MissionRunner
        from ..ports.agent import Budget, Permissions

        if self.repos is None:
            raise CapabilityMissing("this workspace has no repository provider")

        items = self.orchestrator.tasks_provider.list_tasks()
        if only:
            items = [t for t in items if t.key == only]
        catalog = self.repos.list_repositories()
        branches = {}
        for r in catalog:
            try:
                branches[r.ref.key] = self.repos.list_branches(r.ref.key)
            except Exception:   # noqa: BLE001 - a provider hiccup is not a target
                branches[r.ref.key] = []

        planner = mission_mod.MissionPlanner(
            repos=self.repos,
            investigator=Investigator(repos=self.repos, resolver=self.resolver),
            policy=self.policy, risk=self.risk,
            autonomy=self.workspace.max_autonomy,
            workspace_name=self.workspace.name, workspace_id=self.workspace.id,
            organization=self.config.organization, client=self.config.client)

        # Sibling map: tasks sharing a parent. Built here because only the
        # engine sees the whole board -- a provider answers about one task at a
        # time and cannot know who its siblings are.
        by_parent: dict[str, list[str]] = {}
        for t in items:
            for link in t.links:
                if link.kind == "parent":
                    by_parent.setdefault(link.key, []).append(t.key)
        siblings = {t.key: [k for k in by_parent.get(p.key, []) if k != t.key]
                    for t in items for p in t.links if p.kind == "parent"}

        selection = planner.select(items, catalog, branches, siblings=siblings,
                                   branch_is_ahead=self._branch_is_ahead)
        runner = MissionRunner(
            store=self.store, workspace_id=self.workspace.id,
            workspace_name=self.workspace.name, repos=self.repos,
            areas=self.areas, agent=self.agent, planner=planner,
            budget=Budget(max_iterations=self.config.budget.max_iterations,
                          max_tool_calls=self.config.budget.max_tool_calls,
                          max_cost_usd=self.config.budget.max_cost_usd,
                          max_seconds=self.config.budget.max_seconds),
            permissions=Permissions(read=True, write_code=True, run_tests=True,
                                    commit=True),
            policy=self.policy, autonomy=self.workspace.max_autonomy,
            organization=self.config.organization, client=self.config.client,
            # The vendor names come from the adapter layer, which is where a
            # CI provider's filenames are allowed to be known.
            authority_paths=conventions.default_authority_paths(),
            instruction_files=conventions.INSTRUCTION_FILES,
            watched_sources=self.config.watched_sources)
        if not execute:
            from ..engine.runner import MissionOutcome
            if selection.mission is None:
                return MissionOutcome(None, None, None, None, refusal=selection.render())
            m = selection.mission
            briefing = mission_mod.briefing_for(
                m, workspace_path=str(self.config.areas / m.task.key),
                agent=self.agent.name, budget=runner.budget,
                permissions=runner.permissions, baseline_command=None,
                timeout_seconds=runner.budget.max_seconds)
            return MissionOutcome(briefing, None, None, None, refusal="")
        return runner.run(selection)

    def _branch_is_ahead(self, repo_key: str, branch: str) -> bool:
        """Does this branch carry commits the base branch does not?

        Answered by the repository provider through its own read path, so the
        check works the same for a local clone and for a hosted remote.
        """
        info = self.repos.get_repository(repo_key)
        base = info.base_branch
        items = {b.name: b.sha for b in self.repos.list_branches(repo_key)}
        head, base_sha = items.get(branch), items.get(base)
        if not head or not base_sha:
            # Missing either end means the comparison cannot be made. Saying
            # "not ahead" here would turn ignorance into permission.
            raise AdapterError(f"cannot compare '{branch}' with '{base}' in {repo_key}")
        return head != base_sha


def build(cfg: Config) -> Engine:
    cfg.root.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(cfg.database)
    store.migrate()

    org_id = _stable_id(ids.ORG, cfg.organization)
    client_id = _stable_id(ids.CLIENT, cfg.organization, cfg.client)
    ws = Workspace(
        id=_stable_id(ids.WORKSPACE, cfg.organization, cfg.client, cfg.workspace),
        client_id=client_id, name=cfg.workspace,
        max_autonomy=cfg.autonomy, root=str(cfg.root))
    store.save_workspace(ws)

    projects = cfg.projects or ()
    for pr in projects:
        store.save_project(Project(
            id=_stable_id(ids.PROJECT, ws.id, pr.name), workspace_id=ws.id,
            name=pr.name, default_environment=pr.default_environment,
            max_autonomy=pr.autonomy))
    project_id = (_stable_id(ids.PROJECT, ws.id, projects[0].name)
                  if projects else _stable_id(ids.PROJECT, ws.id, _DEFAULT_PROJECT_SEED))
    if not projects:
        store.save_project(Project(id=project_id, workspace_id=ws.id, name="default"))

    # Secrets are scoped to the workspace BEFORE any adapter exists: no adapter
    # receives a resolver that reaches another client.
    secrets = registry.create(Capability.SECRETS, "scoped",
                             {"allowed": cfg.secrets, "workspace": ws.name})

    # Observer: every call to an external provider becomes an event, with
    # tenancy. The adapter does not know the Store -- it announces, and the one
    # listening is the engine.
    def observe(call) -> None:
        store.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id=ws.id, kind="provider_call",
            actor=cfg.providers["tasks"].name,
            summary=(f"{call.operation} {call.path} "
                    f"{'ok' if call.success else 'FALHOU'} {call.duration_ms}ms"),
            data={"organization": cfg.organization, "client": cfg.client,
                   "provider": cfg.providers["tasks"].name,
                   "operation": call.operation, "path": call.path,
                   "duration_ms": call.duration_ms, "success": call.success,
                   "status": call.status, "attempts": call.attempts,
                   "rate_limited": call.rate_limited,
                   "request_id": call.request_id, "error": call.error}))

    def create(cap: Capability, key: str, extras: dict | None = None):
        conf = cfg.providers[key]
        return registry.create(cap, conf.name, {**conf.options, **(extras or {})})

    tasks: TaskProvider = create(Capability.TASKS, "tasks",
                               {"secrets": secrets, "observer": observe})
    repos: RepositoryProvider | None = None
    areas: object | None = None
    agent: object | None = None
    if "repository" in cfg.providers:
        repos = create(Capability.REPOSITORY, "repository",
                     {"secrets": secrets, "observer": observe})
    areas: WorkspaceProvider = create(Capability.WORKSPACE, "workspace_provider",
                                    {"root": str(cfg.areas)})
    runner: AgentRunner = create(Capability.RUNNER, "runner")
    notifier: NotificationProvider | None = None
    if "notification" in cfg.providers:
        notifier = create(Capability.NOTIFICATION, "notification", {"journal": str(cfg.journal)})

    # Writing to the hosted provider is a SEPARATE adapter from reading it.
    # Not configured means not possible: there is no flag that turns the read
    # adapter into a write one.
    repos_write = None
    if "repository_write" in cfg.providers:
        repos_write = create(Capability.REPOSITORY, "repository_write",
                             {"secrets": secrets, "observer": observe})
    cicd = None
    if "cicd" in cfg.providers:
        cicd = create(Capability.CICD, "cicd",
                      {"secrets": secrets, "observer": observe})

    policy = PolicyEngine.from_config(load_policies(cfg.policies))
    risk = RiskEngine.from_config(list(cfg.risk_factors))
    gate = Gate(store=store, policy=policy, risk=risk)

    orch = Orchestrator(
        store=store, workspace=ws, tasks_provider=tasks, area_provider=areas,
        runner=runner, gate=gate, risk=risk, limits=cfg.limits,
        budget=cfg.budget, notifier=notifier, project_id=project_id,
        lease_seconds=cfg.lease_seconds)

    resolver = TargetResolver(
        by_label=dict(cfg.targets.get("by_label") or {}),
        by_project=dict(cfg.targets.get("by_project") or {}),
        by_task=dict(cfg.targets.get("by_task") or {}))

    delivery = RemoteDelivery(
        store=store, areas=areas, repos_write=repos_write, cicd=cicd,
        policy=policy, autonomy=cfg.autonomy,
        environment=(projects[0].default_environment if projects else "staging"))

    return Engine(config=cfg, store=store, workspace=ws, orchestrator=orch, gate=gate,
                  repos=repos, resolver=resolver, policy=policy, risk=risk,
                  areas=areas, agent=runner, delivery=delivery)


def diagnose(cfg: Config) -> list[tuple[str, bool, str]]:
    """The `regente doctor` checks. Every bet proved, none assumed."""
    output: list[tuple[str, bool, str]] = []

    def expect_prefix(name: str, fn) -> None:
        try:
            output.append((name, True, fn() or "ok"))
        except Exception as e:
            output.append((name, False, f"{type(e).__name__}: {e}"[:200]))

    expect_prefix("state root", lambda: (cfg.root.mkdir(parents=True, exist_ok=True), str(cfg.root))[1])

    def database() -> str:
        s = SqliteStore(cfg.database)
        s.migrate()
        s.verify()
        s.close()
        return str(cfg.database)
    expect_prefix("database", database)

    for key, cap in (("tasks", Capability.TASKS),
                       ("repository", Capability.REPOSITORY),
                       ("workspace_provider", Capability.WORKSPACE),
                       ("runner", Capability.RUNNER),
                       ("notification", Capability.NOTIFICATION)):
        if key not in cfg.providers:
            continue
        conf = cfg.providers[key]

        def proof(cap=cap, conf=conf, key=key) -> str:
            extras: dict = {}
            if cap is Capability.WORKSPACE:
                extras = {"root": str(cfg.areas)}
            elif cap is Capability.NOTIFICATION:
                extras = {"journal": str(cfg.journal)}
            elif cap in (Capability.TASKS, Capability.REPOSITORY,
                         Capability.RUNNER):
                extras = {"secrets": registry.create(
                    Capability.SECRETS, "scoped",
                    {"allowed": cfg.secrets, "workspace": cfg.workspace})}
            port = registry.create(cap, conf.name, {**conf.options, **extras})
            port.verify()
            return conf.name
        expect_prefix(f"provider {key}", proof)

    expect_prefix("policies", lambda: f"{len(load_policies(cfg.policies))} rule(s)")

    # Six axes, one per line. Reporting "variable X is missing" would be the
    # wrong advice for anyone who authenticates the agent some other way -- and
    # most clients authenticate some other way.
    if "runner" in cfg.providers:
        def agent_readiness() -> str:
            conf = cfg.providers["runner"]
            agent = registry.create(Capability.RUNNER, conf.name, {
                **conf.options,
                "secrets": registry.create(
                    Capability.SECRETS, "scoped",
                    {"allowed": cfg.secrets, "workspace": cfg.workspace})})
            state = readiness.diagnose(
                agent, policy=PolicyEngine.from_config(load_policies(cfg.policies)),
                autonomy=cfg.autonomy, organization=cfg.organization,
                client=cfg.client, workspace=cfg.workspace,
                ceiling_usd=cfg.budget.max_cost_usd,
                max_dispatches=cfg.limits.max_dispatches_per_day)
            if state.ready:
                return f"READY ({state.auth_mode.value})"
            raise AdapterError(
                f"{state.readiness.value} -- {state.blocking_reason()}")
        expect_prefix("agent readiness", agent_readiness)

    output.append(("mode", True, "shadow" if cfg.shadow else "LIVE"))
    return output
