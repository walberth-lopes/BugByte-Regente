# -*- coding: utf-8 -*-
"""Superficie de linha de comando.

A tela responde quatro perguntas, nessa ordem de importancia: o que precisa de
mim, o que esta acontecendo, o que terminou, e ha algum problema. Complexidade
interna -- lease, run, grafo, policy -- so aparece quando alguem pede.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .app import container
from .app.config import Config, load
from .core.states import TaskState
from .app.container import _stable_id
from .core import ids
from .engine import chain, escalation, shadow
from .engine.store_sqlite import SqliteStore

DEFAULT_CONFIG_FILE = "regente.yaml"


def _force_utf8() -> None:
    # Sem isto, um titulo com acento derruba o comando no console do Windows.
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _load_config(args) -> Config:
    return load(args.config)


# ---- comandos ------------------------------------------------------------

def cmd_init(args) -> int:
    destination = Path(args.config)
    if destination.exists() and not args.force:
        print(f"{destination} ja existe. Use --force para sobrescrever.")
        return 1
    model = Path(__file__).parent / "resources" / "regente.yaml.example"
    destination.write_text(model.read_text(encoding="utf-8"), encoding="utf-8")
    tasks = destination.parent / "tasks"
    tasks.mkdir(exist_ok=True)
    print(f"criado {destination}")
    print(f"criado {tasks}/ -- descreva trabalho em YAML aqui")
    print("proximo: regente doctor")
    return 0


def cmd_doctor(args) -> int:
    cfg = _load_config(args)
    problemas = 0
    for name, ok, detalhe in container.diagnose(cfg):
        mark = "ok  " if ok else "FALHA"
        print(f"  {mark}  {name:<28} {detalhe}")
        problemas += 0 if ok else 1
    print()
    print("tudo pronto" if not problemas else f"{problemas} problema(s) -- o motor nao vai rodar assim")
    return 0 if not problemas else 2


def cmd_tick(args) -> int:
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        rel = motor.orchestrator.tick()
        print(rel.summary())
        if rel.dispatched:
            print("  despachadas:", ", ".join(rel.dispatched))
        if rel.completed:
            print("  concluidas: ", ", ".join(rel.completed))
        if rel.recovered:
            print("  recuperadas:", ", ".join(rel.recovered))
        if rel.cycles:
            print("  em ciclo:   ", ", ".join(rel.cycles))
        if args.verbose and rel.deferred:
            for key, reason in rel.deferred:
                print(f"  adiada {key}: {reason}")
        for e in rel.errors:
            print("  error:", e)
        if rel.escalated:
            print()
            print(f"  {len(rel.escalated)} precisam de voce: regente needs-me")
        return 0
    finally:
        motor.close()


def cmd_status(args) -> int:
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        store, ws = motor.store, motor.workspace
        tasks = store.tasks(ws.id)
        by_state: dict[str, int] = {}
        for t in tasks:
            by_state[t.state.value] = by_state.get(t.state.value, 0) + 1

        rodando = [t for t in tasks if t.state.value in
                   {"ASSIGNED", "IMPLEMENTING", "TESTING", "CI_RUNNING", "AI_REVIEW",
                    "MERGING", "DEPLOYING"}]
        open_items = store.open_approvals(ws.id)
        blocked = [t for t in tasks if t.state in (TaskState.BLOCKED, TaskState.FAILED)]
        ready = [t for t in tasks if t.state is TaskState.DONE]

        print(f"REGENTE -- {ws.name}  [{'sombra' if cfg.shadow else 'VALENDO'}]")
        print()
        print(f"  Rodando     {len(rodando)}")
        print(f"  Precisa de voce  {len(open_items)}" + ("   <-- prioridade" if open_items else ""))
        print(f"  Bloqueadas  {len(blocked)}")
        print(f"  Concluidas  {len(ready)}")

        if rodando:
            print()
            print("  TRABALHO ATIVO")
            for t in rodando:
                print(f"    {t.key:<16} {t.state.value}")
        if open_items:
            print()
            print("  PRECISA DE VOCE")
            for a in open_items:
                t = store.task(a.task_id)
                print(f"    [{a.risk.name}] {t.key:<16} {a.what_happened[:60]}")
            print()
            print("    regente needs-me   para ver e decidir")
        if args.verbose:
            print()
            print("  POR ESTADO")
            for state, n in sorted(by_state.items()):
                print(f"    {state:<16} {n}")
        return 0
    finally:
        motor.close()


def cmd_needs_me(args) -> int:
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        open_items = motor.store.open_approvals(motor.workspace.id)
        if not open_items:
            print("nada precisa de voce now.")
            return 0
        for a in open_items:
            t = motor.store.task(a.task_id)
            print(escalation.render(escalation.briefing(a, t)))
            print(f"\n  regente decide {a.id} <opcao>")
            print("-" * 62)
        return 0
    finally:
        motor.close()


def cmd_decide(args) -> int:
    """Decide uma escalada -- pelo mesmo caminho que a Mission Control usa.

    Este comando estava QUEBRADO: o parser recebia `opcao` e `--por`, o handler
    lia `args.option` e `args.per`, e a chamada morria com `AttributeError`
    antes de tocar no store. Ninguem viu porque todo teste chamava
    `store.decide_approval` diretamente -- a fiacao de argumentos do CLI nao
    tinha teste nenhum, e e justamente onde uma renomeacao deixa restos.

    `--por` tambem foi embora, e essa parte e de propósito. Identidade digitada
    nao e identidade: gravava na auditoria o texto que a pessoa quisesse. Quem
    assina agora e a conta que roda o processo.
    """
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        who = motor.terminal_principal()
        outcome = motor.decisions().decide(
            who, motor.workspace.id, args.approval_id, args.opcao,
            note=args.nota or "")
        if not outcome.accepted:
            print(f"{outcome.denial.value}: {outcome.reason}", file=sys.stderr)
            return 1
        print(f"{outcome.task_key or outcome.task_id}: "
              f"registrado '{outcome.choice}' por {outcome.decided_by}.")
        print("O proximo tick retoma a task a partir daqui.")
        return 0
    finally:
        motor.close()


def cmd_health(args) -> int:
    """What is running, what is stuck, for how long and why.

    Reads persisted state only. It answers after a crash, which is the moment it
    matters -- a report assembled from a live process's memory would be empty
    exactly then, and an empty report reads like a healthy one.
    """
    from .engine import health as health_module

    cfg = load(args.config)
    store = SqliteStore(cfg.banco)
    store.migrate()
    try:
        ws_id = _stable_id(ids.WORKSPACE, cfg.organization, cfg.client, cfg.workspace)
        report = health_module.inspect(
            store, ws_id, cfg.workspace,
            areas_root=cfg.areas,
            budget_usd=cfg.budget.max_cost_usd,
            max_dispatches=cfg.limits.max_dispatches_per_day)
        print(report.render())
    finally:
        store.close()
    # Exit code carries the verdict so a cron job can act on it without parsing
    # prose: 0 healthy, 1 needs attention or is unexamined, 2 stuck.
    return {health_module.Level.OK: 0,
            health_module.Level.ATTENTION: 1,
            health_module.Level.UNKNOWN: 1,
            health_module.Level.STUCK: 2}[report.level]


def cmd_ui(args) -> int:
    """Sobe a Mission Control sobre o estado deste workspace.

    Um processo, uma porta, loopback. A tela e servida pelo mesmo servidor que
    responde a API para nao existir configuracao de origem cruzada -- e para nao
    existir a tentacao de abrir CORS "so para desenvolver".

    Nao ha autenticacao nesta versao, e por isso o default nao escuta na rede.
    Quem precisar expor tem de trocar o `Principal` por um vindo de identidade
    real; a fronteira ja existe, vazia de proposito.
    """
    from .adapters.identity.dev_token import DevTokenIdentity
    from .app.api import serve
    from .app.config import load_policies
    from .core.policy import PolicyEngine
    from .engine.decision import DecisionService
    from .engine.readmodel import ReadModel

    cfg = _load_config(args)
    local = args.host in ("127.0.0.1", "::1", "localhost")
    if not local and not args.i_know_this_is_not_authenticated:
        # Recusa no codigo, e nao conselho no README. O unico mecanismo de
        # identidade desta versao e de desenvolvimento; servi-lo na rede
        # entregaria o estado de todos os clientes visiveis a quem alcancar a
        # porta -- e a escrita junto.
        print(f"recusando escutar em {args.host}: o mecanismo de identidade "
              f"desta versao e SOMENTE DESENVOLVIMENTO e nao serve para "
              f"exposicao em rede.\nUse --host 127.0.0.1, ou ligue um provedor "
              f"de identidade real antes de expor.", file=sys.stderr)
        return 2

    store = SqliteStore(cfg.banco)
    store.migrate()
    # Os nomes de organizacao e cliente so existem no arquivo de configuracao, e
    # este comando e um dos poucos lugares que o le. Sem esta linha a tela mostra
    # um id opaco para quem precisa saber de quem e o trabalho -- que foi
    # exatamente o que a primeira execucao real mostrou.
    store.save_client(_stable_id(ids.CLIENT, cfg.organization, cfg.client),
                      cfg.organization, cfg.client)
    read = ReadModel(store=store, areas_root=str(cfg.areas),
                     organization=cfg.organization,
                     budget_usd=cfg.budget.max_cost_usd,
                     max_dispatches=cfg.limits.max_dispatches_per_day)

    # Escopo do operador local. `None` seria "todos os workspaces do banco";
    # nomear os do proprio arquivo de configuracao e mais estreito e continua
    # sendo verdade -- e o dia em que houver identidade real, so este ponto muda.
    configured = _stable_id(ids.WORKSPACE, cfg.organization, cfg.client,
                            cfg.workspace)
    visible = frozenset({configured})
    if args.all_workspaces:
        visible = None

    # Ler e decidir sao concessoes separadas, e a de escrita e sempre estreita:
    # o workspace desta configuracao, mesmo quando a leitura foi ampliada.
    # `--read-only` remove a concessao de escrita sem remover a identidade.
    decides = frozenset() if args.read_only else frozenset({configured})

    identity = DevTokenIdentity(
        operator=args.as_operator or cfg.client,
        reads=visible, decides=decides, bind_is_local=local)

    decisions = DecisionService(
        store=store, policy=PolicyEngine.from_config(load_policies(cfg.policies)),
        organization=cfg.organization, client=cfg.client,
        workspace_name=cfg.workspace,
        environment=(cfg.projects[0].default_environment
                     if cfg.projects else "staging"))

    httpd = serve(read, host=args.host, port=args.port, identity=identity,
                  decisions=decisions, session_token=identity.token)
    where = f"http://{args.host}:{args.port}/"
    print(f"Mission Control em {where}")
    print(f"identidade: {identity.describe()}")
    if identity.development_only:
        print("ATENCAO: mecanismo de identidade SOMENTE DESENVOLVIMENTO")
    print(f"escrita permitida: {'nenhuma' if not decides else 'decidir escaladas'}")
    print("ctrl-c para parar")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
        identity.close()
        store.close()
    return 0


def cmd_log(args) -> int:
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        target = None
        if args.task:
            for t in motor.store.tasks(motor.workspace.id):
                if t.key == args.task or t.id == args.task:
                    target = t.id
                    break
            if target is None:
                print(f"task '{args.task}' nao encontrada")
                return 1
        events = motor.store.events(motor.workspace.id, task_id=target, limit=args.n)
        for e in reversed(events):
            hora = e.ts.strftime("%d/%m %H:%M")
            key = ""
            if e.task_id and not target:
                t = motor.store.task(e.task_id)
                key = f"{t.key} " if t else ""
            print(f"{hora}  {key}{e.kind:<14} {e.summary}")
        return 0
    finally:
        motor.close()


def cmd_plan(args) -> int:
    """Mostra a decisao do scheduler sem executar nada."""
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        p = motor.orchestrator.plan()
        if p.dispatch:
            print("DESPACHARIA EM PARALELO")
            for i in p.dispatch:
                t = motor.store.task(i)
                print(f"  {t.key:<16} {', '.join(t.resources)}")
        else:
            print("nada pronto para despachar")
        if p.deferred:
            print()
            print("ADIADAS")
            for a in p.deferred:
                t = motor.store.task(a.task_id)
                print(f"  {t.key:<16} {a.reason}")
        if p.in_cycle:
            print()
            print("EM CICLO (ninguem pode comecar)")
            for i in p.in_cycle:
                print(f"  {motor.store.task(i).key}")
        return 0
    finally:
        motor.close()


def cmd_sombra(args) -> int:
    """Descobre e planeja contra o provedor real, sem mutar nada."""
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        r = shadow.execute(
            provider=motor.orchestrator.tasks_provider,
            limits=cfg.limits,
            filtro={"apenas_minhas": True} if args.mine else None,
            eu=args.eu)
        print(shadow.render(r))
        if args.output:
            Path(args.output).write_text(shadow.render(r), encoding="utf-8")
            print()
            print(f"  gravado em {args.output}")
        return 0 if not r.provider_errors else 2
    finally:
        motor.close()


def cmd_repos(args) -> int:
    """Repositorios visiveis, como o motor os enxerga."""
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        if motor.repos is None:
            print("nenhum provedor de repositorio configurado")
            return 1
        items = motor.repos.list_repositories()
        print(f"{len(items)} repositorio(s) via {motor.repos.name}")
        print()
        for r in sorted(items, key=lambda x: x.ref.key):
            mark = "!" if r.anomalies else " "
            print(f" {mark} {r.ref.key:<46} base={r.base_branch or '(nao lida)':<10}")
            if args.verbose:
                print(f"     recurso: {r.ref.resource(motor.workspace.id)}")
                if r.anomalies:
                    print(f"     anomalias: {'; '.join(r.anomalies)}")
        return 0
    finally:
        motor.close()


def cmd_cadeia(args) -> int:
    """task -> repositorio -> base -> recursos -> risco/policy -> candidato."""
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        if motor.repos is None:
            print("nenhum provedor de repositorio configurado")
            return 1
        items = motor.orchestrator.tasks_provider.list_tasks()
        repositories = motor.repos.list_repositories()
        branches = {}
        if not args.sem_branches:
            for r in repositories:
                try:
                    branches[r.ref.key] = motor.repos.list_branches(r.ref.key)
                except Exception:
                    branches[r.ref.key] = []
        rel = chain.build(
            workspace_nome=motor.workspace.name, workspace_id=motor.workspace.id,
            tasks=items, repos=repositories, resolvedor=motor.resolvedor,
            policy=motor.policy, risk=motor.risk,
            autonomy=motor.workspace.max_autonomy, branches=branches,
            organization=cfg.organization, client=cfg.client)
        print(chain.render(rel, limit=args.limit))
        if args.output:
            Path(args.output).write_text(chain.render(rel, limit=200), encoding="utf-8")
            print()
            print(f"  gravado em {args.output}")
        return 0
    finally:
        motor.close()


def cmd_mission(args) -> int:
    """Select one task and show the briefing. Executes only with --run."""
    cfg = _load_config(args)
    engine = container.build(cfg)
    try:
        if engine.repos is None:
            print("no repository provider configured")
            return 1
        outcome = engine.run_mission(execute=args.run, only=args.task)
        if outcome.refused:
            print(outcome.refusal)
            return 3
        print(outcome.briefing.render())
        if not args.run:
            print()
            print("  DRY: nothing was executed. Add --run to execute.")
            return 0
        print()
        print(f"VERDICT  {outcome.verdict.value}")
        print(f"  {outcome.loop.reason}")
        if outcome.loop.changed_files:
            print(f"  changed: {', '.join(outcome.loop.changed_files[:8])}")
        print()
        print(outcome.measurements.render())
        if args.output:
            report = outcome.briefing.render() + "\n\n" + outcome.measurements.render()
            Path(args.output).write_text(report, encoding="utf-8")
        return 0
    finally:
        engine.close()


def cmd_rules(args) -> int:
    cfg = _load_config(args)
    from .app.config import load_policies
    from .adapters import registry
    print(f"autonomia maxima: {cfg.autonomy.name}")
    print(f"modo: {'sombra' if cfg.shadow else 'VALENDO'}")
    print(f"limites: {cfg.limits.max_workers} workers, "
          f"{cfg.limits.max_dispatches_per_day} despachos/dia")
    print()
    print("REGRAS")
    for r in load_policies(cfg.policies):
        criteria = ", ".join(f"{k}={v}" for k, v in (r.get("match") or {}).items())
        print(f"  {r['effect']:<15} {r.get('name', '?'):<26} {criteria}")
    print()
    print("ADAPTERS DISPONIVEIS")
    for cap, nomes in registry.available().items():
        print(f"  {cap:<14} {', '.join(nomes)}")
    return 0


# ---- entrada -------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    ap = argparse.ArgumentParser(prog="regente",
                                 description="Sistema operacional para agentes de engenharia.")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="cria a configuracao inicial")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("doctor", help="prova que o motor sobe")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("tick", help="roda um ciclo")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_tick)

    p = sub.add_parser("status", help="o que esta acontecendo")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("plan", help="o que o scheduler faria now")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("health", help="o que esta rodando, o que travou e ha quanto tempo")
    p.add_argument("--config", default="regente.yaml")
    p.set_defaults(fn=cmd_health)

    p = sub.add_parser("needs-me", help="a fila de decisoes humanas")
    p.set_defaults(fn=cmd_needs_me)

    p = sub.add_parser("decide", help="decide um item da fila")
    p.add_argument("approval_id")
    p.add_argument("opcao")
    # Sem `--por`: quem assina e a conta que roda o processo, e nao um texto
    # que quem decide escolhe. Identidade digitada nao e identidade.
    p.add_argument("--nota", default="")
    p.set_defaults(fn=cmd_decide)

    p = sub.add_parser("log", help="a trilha do que o motor fez")
    p.add_argument("-n", type=int, default=40)
    p.add_argument("--task", help="filtra por chave de task")
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("sombra", help="ve o trabalho real sem tocar em nada")
    p.add_argument("--minhas", action="store_true", help="so o que esta comigo")
    p.add_argument("--eu", help="nome do responsavel a contar como 'minhas'")
    p.add_argument("--saida", help="grava o relatorio neste arquivo")
    p.set_defaults(fn=cmd_sombra)

    p = sub.add_parser("repos", help="repositorios visiveis, sem tocar em nada")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_repos)

    p = sub.add_parser("cadeia", help="da task real ao candidato a execucao, em sombra")
    p.add_argument("--limite", type=int, default=10)
    p.add_argument("--sem-branches", action="store_true",
                   help="pula a leitura de branches (mais rapido, menos evidencia)")
    p.add_argument("--saida", help="grava o relatorio neste arquivo")
    p.set_defaults(fn=cmd_cadeia)

    p = sub.add_parser("mission", help="select one task, show the briefing, optionally run")
    p.add_argument("--run", action="store_true", help="execute; without it, nothing runs")
    p.add_argument("--task", help="restrict selection to this task key")
    p.add_argument("--output", help="write briefing and metrics to this file")
    p.set_defaults(fn=cmd_mission)

    p = sub.add_parser("rules", help="regras, limites e adapters em vigor")
    p.set_defaults(fn=cmd_rules)

    p = sub.add_parser("ui", help="Mission Control: o estado do motor numa tela")
    p.add_argument("--config", default="regente.yaml")
    p.add_argument("--host", default="127.0.0.1",
                   help="loopback por padrao: esta versao nao autentica ninguem")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--all-workspaces", action="store_true",
                   help="mostra todo workspace do banco, nao so o configurado")
    p.add_argument("--read-only", action="store_true",
                   help="nao concede autoridade de decisao a esta sessao")
    p.add_argument("--as-operator", default="",
                   help="como esta sessao assina na auditoria; o default vem "
                        "da configuracao, nunca do navegador")
    p.add_argument("--i-know-this-is-not-authenticated", action="store_true",
                   help="permite escutar fora do loopback; o provedor de "
                        "identidade de desenvolvimento ainda recusa autenticar")
    p.set_defaults(fn=cmd_ui)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except FileNotFoundError as e:
        print(f"{e}\nRode `regente init` para comecar.", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
