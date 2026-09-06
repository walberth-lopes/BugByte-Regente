# -*- coding: utf-8 -*-
"""The command-line surface.

The screen answers four questions, in this order of importance: what needs me,
what is happening, what finished, and is there a problem. Internal complexity --
leases, runs, the graph, the policy -- only shows up when somebody asks.

Subcommand and flag names are the CLI's public contract and stay as they are;
what is translated here is what the CLI prints.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .app import container
from .app.config import Config, load
from .core.states import TaskState
from .engine import chain, escalation, shadow

DEFAULT_CONFIG_FILE = "regente.yaml"


def _force_utf8() -> None:
    # Without this, a title with an accent brings the command down on the Windows console.
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _load_config(args) -> Config:
    return load(args.config)


# ---- commands ------------------------------------------------------------

def cmd_init(args) -> int:
    destination = Path(args.config)
    if destination.exists() and not args.force:
        print(f"{destination} already exists. Use --force to overwrite.")
        return 1
    model = Path(__file__).parent / "resources" / "regente.yaml.example"
    destination.write_text(model.read_text(encoding="utf-8"), encoding="utf-8")
    tasks = destination.parent / "tasks"
    tasks.mkdir(exist_ok=True)
    print(f"created {destination}")
    print(f"created {tasks}/ -- describe work in YAML here")
    print("next: regente doctor")
    return 0


def cmd_doctor(args) -> int:
    cfg = _load_config(args)
    problemas = 0
    for name, ok, detalhe in container.diagnose(cfg):
        mark = "ok  " if ok else "FAIL "
        print(f"  {mark}  {name:<28} {detalhe}")
        problemas += 0 if ok else 1
    print()
    print("all set" if not problemas else f"{problemas} problem(s) -- the engine will not run like this")
    return 0 if not problemas else 2


def cmd_tick(args) -> int:
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        rel = motor.orchestrator.tick()
        print(rel.summary())
        if rel.dispatched:
            print("  dispatched:", ", ".join(rel.dispatched))
        if rel.completed:
            print("  completed: ", ", ".join(rel.completed))
        if rel.recovered:
            print("  recovered: ", ", ".join(rel.recovered))
        if rel.cycles:
            print("  in a cycle:", ", ".join(rel.cycles))
        if args.verbose and rel.deferred:
            for key, reason in rel.deferred:
                print(f"  deferred {key}: {reason}")
        for e in rel.errors:
            print("  error:", e)
        if rel.escalated:
            print()
            print(f"  {len(rel.escalated)} need you: regente needs-me")
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

        print(f"REGENTE -- {ws.name}  [{'shadow' if cfg.shadow else 'LIVE'}]")
        print()
        print(f"  Running     {len(rodando)}")
        print(f"  Needs you   {len(open_items)}" + ("   <-- priority" if open_items else ""))
        print(f"  Blocked     {len(blocked)}")
        print(f"  Completed   {len(ready)}")

        if rodando:
            print()
            print("  ACTIVE WORK")
            for t in rodando:
                print(f"    {t.key:<16} {t.state.value}")
        if open_items:
            print()
            print("  NEEDS YOU")
            for a in open_items:
                t = store.task(a.task_id)
                print(f"    [{a.risk.name}] {t.key:<16} {a.what_happened[:60]}")
            print()
            print("    regente needs-me   to see and decide")
        if args.verbose:
            print()
            print("  BY STATE")
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
            print("nothing needs you right now.")
            return 0
        for a in open_items:
            t = motor.store.task(a.task_id)
            print(escalation.render(escalation.briefing(a, t)))
            print(f"\n  regente decide {a.id} <option>")
            print("-" * 62)
        return 0
    finally:
        motor.close()


def cmd_decide(args) -> int:
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        a = motor.store.decide_approval(args.approval_id, args.option,
                                        per=args.per, note=args.note or "")
        t = motor.store.task(a.task_id)
        print(f"{t.key}: recorded '{args.option}'.")
        print("The next tick resumes the task from here.")
        return 0
    finally:
        motor.close()


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
                print(f"task '{args.task}' not found")
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
    """Shows the scheduler's decision without executing anything."""
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        p = motor.orchestrator.plan()
        if p.dispatch:
            print("WOULD DISPATCH IN PARALLEL")
            for i in p.dispatch:
                t = motor.store.task(i)
                print(f"  {t.key:<16} {', '.join(t.resources)}")
        else:
            print("nothing ready to dispatch")
        if p.deferred:
            print()
            print("DEFERRED")
            for a in p.deferred:
                t = motor.store.task(a.task_id)
                print(f"  {t.key:<16} {a.reason}")
        if p.in_cycle:
            print()
            print("IN A CYCLE (nobody can start)")
            for i in p.in_cycle:
                print(f"  {motor.store.task(i).key}")
        return 0
    finally:
        motor.close()


