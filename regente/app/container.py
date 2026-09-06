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
from ..core.model import Project, Workspace
from ..core.policy import PolicyEngine
from ..core.risk import RiskEngine
from ..engine.gate import Gate
from ..engine.orchestrator import Orchestrator
from ..engine.store_sqlite import SqliteStore
from ..ports import Capability
from ..ports.support import NotificationProvider
from ..ports.tasks import TaskProvider
from ..ports.workspace import AgentRunner, WorkspaceProvider
from .config import Config, carrega_policies


def _id_estavel(prefixo: str, *partes: str) -> str:
    """Id deterministico a partir do nome.

    Reabrir o mesmo workspace precisa devolver o mesmo id, senao cada `regente
    tick` cria um workspace novo e o estado anterior fica orfao no banco.
    """
    import hashlib
    marca = hashlib.sha1("/".join(partes).encode("utf-8")).hexdigest()[:12]
    return f"{prefixo}_{marca}"


@dataclass(slots=True)
class Motor:
    config: Config
    store: SqliteStore
    workspace: Workspace
    orchestrator: Orchestrator
    gate: Gate

    def fecha(self) -> None:
        self.store.fecha()


def monta(cfg: Config) -> Motor:
    cfg.raiz.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(cfg.banco)
    store.migra()

    org_id = _id_estavel(ids.ORG, cfg.organizacao)
    client_id = _id_estavel(ids.CLIENT, cfg.organizacao, cfg.cliente)
    ws = Workspace(
        id=_id_estavel(ids.WORKSPACE, cfg.organizacao, cfg.cliente, cfg.workspace),
        client_id=client_id, nome=cfg.workspace,
        autonomia_maxima=cfg.autonomia, raiz=str(cfg.raiz))
    store.salva_workspace(ws)

    projetos = cfg.projetos or ()
    for pr in projetos:
        store.salva_project(Project(
            id=_id_estavel(ids.PROJECT, ws.id, pr.nome), workspace_id=ws.id,
            nome=pr.nome, ambiente_padrao=pr.ambiente_padrao,
            autonomia_maxima=pr.autonomia))
    project_id = (_id_estavel(ids.PROJECT, ws.id, projetos[0].nome)
                  if projetos else _id_estavel(ids.PROJECT, ws.id, "padrao"))
    if not projetos:
        store.salva_project(Project(id=project_id, workspace_id=ws.id, nome="padrao"))

    def cria(cap: Capability, chave: str, extras: dict | None = None):
        conf = cfg.providers[chave]
        return registry.cria(cap, conf.nome, {**conf.opcoes, **(extras or {})})

    tasks: TaskProvider = cria(Capability.TASKS, "tasks")
    areas: WorkspaceProvider = cria(Capability.WORKSPACE, "workspace_provider",
                                    {"raiz": str(cfg.areas)})
    runner: AgentRunner = cria(Capability.RUNNER, "runner")
    notificador: NotificationProvider | None = None
    if "notification" in cfg.providers:
        notificador = cria(Capability.NOTIFICATION, "notification", {"jornal": str(cfg.jornal)})

    policy = PolicyEngine.de_config(carrega_policies(cfg.policies))
    risco = RiskEngine.de_config(list(cfg.fatores_de_risco))
    gate = Gate(store=store, policy=policy, risco=risco)

    orq = Orchestrator(
        store=store, workspace=ws, tasks_provider=tasks, area_provider=areas,
        runner=runner, gate=gate, risco=risco, limites=cfg.limites,
        orcamento=cfg.orcamento, notificador=notificador, project_id=project_id,
        lease_segundos=cfg.lease_segundos)

    return Motor(config=cfg, store=store, workspace=ws, orchestrator=orq, gate=gate)


def diagnostico(cfg: Config) -> list[tuple[str, bool, str]]:
    """Checagens do `regente doctor`. Cada aposta provada, nenhuma suposta."""
    saida: list[tuple[str, bool, str]] = []

    def confere(nome: str, fn) -> None:
        try:
            saida.append((nome, True, fn() or "ok"))
        except Exception as e:
            saida.append((nome, False, f"{type(e).__name__}: {e}"[:200]))

    confere("raiz de estado", lambda: (cfg.raiz.mkdir(parents=True, exist_ok=True), str(cfg.raiz))[1])

    def banco() -> str:
        s = SqliteStore(cfg.banco)
        s.migra()
        s.verifica()
        s.fecha()
        return str(cfg.banco)
    confere("banco", banco)

    for chave, cap in (("tasks", Capability.TASKS),
                       ("workspace_provider", Capability.WORKSPACE),
                       ("runner", Capability.RUNNER),
                       ("notification", Capability.NOTIFICATION)):
        if chave not in cfg.providers:
            continue
        conf = cfg.providers[chave]

        def prova(cap=cap, conf=conf, chave=chave) -> str:
            extras = {"raiz": str(cfg.areas)} if cap is Capability.WORKSPACE else {}
            if cap is Capability.NOTIFICATION:
                extras = {"jornal": str(cfg.jornal)}
            porta = registry.cria(cap, conf.nome, {**conf.opcoes, **extras})
            porta.verifica()
            return conf.nome
        confere(f"provider {chave}", prova)

    confere("policies", lambda: f"{len(carrega_policies(cfg.policies))} regra(s)")
    saida.append(("modo", True, "sombra" if cfg.sombra else "VALENDO"))
    return saida
