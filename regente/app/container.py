# -*- coding: utf-8 -*-
"""Raiz de composicao: configuracao -> motor montado.

Este e o unico modulo que conhece ao mesmo tempo a configuracao, o registro de
adapters e o Orchestrator. Todo o resto recebe suas dependencias prontas -- e por
isso o Core Engine nunca precisa saber de onde elas vieram.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..adapters import conventions, registry
from ..core import ids
from ..core.model import Event, Project, Workspace
from ..core.policy import PolicyEngine
from ..core.risk import RiskEngine
from ..engine.gate import Gate
from ..engine.target import TargetResolver
from ..engine.orchestrator import Orchestrator
from ..engine.readiness import diagnose as _diagnose_agent
from ..adapters.identity.os_account import OsAccountIdentity
from ..engine import readiness
from ..engine.access import AccessService
from ..engine.credentials import CredentialService
from ..engine.decision import DecisionService
from ..engine.remote import RemoteDelivery
from ..core.resource import Kind
from ..engine.resources import SomenteSelecionados
from ..engine.store_sqlite import SqliteStore
from ..ports import Capability
from ..ports.support import NotificationProvider
from ..core.errors import CapabilityMissing
from ..ports import AdapterError
from ..ports.repository import RepositoryProvider
from ..ports.tasks import TaskProvider
from ..ports.agent import AgentRunner
from ..ports.workspace import WorkspaceProvider
from .config import AdapterConf, Config, load_policies


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
    areas: object | None = None
    agent: object | None = None
    resolver: TargetResolver | None = None
    policy: PolicyEngine | None = None
    risk: RiskEngine | None = None
    #: Push, pull request and CI observation. `None` when the workspace has no
    #: workspace provider at all -- absent capability, not silent local action.
    delivery: RemoteDelivery | None = None
    #: `(provedor, ator) -> porta de descoberta | None`. Uma FABRICA: a porta
    #: e construida com o broker de quem pediu, e nao com um ator congelado na
    #: composicao.
    descobridores: object = None

    def close(self) -> None:
        self.store.close()

    # ---- a unica escrita humana --------------------------------------
    def decisions(self) -> DecisionService:
        """O caminho de decisao. UM so, para terminal e navegador.

        Montado aqui e nao em cada superficie: duas construcoes divergem, e a
        que diverge e sempre a que esquece de passar a policy.
        """
        return DecisionService(
            store=self.store, policy=self.policy or PolicyEngine.from_config([]),
            organization=self.config.organization, client=self.config.client,
            workspace_name=self.workspace.name,
            environment=(self.config.projects[0].default_environment
                         if self.config.projects else "staging"))

    def operations(self) -> "OperationService":
        """Ligar, pausar e parar. UM caminho, para terminal e navegador.

        Montado aqui pelo mesmo motivo dos outros: duas construcoes divergem, e
        a que diverge e a que esquece de passar a policy.
        """
        from ..engine.operation import OperationService

        return OperationService(
            store=self.store, policy=self.policy or PolicyEngine.from_config([]),
            organization=self.config.organization, client=self.config.client,
            workspace_name=self.workspace.name,
            environment=(self.config.projects[0].default_environment
                         if self.config.projects else "staging"))

    def access(self) -> AccessService:
        """Administracao de acesso. UMA, para terminal e navegador."""
        return AccessService(
            store=self.store, policy=self.policy or PolicyEngine.from_config([]),
            organization=self.config.organization, client=self.config.client,
            workspace_name=self.workspace.name,
            environment=(self.config.projects[0].default_environment
                         if self.config.projects else "staging"))

    def resources(self) -> "ResourceService":
        """Descobrir e escolher. UM caminho, para terminal e navegador.

        Montado aqui pelo mesmo motivo dos irmaos: duas construcoes divergem, e
        a que diverge e sempre a que esquece de passar a policy.

        `discovery_for` fecha sobre o mapa ja composto. O servico continua sem
        saber o que e um GitHub -- ele pede "a porta deste provedor" e recebe
        `None` quando aquele provedor nao descobre nada.
        """
        from ..engine.resources import ResourceService

        return ResourceService(
            store=self.store, policy=self.policy or PolicyEngine.from_config([]),
            organization=self.config.organization, client=self.config.client,
            workspace_name=self.workspace.name,
            environment=(self.config.projects[0].default_environment
                         if self.config.projects else "staging"),
            discovery_for=self.descobridores)

    def connections(self) -> "ConnectService":
        """Conectar um servico. UM caminho, para terminal e navegador.

        Recebe os DOIS servicos governados prontos, e nao o store: assim nao
        existe forma de conectar gravando por fora -- se `SettingsService` ou
        `CredentialService` recusar, conectar recusa.
        """
        from ..engine.connect import ConnectService

        return ConnectService(
            settings=self.settings(), credentials=self.credentials(),
            access=self.access(), conectores=registry.conectores(),
            providers_do_arquivo=lambda: providers_efetivos(self.config))

    def settings(self) -> "SettingsService":
        """A configuracao do workspace. Montada aqui, como as irmas."""
        from ..engine.settings import SettingsService

        return SettingsService(
            store=self.store, policy=self.policy or PolicyEngine.from_config([]),
            organization=self.config.organization, client=self.config.client,
            workspace_name=self.workspace.name,
            environment=(self.config.projects[0].default_environment
                         if self.config.projects else "staging"))

    def credentials(self) -> CredentialService:
        """O caminho governado ate um segredo. UM, para terminal e navegador.

        A fonte de segredo entra com `allow_any=True` de proposito: a lista de
        referencias do YAML era a autoridade ANTIGA, e mante-la aqui criaria
        duas -- uma no arquivo, outra na credencial registrada. A barreira que
        vale e a de cima, com autor, validade e revogacao.
        """
        from ..adapters.secrets import ScopedSecrets

        return CredentialService(
            store=self.store, policy=self.policy or PolicyEngine.from_config([]),
            secrets=ScopedSecrets(workspace=self.workspace.name, allow_any=True,
                                  helpers=dict(self.config.helpers)),
            organization=self.config.organization, client=self.config.client,
            workspace_name=self.workspace.name,
            environment=(self.config.projects[0].default_environment
                         if self.config.projects else "staging"))

    def engine_principal(self):
        """Quem e o motor quando ele age sozinho.

        Um tick roda de madrugada, sem ninguem olhando. Ate o marco 15 a
        autoridade dele era implicita -- agia por ter sido construido. Agora ele
        e um principal como qualquer outro: identidade, e o que uma concessao
        gravada disser. Se ninguem conceder acesso ao motor, ele nao usa
        credencial nenhuma, e isso e a resposta certa.
        """
        import socket

        from ..adapters.identity.engine_service import EngineServiceIdentity

        provider = EngineServiceIdentity(workspace_id=self.workspace.id,
                                         host=socket.gethostname())
        found = provider.authenticate(None)
        if found is None:
            from ..core.principal import ANONYMOUS
            return ANONYMOUS
        return self.access().authorize(provider.principal(found))

    def terminal_principal(self):
        """Quem esta no terminal: identidade real do sistema, e nada mais.

        Duas etapas, nesta ordem, e nunca fundidas: o provedor diz QUEM e; o
        `AccessService` diz o que essa pessoa PODE, lendo concessoes gravadas.

        Antes, o provedor devolvia as duas coisas -- e quem abria a configuracao
        concedia a si mesmo autoridade de escrita sem deixar registro.
        """
        provider = OsAccountIdentity(reads=frozenset({self.workspace.id}))
        found = provider.authenticate(None)
        if found is None:
            from ..core.principal import ANONYMOUS
            return ANONYMOUS
        return self.access().authorize(provider.principal(found))

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
            watched_sources=self.config.watched_sources,
            task_provider=self.config.providers["tasks"].name,
            delivery=self._delivery_stage())
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

    def _delivery_stage(self):
        """The road out of `TESTING`, when this workspace has one.

        Absent whenever the remote write path is not configured. Returning
        `None` there is deliberate: a workspace that cannot push should say so
        at the moment of delivery, not deliver into a local clone and call it
        the same thing.
        """
        from ..engine.pipeline import DeliveryStage
        if self.delivery is None or self.delivery.repos_write is None:
            return None
        return DeliveryStage(store=self.store, delivery=self.delivery,
                             workspace_id=self.workspace.id)

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


def apply_overlay(cfg: Config, store) -> Config:
    """A configuracao EFETIVA: arquivo, com o que a tela sobrepos.

    Aplicada aqui e em nenhum outro lugar. Se cada superficie aplicasse a
    sobreposicao por conta propria, a tela e o tick leriam configuracoes
    diferentes -- e a que divergisse seria a que roda de madrugada.

    Uma sobreposicao invalida e IGNORADA, com o arquivo valendo no lugar dela.
    Ela ja foi validada quando gravada (`SettingsService.validate`), entao
    chegar aqui quebrada significa que alguem editou o banco por fora -- e nesse
    caso derrubar o motor seria transformar uma linha ruim numa parada total.
    """
    from ..core.settings import effective
    from ..core.selection import rules_from
    from ..ports.tasks import status_map_from

    overlay = store.settings(_stable_id(ids.WORKSPACE, cfg.organization,
                                        cfg.client, cfg.workspace))
    if not overlay.values:
        return cfg

    mudancas: dict = {}

    provedores = effective("providers", None, overlay)
    if provedores.overridden and isinstance(provedores.value, dict):
        try:
            novos = {k: AdapterConf.de(v, f"providers.{k}")
                     for k, v in provedores.value.items()}
            # A sobreposicao ACRESCENTA e substitui por chave; nao apaga o que o
            # arquivo declarou. Trocar o dicionario inteiro faria configurar um
            # provider pela tela remover os outros em silencio.
            mudancas["providers"] = {**cfg.providers, **novos}
        except ValueError:
            pass

    mapa = effective("status_map", None, overlay)
    if mapa.overridden and isinstance(mapa.value, dict):
        try:
            status_map_from(mapa.value)
            base = mudancas.get("providers", cfg.providers)
            tasks = base.get("tasks")
            if tasks is not None:
                mudancas["providers"] = {
                    **base,
                    "tasks": AdapterConf(
                        name=tasks.name,
                        options={**tasks.options, "status_map": mapa.value})}
        except ValueError:
            pass

    regras = effective("selection", None, overlay)
    if regras.overridden and isinstance(regras.value, list):
        try:
            mudancas["selection"] = rules_from(regras.value)
        except ValueError:
            pass

    return replace(cfg, **mudancas) if mudancas else cfg


def build(cfg: Config) -> Engine:
    cfg.root.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(cfg.banco)
    store.migrate()
    # O que a tela configurou vale a partir daqui -- inclusive para o tick.
    cfg = apply_overlay(cfg, store)

    org_id = _stable_id(ids.ORG, cfg.organization)
    client_id = _stable_id(ids.CLIENT, cfg.organization, cfg.client)
    ws = Workspace(
        id=_stable_id(ids.WORKSPACE, cfg.organization, cfg.client, cfg.workspace),
        client_id=client_id, name=cfg.workspace,
        max_autonomy=cfg.autonomy, root=str(cfg.root))
    # Os nomes so existem no arquivo de configuracao. Gravados aqui porque esta
    # e a unica camada que os ve -- sem isto, toda leitura fora do terminal
    # mostra um id opaco a quem precisa saber de quem e o trabalho.
    store.save_client(client_id, cfg.organization, cfg.client)
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

    def broker(key: str):
        """A porta de credencial deste adapter, ja presa a quem age.

        Delega a `_engine_broker`, que e o MESMO caminho que o `Engine` usa
        depois de montado. Duas montagens divergem, e a que diverge e sempre a
        que esquece de ler a concessao -- foi por isso que o sweep de mutacao
        conseguiu conceder autoridade aqui sem nenhum teste perceber.
        """
        return _engine_broker(store, cfg, ws, key, projects)

    def create(cap: Capability, key: str, extras: dict | None = None):
        conf = cfg.providers[key]
        return registry.create(cap, conf.name, {**conf.options, **(extras or {})})

    tasks: TaskProvider = create(Capability.TASKS, "tasks",
                               {"credentials": broker("tasks"), "observer": observe})
    repos: RepositoryProvider | None = None
    areas: object | None = None
    agent: object | None = None
    if "repository" in cfg.providers:
        repos = create(Capability.REPOSITORY, "repository",
                     {"credentials": broker("repository"), "observer": observe})
    # Empurrar ESCREVE no repositorio, entao a credencial e a do repositorio --
    # a MESMA que abre o pull request. Uma credencial propria de "workspace"
    # seria uma segunda autoridade para o mesmo alvo, e as duas divergiriam.
    # Sem `repository_write` configurado nao ha porta, e empurrar e impossivel:
    # ausencia de configuracao nunca vira permissao.
    areas: WorkspaceProvider = create(
        Capability.WORKSPACE, "workspace_provider",
        {"root": str(cfg.areas),
         "credentials": (broker("repository_write")
                         if "repository_write" in cfg.providers else None)})
    runner: AgentRunner = create(Capability.RUNNER, "runner")
    notificador: NotificationProvider | None = None
    if "notification" in cfg.providers:
        notificador = create(Capability.NOTIFICATION, "notification", {"journal": str(cfg.journal)})

    # Writing to the hosted provider is a SEPARATE adapter from reading it.
    # Not configured means not possible: there is no flag that turns the read
    # adapter into a write one.
    repos_write = None
    if "repository_write" in cfg.providers:
        repos_write = create(Capability.REPOSITORY, "repository_write",
                             {"credentials": broker("repository_write"), "observer": observe})
    cicd = None
    if "cicd" in cfg.providers:
        cicd = create(Capability.CICD, "cicd",
                      {"credentials": broker("cicd"), "observer": observe})

    # ---- descoberta: quais provedores sabem dizer o que existe -----------
    #
    # Montado a partir dos MESMOS provedores ja configurados, e nao de uma lista
    # propria: um provedor so aparece na tela de integracoes se este workspace
    # ja o configurou. Uma segunda lista aqui deixaria a tela oferecer conexoes
    # que o motor nao tem como usar.
    #
    # A credencial e a mesma que ja estava registrada -- o que muda e a
    # CAPACIDADE pedida a ela. Uma credencial sem `repo.discover` recusa a
    # descoberta e continua lendo o que o workspace escolheu, que e a resposta
    # certa: ela nunca foi autorizada a varrer a organizacao inteira.
    descobridores = discovery_for(cfg, store, ws, projects, observe=observe)

    # ---- o corte: o motor so alcanca o que o workspace escolheu ----------
    #
    # Envelopado AQUI, na composicao, e nao em cada chamador. Um filtro no
    # chamador teria mais de um chamador, e o que esquecesse seria o furo; aqui
    # a unica forma de alcancar o provedor e passando por este objeto.
    #
    # Um workspace sem nenhuma selecao continua vendo tudo, de proposito: a
    # tabela nasce vazia, e quem migra um banco existente nao pode acordar com o
    # motor parado por uma decisao que ninguem tomou.
    if repos is not None:
        repos = SomenteSelecionados(inner=repos, workspace_id=ws.id, store=store,
                                    role=Kind.CODE)
    # O mesmo corte do outro lado. O envelope entra ANTES do orquestrador, e nao
    # depois: o `Orchestrator` chama `list_tasks()` como sempre chamou, e o que
    # muda e que a resposta ja vem cortada. Filtrar no orquestrador exigiria que
    # ele conhecesse selecao, workspace e tenancy -- e o proximo chamador, que
    # nao e o orquestrador, esqueceria.
    tasks = SomenteSelecionados(inner=tasks, workspace_id=ws.id, store=store,
                                role=Kind.TASK_SOURCE)

    policy = PolicyEngine.from_config(load_policies(cfg.policies))
    risk = RiskEngine.from_config(list(cfg.risk_factors))
    gate = Gate(store=store, policy=policy, risk=risk)

    delivery = RemoteDelivery(
        store=store, areas=areas, repos_write=repos_write, cicd=cicd,
        policy=policy, autonomy=cfg.autonomy,
        environment=(projects[0].default_environment if projects else "staging"))

    orq = Orchestrator(
        store=store, workspace=ws, tasks_provider=tasks, area_provider=areas,
        runner=runner, gate=gate, risk=risk, limits=cfg.limits,
        budget=cfg.budget, notificador=notificador, project_id=project_id,
        lease_seconds=cfg.lease_seconds,
        selection=cfg.selection,
        organization=cfg.organization, client=cfg.client,
        # The tick reads the checks of deliveries already in flight. Given to
        # the orchestrator rather than built inside it: what a workspace can
        # reach is composition's answer, not the engine's.
        delivery=delivery)

    resolver = TargetResolver(
        by_label=dict(cfg.targets.get("by_label") or {}),
        by_project=dict(cfg.targets.get("by_project") or {}),
        by_task=dict(cfg.targets.get("by_task") or {}))

    return Engine(config=cfg, store=store, workspace=ws, orchestrator=orq, gate=gate,
                  repos=repos, resolver=resolver, policy=policy, risk=risk,
                  areas=areas, agent=runner, delivery=delivery,
                  descobridores=descobridores)


def providers_efetivos(cfg: Config, settings=None, workspace_id: str = "") -> dict:
    """`{papel: {name, ...opcoes}}` -- o arquivo, com a tela por cima.

    Normaliza as duas fontes numa forma so. `cfg.providers` guarda objetos de
    configuracao; a sobreposicao guarda dicionarios crus. Quem consome os dois
    sem normalizar acaba escrevendo `conf.name` num caso e `conf["name"]` no
    outro -- e o segundo e sempre o que ninguem testou.
    """
    saida = {nome: dict(conf.options, name=conf.name)
             for nome, conf in cfg.providers.items()}
    if settings is not None and workspace_id:
        sobreposto = settings.overlay(workspace_id).get("providers")
        if isinstance(sobreposto, dict):
            saida.update(sobreposto)
    return saida


def discovery_for(cfg: Config, store, ws, projects=(), observe=None,
                  providers_agora=None):
    """Devolve `(provedor, ator) -> porta de descoberta`, ou `None`.

    Uma FABRICA, e nao um dicionario pronto, por causa do ator. A porta precisa
    do broker vinculado a QUEM PEDIU: uma pessoa clicando "buscar" descobre com
    a autoridade dela, e o tick da madrugada descobre com a do motor. Construir
    as portas uma vez na composicao congelava um ator so -- e foi assim que a
    primeira conexao real recusou, com o motor no lugar da pessoa.

    So entram provedores que ESTE workspace ja configurou. Uma lista propria
    aqui faria a tela oferecer integracoes que o motor nao tem como usar.

    A credencial e a que ja estava registrada; o que muda e a CAPACIDADE pedida
    a ela. Uma credencial sem `repo.discover` recusa a descoberta e continua
    lendo o que o workspace escolheu -- ela nunca foi autorizada a varrer a
    organizacao inteira, e a recusa e a resposta certa.
    """
    agora = providers_agora or (lambda: providers_efetivos(cfg))

    def porta(provider: str, actor=None):
        # Lida A CADA PEDIDO. Um processo de tela fica de pe por horas, e quem
        # conecta um servico nela espera ve-lo funcionando no clique seguinte
        # -- e nao depois de reiniciar.
        for chave, conf in agora().items():
            if chave not in ("tasks", "repository"):
                continue
            nome = str(conf.get("name") or "")
            if nome != provider or not registry.has(Capability.DISCOVERY, nome):
                continue
            opcoes = {k: v for k, v in conf.items() if k != "name"}
            broker = _engine_broker(store, cfg, ws, chave, projects,
                                    principal=actor)
            extras: dict[str, object] = {"credentials": broker,
                                         "observer": observe}
            if chave == "repository":
                # O provider de LEITURA, montado com o mesmo ator. Ele e quem
                # sabe falar com o fornecedor; a descoberta so pede a ele com
                # outra capacidade.
                extras["repos"] = registry.create(
                    Capability.REPOSITORY, nome,
                    {**opcoes, "credentials": broker, "observer": observe})
            return registry.create(Capability.DISCOVERY, nome,
                                   {**opcoes, **extras})
        return None
    return porta


def discovery_trees(providers: dict) -> dict[str, tuple[str, ...]]:
    """`{provedor: arvore}` -- o que a tela precisa para navegar por niveis.

    Recebe os providers EFETIVOS, e nao a configuracao: quem conecta um servico
    pela tela precisa ver a arvore dele no clique seguinte.

    Nao custa credencial nem rede: a arvore e uma propriedade do adapter, e nao
    da conta de ninguem. Por isso ela pode ser lida sem ator.
    """
    arvores: dict[str, tuple[str, ...]] = {}
    for chave, conf in (providers or {}).items():
        if chave not in ("tasks", "repository"):
            continue
        nome = str(conf.get("name") or "")
        if not registry.has(Capability.DISCOVERY, nome):
            continue
        opcoes = {k: v for k, v in conf.items() if k != "name"}
        porta = registry.create(Capability.DISCOVERY, nome,
                                {**opcoes, "credentials": None, "repos": None})
        arvores[nome] = porta.discovers()
    return arvores


def _engine_broker(store, cfg, ws, key: str, projects, principal=None):
    """A porta para um provider, vinculada a QUEM AGE. UMA montagem, para todos.

    Sem `principal`, quem age e o motor: identidade propria de servico, e o que
    uma concessao gravada disser. A autoridade e LIDA do registro -- nunca
    concedida aqui, por mais pratico que fosse.

    COM `principal`, quem age e a pessoa que pediu. E o caso da tela e do
    terminal: descobrir repositorios usa a autoridade de quem clicou, e nao a do
    motor. Confundir os dois barra quem pode e, pior, deixa passar quem nao
    pode -- usando o motor como emprestimo de autoridade.
    """
    from ..adapters.identity.engine_service import EngineServiceIdentity
    from ..adapters.secrets import ScopedSecrets
    from ..engine.access import AccessService
    from ..engine.credentials import CredentialService

    regras = PolicyEngine.from_config(load_policies(cfg.policies))
    ambiente = (projects[0].default_environment if projects else "staging")

    if principal is None:
        provider = EngineServiceIdentity(workspace_id=ws.id)
        principal = provider.principal(provider.authenticate(None))

    servico = CredentialService(
        store=store, policy=regras,
        secrets=ScopedSecrets(workspace=cfg.workspace, allow_any=True,
                              helpers=dict(cfg.helpers)),
        organization=cfg.organization, client=cfg.client,
        workspace_name=cfg.workspace, environment=ambiente)
    acesso = AccessService(store=store, policy=regras,
                           organization=cfg.organization, client=cfg.client,
                           workspace_name=cfg.workspace)
    return servico.broker(acesso.authorize(principal), ws.id, key)


class _SemCredencial:
    """A porta que o diagnostico recebe: existe e nao entrega nada.

    `doctor` precisa construir adapters para provar que eles sobem, e construir
    nao pode mais significar ter credencial. Devolver `None` faria o adapter
    quebrar de um jeito que parece defeito; esta porta responde a mesma coisa
    que a real responderia a quem nao foi autorizado.
    """

    def material(self, use) -> str:
        from ..ports.support import CredentialDenied

        raise CredentialDenied(
            "NOT_FOUND",
            "o diagnostico nao resolve credencial; use `regente credentials "
            "testar` para provar uma de verdade")

    def allows(self, use) -> bool:
        return False


def acoes_que_o_motor_emite() -> tuple[str, ...]:
    """Toda acao que ESTA versao consegue submeter a policy.

    Lida dos enums, e nao de uma lista escrita a mao: uma lista a mao envelhece
    no primeiro marco em que alguem esquecer de atualiza-la, e o sintoma seria
    justamente o silencio que esta checagem existe para acabar.
    """
    from ..core.access import Ability
    from ..core.credential import Use

    acoes = {a.value for a in Ability} | {u.value for u in Use}
    # As acoes de recurso sao formadas com o nome do provedor na frente. O
    # curinga da policy enviada cobre todas; um provedor concreto serve de
    # amostra para a checagem.
    acoes.add("github.resource.discover")
    return tuple(sorted(acoes))


def _policy_em_dia(cfg: Config) -> tuple[str, bool, str]:
    """Esta policy conhece tudo o que esta versao sabe fazer?

    Uma acao sem NENHUMA regra que a mencione cai no DENY por omissao. Quem
    esbarra nisso ve "nenhuma regra permite X" numa tela, sem nada dizendo que
    o arquivo do workspace e mais antigo que o programa.
    """
    from ..core.policy import Action, AutonomyLevel, Effect, PolicyContext

    try:
        regras = PolicyEngine.from_config(load_policies(cfg.policies))
    except Exception as e:                                # noqa: BLE001
        return ("policy em dia", False, f"{type(e).__name__}: {e}"[:200])

    mudas = []
    for acao in acoes_que_o_motor_emite():
        decisao = regras.decide(PolicyContext(
            action=Action(kind=acao, resource="workspace", environment="staging"),
            organization=cfg.organization, client=cfg.client,
            workspace=cfg.workspace, agent="doctor",
            autonomy=AutonomyLevel.L4))
        # Sem regra que CASE -- e nao "negada". Uma policy que nega
        # explicitamente esta em dia: alguem decidiu aquilo.
        if not decisao.matched:
            mudas.append(acao)

    if not mudas:
        return ("policy em dia", True,
                f"conhece as {len(acoes_que_o_motor_emite())} acoes desta versao")
    return ("policy em dia", False,
            f"{cfg.policies} nao conhece {len(mudas)} acao(oes) desta versao: "
            + ", ".join(mudas[:6])
            + (f" (+{len(mudas) - 6})" if len(mudas) > 6 else "")
            + ". Ponha em dia com: regente atualizar")


def _concessoes_em_dia(cfg: Config) -> tuple[str, bool, str]:
    """As concessoes deste workspace conhecem o que os papeis valem hoje?

    Uma concessao viva feita antes de um papel ganhar capacidade continua
    valendo o que valia -- e a pessoa descobre isso quando um botao recusa com
    "recurso nao encontrado neste escopo", que nao explica nada.
    """
    from ..core import ids as _ids
    from ..engine.access import AccessService

    try:
        store = SqliteStore(cfg.banco)
        store.migrate()
        ws = _stable_id(_ids.WORKSPACE, cfg.organization, cfg.client,
                        cfg.workspace)
        servico = AccessService(
            store=store, policy=PolicyEngine.from_config([]),
            organization=cfg.organization, client=cfg.client,
            workspace_name=cfg.workspace)
        defasadas = servico.defasadas(ws)
        sem_papel = [g for g in store.grants(ws) if not g.role]
        store.close()
    except Exception as e:                                # noqa: BLE001
        return ("concessoes em dia", False, f"{type(e).__name__}: {e}"[:200])

    if not defasadas and not sem_papel:
        return ("concessoes em dia", True, "os papeis valem o que valem hoje")

    partes = []
    for g in defasadas:
        ganha = ", ".join(sorted(a.value for a in g.faltando))
        partes.append(f"{g.principal.key} ({g.role}) ganharia {ganha}")
    for g in sem_papel:
        partes.append(f"{g.principal.key} nao registrou papel")
    return ("concessoes em dia", False,
            "; ".join(partes[:3])
            + (f" (+{len(partes) - 3})" if len(partes) > 3 else "")
            + ". Ponha em dia com: regente atualizar")


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
            elif cap in (Capability.TASKS, Capability.REPOSITORY,
                         Capability.RUNNER):
                # O diagnostico constroi o adapter para provar que ele SOBE.
                # Ele nao recebe porta de credencial: um `doctor` que resolvesse
                # segredo faria da checagem de saude o caminho mais curto para
                # extrair material -- e ninguem estranharia, porque `doctor` e
                # o comando que todo mundo roda primeiro.
                extras = {"credentials": _SemCredencial()}
            port = registry.create(cap, conf.name, {**conf.options, **extras})
            try:
                port.verify()
            except Exception as e:                       # noqa: BLE001
                # Um adapter que so falha por NAO TER CREDENCIAL aqui nao esta
                # quebrado: o diagnostico deliberadamente nao resolve segredo.
                # Reportar isso como falha faria `doctor` acusar um provedor
                # saudavel, e ensinaria a ignorar `doctor`.
                if "CredentialDenied" not in f"{type(e).__name__}: {e}":
                    raise
                return (f"{conf.name} (sobe; credencial nao verificada aqui -- "
                        f"use `regente credentials testar`)")
            return conf.name
        expect_prefix(f"provider {key}", prova)

    expect_prefix("policies", lambda: f"{len(load_policies(cfg.policies))} regra(s)")
    output.append(_policy_em_dia(cfg))
    output.append(_concessoes_em_dia(cfg))

    # Seis eixos, um por linha. Reportar "falta a variavel X" seria conselho
    # errado para quem autentica o agente de outra forma -- e a maioria dos
    # clientes autentica de outra forma.
    if "runner" in cfg.providers:
        def prontidao() -> str:
            conf = cfg.providers["runner"]
            agent = registry.create(Capability.RUNNER, conf.name, {
                **conf.options, "credentials": _SemCredencial()})
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
        expect_prefix("prontidao do agente", prontidao)

    output.append(("modo", True, "sombra" if cfg.shadow else "VALENDO"))
    return output