def cmd_sombra(args) -> int:
    """Discovers and plans against the real provider, mutating nothing."""
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
            print(f"  written to {args.output}")
        return 0 if not r.provider_errors else 2
    finally:
        motor.close()


def cmd_repos(args) -> int:
    """Visible repositories, as the engine sees them."""
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        if motor.repos is None:
            print("no repository provider configured")
            return 1
        items = motor.repos.list_repositories()
        print(f"{len(items)} repositor(y/ies) via {motor.repos.name}")
        print()
        for r in sorted(items, key=lambda x: x.ref.key):
            mark = "!" if r.anomalies else " "
            print(f" {mark} {r.ref.key:<46} base={r.base_branch or '(not read)':<10}")
            if args.verbose:
                print(f"     resource: {r.ref.resource(motor.workspace.id)}")
                if r.anomalies:
                    print(f"     anomalies: {'; '.join(r.anomalies)}")
        return 0
    finally:
        motor.close()


def cmd_cadeia(args) -> int:
    """task -> repository -> base -> resources -> risk/policy -> candidate."""
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        if motor.repos is None:
            print("no repository provider configured")
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
            tasks=items, repos=repositories, resolvedor=motor.resolver,
            policy=motor.policy, risk=motor.risk,
            autonomy=motor.workspace.max_autonomy, branches=branches,
            organization=cfg.organization, client=cfg.client)
        print(chain.render(rel, limit=args.limit))
        if args.output:
            Path(args.output).write_text(chain.render(rel, limit=200), encoding="utf-8")
            print()
            print(f"  written to {args.output}")
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
    print(f"maximum autonomy: {cfg.autonomy.name}")
    print(f"mode: {'shadow' if cfg.shadow else 'LIVE'}")
    print(f"limits: {cfg.limits.max_workers} workers, "
          f"{cfg.limits.max_dispatches_per_day} dispatches/day")
    print()
    print("RULES")
    for r in load_policies(cfg.policies):
        criteria = ", ".join(f"{k}={v}" for k, v in (r.get("match") or {}).items())
        print(f"  {r['effect']:<15} {r.get('name', '?'):<26} {criteria}")
    print()
    print("AVAILABLE ADAPTERS")
    for cap, nomes in registry.available().items():
        print(f"  {cap:<14} {', '.join(nomes)}")
    return 0


# ---- entry point ---------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    ap = argparse.ArgumentParser(prog="regente",
                                 description="An operating system for software engineering agents.")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create the initial configuration")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("doctor", help="prove the engine starts")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("tick", help="run one cycle")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_tick)

    p = sub.add_parser("status", help="what is happening")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("plan", help="what the scheduler would do now")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("needs-me", help="the queue of human decisions")
    p.set_defaults(fn=cmd_needs_me)

    p = sub.add_parser("decide", help="decide one item in the queue")
    p.add_argument("approval_id")
    p.add_argument("option")
    # `dest="per"` because that is the keyword `Store.decide_approval` takes.
    p.add_argument("--by", dest="per", default="humano")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_decide)

    p = sub.add_parser("log", help="the trail of what the engine did")
    p.add_argument("-n", type=int, default=40)
    p.add_argument("--task", help="filter by task key")
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("sombra", help="see the real work without touching anything")
    p.add_argument("--mine", action="store_true", help="only what is assigned to me")
    p.add_argument("--eu", help="assignee name to count as 'mine'")
    p.add_argument("--output", help="write the report to this file")
    p.set_defaults(fn=cmd_sombra)

    p = sub.add_parser("repos", help="visible repositories, without touching anything")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_repos)

    p = sub.add_parser("cadeia", help="from the real task to an execution candidate, in shadow")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--sem-branches", action="store_true",
                   help="skip reading branches (faster, less evidence)")
    p.add_argument("--output", help="write the report to this file")
    p.set_defaults(fn=cmd_cadeia)

    p = sub.add_parser("mission", help="select one task, show the briefing, optionally run")
    p.add_argument("--run", action="store_true", help="execute; without it, nothing runs")
    p.add_argument("--task", help="restrict selection to this task key")
    p.add_argument("--output", help="write briefing and metrics to this file")
    p.set_defaults(fn=cmd_mission)

    p = sub.add_parser("rules", help="the rules, limits and adapters in force")
    p.set_defaults(fn=cmd_rules)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except FileNotFoundError as e:
        print(f"{e}\nRun `regente init` to get started.", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
