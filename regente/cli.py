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
from .app.config import Config, carrega
from .core.states import TaskState
from .engine import cadeia, escalation, sombra

PADRAO = "regente.yaml"


def _utf8() -> None:
    # Sem isto, um titulo com acento derruba o comando no console do Windows.
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _config(args) -> Config:
    return carrega(args.config)


# ---- comandos ------------------------------------------------------------

def cmd_init(args) -> int:
    destino = Path(args.config)
    if destino.exists() and not args.force:
        print(f"{destino} ja existe. Use --force para sobrescrever.")
        return 1
    modelo = Path(__file__).parent / "recursos" / "regente.yaml.exemplo"
    destino.write_text(modelo.read_text(encoding="utf-8"), encoding="utf-8")
    tasks = destino.parent / "tasks"
    tasks.mkdir(exist_ok=True)
    print(f"criado {destino}")
    print(f"criado {tasks}/ -- descreva trabalho em YAML aqui")
    print("proximo: regente doctor")
    return 0


def cmd_doctor(args) -> int:
    cfg = _config(args)
    problemas = 0
    for nome, ok, detalhe in container.diagnostico(cfg):
        marca = "ok  " if ok else "FALHA"
        print(f"  {marca}  {nome:<28} {detalhe}")
        problemas += 0 if ok else 1
    print()
    print("tudo pronto" if not problemas else f"{problemas} problema(s) -- o motor nao vai rodar assim")
    return 0 if not problemas else 2


def cmd_tick(args) -> int:
    cfg = _config(args)
    motor = container.monta(cfg)
    try:
        rel = motor.orchestrator.tick()
        print(rel.resumo())
        if rel.despachadas:
            print("  despachadas:", ", ".join(rel.despachadas))
        if rel.concluidas:
            print("  concluidas: ", ", ".join(rel.concluidas))
        if rel.recuperadas:
            print("  recuperadas:", ", ".join(rel.recuperadas))
        if rel.ciclos:
            print("  em ciclo:   ", ", ".join(rel.ciclos))
        if args.verboso and rel.adiadas:
            for chave, motivo in rel.adiadas:
                print(f"  adiada {chave}: {motivo}")
        for e in rel.erros:
            print("  erro:", e)
        if rel.escalonadas:
            print()
            print(f"  {len(rel.escalonadas)} precisam de voce: regente needs-me")
        return 0
    finally:
        motor.fecha()


def cmd_status(args) -> int:
    cfg = _config(args)
    motor = container.monta(cfg)
    try:
        store, ws = motor.store, motor.workspace
        tasks = store.tasks(ws.id)
        por_estado: dict[str, int] = {}
        for t in tasks:
            por_estado[t.estado.value] = por_estado.get(t.estado.value, 0) + 1

        rodando = [t for t in tasks if t.estado.value in
                   {"ASSIGNED", "IMPLEMENTING", "TESTING", "CI_RUNNING", "AI_REVIEW",
                    "MERGING", "DEPLOYING"}]
        abertos = store.approvals_abertos(ws.id)
        bloqueadas = [t for t in tasks if t.estado in (TaskState.BLOCKED, TaskState.FAILED)]
        prontas = [t for t in tasks if t.estado is TaskState.DONE]

        print(f"REGENTE -- {ws.nome}  [{'sombra' if cfg.sombra else 'VALENDO'}]")
        print()
        print(f"  Rodando     {len(rodando)}")
        print(f"  Precisa de voce  {len(abertos)}" + ("   <-- prioridade" if abertos else ""))
        print(f"  Bloqueadas  {len(bloqueadas)}")
        print(f"  Concluidas  {len(prontas)}")

        if rodando:
            print()
            print("  TRABALHO ATIVO")
            for t in rodando:
                print(f"    {t.chave:<16} {t.estado.value}")
        if abertos:
            print()
            print("  PRECISA DE VOCE")
            for a in abertos:
                t = store.task(a.task_id)
                print(f"    [{a.risco.name}] {t.chave:<16} {a.o_que_aconteceu[:60]}")
            print()
            print("    regente needs-me   para ver e decidir")
        if args.verboso:
            print()
            print("  POR ESTADO")
            for estado, n in sorted(por_estado.items()):
                print(f"    {estado:<16} {n}")
        return 0
    finally:
        motor.fecha()


