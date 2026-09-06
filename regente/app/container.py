# -*- coding: utf-8 -*-
"""Raiz de composicao: configuracao -> motor montado.

Este e o unico modulo que conhece ao mesmo tempo a configuracao, o registro de
adapters e o Orchestrator. Todo o resto recebe suas dependencias prontas -- e por
isso o Core Engine nunca precisa saber de onde elas vieram.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..adapters import registry
from ..core import ids
from ..core.model import Event, Project, Workspace
from ..core.policy import PolicyEngine
from ..core.risk import RiskEngine
from ..engine.gate import Gate
from ..engine.target import TargetResolver
from ..engine.orchestrator import Orchestrator
from ..engine.store_sqlite import SqliteStore
from ..ports import Capability
from ..ports.support import NotificationProvider
from ..ports.repository import RepositoryProvider
from ..ports.tasks import TaskProvider
from ..ports.workspace import AgentRunner, WorkspaceProvider
from .config import Config, load_policies


def _stable_id(prefixo: str, *partes: str) -> str:
    """Id deterministico a partir do nome.

    Reabrir o mesmo workspace precisa devolver o mesmo id, senao cada `regente
    tick` cria um workspace novo e o estado anterior fica orfao no banco.
    """
    import hashlib
    mark = hashlib.sha1("/".join(partes).encode("utf-8")).hexdigest()[:12]
    return f"{prefixo}_{mark}"


@dataclass(slots=True)
class Engine:
    config: Config
    store: SqliteStore
    workspace: Workspace
    orchestrator: Orchestrator
    gate: Gate
    #: Opcional: um workspace pode governar tasks sem governar codigo.
    repos: RepositoryProvider | None = None
    resolvedor: TargetResolver | None = None
    policy: PolicyEngine | None = None
    risk: RiskEngine | None = None

    def close(self) -> None:
        self.store.close()


def build(cfg: Config) -> Engine:
    cfg.root.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(cfg.banco)
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
                  if projects else _stable_id(ids.PROJECT, ws.id, "padrao"))
    if not projects:
        store.save_project(Project(id=project_id, workspace_id=ws.id, name="padrao"))

    # Segredos sao escopados ao workspace ANTES de qualquer adapter existir:
    # nenhum adapter recebe um resolvedor que alcance outro cliente.
    secrets = registry.create(Capability.SECRETS, "escopado",
                             {"allowed": cfg.secrets, "workspace": ws.name})

    # Observador: toda call a provedor externo vira evento, com tenancy.
    # O adapter nao conhece o Store -- ele avisa, e quem escuta e o motor.
    def observe(call) -> None:
        store.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id=ws.id, kind="chamada_provedor",
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
    if "repository" in cfg.providers:
        repos = create(Capability.REPOSITORY, "repository",
                     {"secrets": secrets, "observer": observe})
    areas: WorkspaceProvider = create(Capability.WORKSPACE, "workspace_provider",
                                    {"root": str(cfg.areas)})
    runner: AgentRunner = create(Capability.RUNNER, "runner")
    notificador: NotificationProvider | None = None
    if "notification" in cfg.providers:
        notificador = create(Capability.NOTIFICATION, "notification", {"journal": str(cfg.journal)})

    policy = PolicyEngine.from_config(load_policies(cfg.policies))
    risk = RiskEngine.from_config(list(cfg.risk_factors))
    gate = Gate(store=store, policy=policy, risk=risk)

    orq = Orchestrator(
        store=store, workspace=ws, tasks_provider=tasks, area_provider=areas,
        runner=runner, gate=gate, risk=risk, limits=cfg.limits,
        budget=cfg.budget, notificador=notificador, project_id=project_id,
        lease_seconds=cfg.lease_seconds)

    resolvedor = TargetResolver(
        by_label=dict(cfg.targets.get("por_rotulo") or {}),
        by_project=dict(cfg.targets.get("por_projeto") or {}),
        by_task=dict(cfg.targets.get("por_task") or {}))

    return Engine(config=cfg, store=store, workspace=ws, orchestrator=orq, gate=gate,
                 repos=repos, resolvedor=resolvedor, policy=policy, risk=risk)


def diagnose(cfg: Config) -> list[tuple[str, bool, str]]:
    """Checagens do `regente doctor`. Cada aposta provada, nenhuma suposta."""
    output: list[tuple[str, bool, str]] = []

    def expect_prefix(name: str, fn) -> None:
        try:
            output.append((name, True, fn() or "ok"))
        except Exception as e:
            output.append((name, False, f"{type(e).__name__}: {e}"[:200]))

    expect_prefix("raiz de estado", lambda: (cfg.root.mkdir(parents=True, exist_ok=True), str(cfg.root))[1])

    def banco() -> str:
        s = SqliteStore(cfg.banco)
        s.migrate()
        s.verify()
        s.close()
        return str(cfg.banco)
    expect_prefix("banco", banco)

    for key, cap in (("tasks", Capability.TASKS),
                       ("repository", Capability.REPOSITORY),
                       ("workspace_provider", Capability.WORKSPACE),
                       ("runner", Capability.RUNNER),
                       ("notification", Capability.NOTIFICATION)):
        if key not in cfg.providers:
            continue
        conf = cfg.providers[key]

        def prova(cap=cap, conf=conf, key=key) -> str:
            extras: dict = {}
            if cap is Capability.WORKSPACE:
                extras = {"root": str(cfg.areas)}
            elif cap is Capability.NOTIFICATION:
                extras = {"journal": str(cfg.journal)}
            elif cap in (Capability.TASKS, Capability.REPOSITORY):
                extras = {"secrets": registry.create(
                    Capability.SECRETS, "escopado",
                    {"allowed": cfg.secrets, "workspace": cfg.workspace})}
            porta = registry.create(cap, conf.name, {**conf.options, **extras})
            porta.verify()
            return conf.name
        expect_prefix(f"provider {key}", prova)

    expect_prefix("policies", lambda: f"{len(load_policies(cfg.policies))} regra(s)")
    output.append(("modo", True, "sombra" if cfg.shadow else "VALENDO"))
    return output