def cmd_needs_me(args) -> int:
    cfg = _config(args)
    motor = container.monta(cfg)
    try:
        abertos = motor.store.approvals_abertos(motor.workspace.id)
        if not abertos:
            print("nada precisa de voce agora.")
            return 0
        for a in abertos:
            t = motor.store.task(a.task_id)
            print(escalation.texto(escalation.briefing(a, t)))
            print(f"\n  regente decide {a.id} <opcao>")
            print("-" * 62)
        return 0
    finally:
        motor.fecha()


def cmd_decide(args) -> int:
    cfg = _config(args)
    motor = container.monta(cfg)
    try:
        a = motor.store.decide_approval(args.approval_id, args.opcao,
                                        por=args.por, nota=args.nota or "")
        t = motor.store.task(a.task_id)
        print(f"{t.chave}: registrado '{args.opcao}'.")
        print("O proximo tick retoma a task a partir daqui.")
        return 0
    finally:
        motor.fecha()


def cmd_log(args) -> int:
    cfg = _config(args)
    motor = container.monta(cfg)
    try:
        alvo = None
        if args.task:
            for t in motor.store.tasks(motor.workspace.id):
                if t.chave == args.task or t.id == args.task:
                    alvo = t.id
                    break
            if alvo is None:
                print(f"task '{args.task}' nao encontrada")
                return 1
        eventos = motor.store.eventos(motor.workspace.id, task_id=alvo, limite=args.n)
        for e in reversed(eventos):
            hora = e.ts.strftime("%d/%m %H:%M")
            chave = ""
            if e.task_id and not alvo:
                t = motor.store.task(e.task_id)
                chave = f"{t.chave} " if t else ""
            print(f"{hora}  {chave}{e.tipo:<14} {e.resumo}")
        return 0
    finally:
        motor.fecha()


def cmd_plan(args) -> int:
    """Mostra a decisao do scheduler sem executar nada."""
    cfg = _config(args)
    motor = container.monta(cfg)
    try:
        p = motor.orchestrator.plano()
        if p.despachar:
            print("DESPACHARIA EM PARALELO")
            for i in p.despachar:
                t = motor.store.task(i)
                print(f"  {t.chave:<16} {', '.join(t.recursos)}")
        else:
            print("nada pronto para despachar")
        if p.adiadas:
            print()
            print("ADIADAS")
            for a in p.adiadas:
                t = motor.store.task(a.task_id)
                print(f"  {t.chave:<16} {a.motivo}")
        if p.em_ciclo:
            print()
            print("EM CICLO (ninguem pode comecar)")
            for i in p.em_ciclo:
                print(f"  {motor.store.task(i).chave}")
        return 0
    finally:
        motor.fecha()


def cmd_sombra(args) -> int:
    """Descobre e planeja contra o provedor real, sem mutar nada."""
    cfg = _config(args)
    motor = container.monta(cfg)
    try:
        r = sombra.executa(
            provedor=motor.orchestrator.tasks_provider,
            limites=cfg.limites,
            filtro={"apenas_minhas": True} if args.minhas else None,
            eu=args.eu)
        print(sombra.texto(r))
        if args.saida:
            Path(args.saida).write_text(sombra.texto(r), encoding="utf-8")
            print()
            print(f"  gravado em {args.saida}")
        return 0 if not r.erros_do_provedor else 2
    finally:
        motor.fecha()


def cmd_repos(args) -> int:
    """Repositorios visiveis, como o motor os enxerga."""
    cfg = _config(args)
    motor = container.monta(cfg)
    try:
        if motor.repos is None:
            print("nenhum provedor de repositorio configurado")
            return 1
        lista = motor.repos.list_repositories()
        print(f"{len(lista)} repositorio(s) via {motor.repos.nome}")
        print()
        for r in sorted(lista, key=lambda x: x.ref.key):
            marca = "!" if r.anomalias else " "
            print(f" {marca} {r.ref.key:<46} base={r.branch_base or '(nao lida)':<10}")
            if args.verboso:
                print(f"     recurso: {r.ref.recurso(motor.workspace.id)}")
                if r.anomalias:
                    print(f"     anomalias: {'; '.join(r.anomalias)}")
        return 0
    finally:
        motor.fecha()


def cmd_cadeia(args) -> int:
    """task -> repositorio -> base -> recursos -> risco/policy -> candidato."""
    cfg = _config(args)
    motor = container.monta(cfg)
    try:
        if motor.repos is None:
            print("nenhum provedor de repositorio configurado")
            return 1
        tarefas = motor.orchestrator.tasks_provider.list_tasks()
        repositorios = motor.repos.list_repositories()
        branches = {}
        if not args.sem_branches:
            for r in repositorios:
                try:
                    branches[r.ref.key] = motor.repos.list_branches(r.ref.key)
                except Exception:
                    branches[r.ref.key] = []
        rel = cadeia.monta(
            workspace_nome=motor.workspace.nome, workspace_id=motor.workspace.id,
            tasks=tarefas, repos=repositorios, resolvedor=motor.resolvedor,
            policy=motor.policy, risco=motor.risco,
            autonomia=motor.workspace.autonomia_maxima, branches=branches,
            organizacao=cfg.organizacao, cliente=cfg.cliente)
        print(cadeia.texto(rel, limite=args.limite))
        if args.saida:
            Path(args.saida).write_text(cadeia.texto(rel, limite=200), encoding="utf-8")
            print()
            print(f"  gravado em {args.saida}")
        return 0
    finally:
        motor.fecha()


def cmd_rules(args) -> int:
    cfg = _config(args)
    from .app.config import carrega_policies
    from .adapters import registry
    print(f"autonomia maxima: {cfg.autonomia.name}")
    print(f"modo: {'sombra' if cfg.sombra else 'VALENDO'}")
    print(f"limites: {cfg.limites.max_workers} workers, "
          f"{cfg.limites.max_despachos_dia} despachos/dia")
    print()
    print("REGRAS")
    for r in carrega_policies(cfg.policies):
        criterios = ", ".join(f"{k}={v}" for k, v in (r.get("match") or {}).items())
        print(f"  {r['efeito']:<15} {r.get('nome', '?'):<26} {criterios}")
    print()
    print("ADAPTERS DISPONIVEIS")
    for cap, nomes in registry.disponiveis().items():
        print(f"  {cap:<14} {', '.join(nomes)}")
    return 0


# ---- entrada -------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    _utf8()
    ap = argparse.ArgumentParser(prog="regente",
                                 description="Sistema operacional para agentes de engenharia.")
    ap.add_argument("-c", "--config", default=PADRAO)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="cria a configuracao inicial")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("doctor", help="prova que o motor sobe")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("tick", help="roda um ciclo")
    p.add_argument("-v", "--verboso", action="store_true")
    p.set_defaults(fn=cmd_tick)

    p = sub.add_parser("status", help="o que esta acontecendo")
    p.add_argument("-v", "--verboso", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("plan", help="o que o scheduler faria agora")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("needs-me", help="a fila de decisoes humanas")
    p.set_defaults(fn=cmd_needs_me)

    p = sub.add_parser("decide", help="decide um item da fila")
    p.add_argument("approval_id")
    p.add_argument("opcao")
    p.add_argument("--por", default="humano")
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
    p.add_argument("-v", "--verboso", action="store_true")
    p.set_defaults(fn=cmd_repos)

    p = sub.add_parser("cadeia", help="da task real ao candidato a execucao, em sombra")
    p.add_argument("--limite", type=int, default=10)
    p.add_argument("--sem-branches", action="store_true",
                   help="pula a leitura de branches (mais rapido, menos evidencia)")
    p.add_argument("--saida", help="grava o relatorio neste arquivo")
    p.set_defaults(fn=cmd_cadeia)

    p = sub.add_parser("rules", help="regras, limites e adapters em vigor")
    p.set_defaults(fn=cmd_rules)

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
