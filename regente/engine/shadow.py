# -*- coding: utf-8 -*-
"""Shadow: discover, normalise and plan -- mutating nothing, nowhere.

The report answers a single question: **does the engine see the real work?**

It uses neither the Store nor the state machine. That is deliberate: a shadow run
has to be repeatable and disposable, and writing state would turn it into a
half-real run. What it does share with the real engine is what matters to prove
-- the graph and the scheduler are the SAME objects.

`mutations` in the report is always zero, and not out of trust: the shadow
adapter has no write path. The field exists so the number appears next to the
others, where somebody would notice if it ever stopped being zero.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..core.graph import DependencyGraph
from ..core.scheduling import Candidate, Limits, Plan, plan
from ..ports import AdapterError
from ..ports.tasks import ExternalTask, ExternalStatus, TaskProvider


@dataclass(slots=True)
class ShadowReport:
    provider: str
    discovered: int = 0
    relevant: int = 0
    ignored: int = 0
    normalized: int = 0
    normalization_errors: int = 0
    by_status: Counter = field(default_factory=Counter)
    by_external_status: Counter = field(default_factory=Counter)
    dependencies: int = 0
    non_blocking_links: int = 0
    #: A dependency declared on a task the query did not bring back. Not an
    #: engine error: it is the JQL slice being smaller than the real graph, and it
    #: has to be visible.
    dependencies_out_of_scope: int = 0
    ready: int = 0
    blocked: int = 0
    in_progress: int = 0
    parallel_groups: int = 0
    largest_group: int = 0
    in_cycle: tuple[str, ...] = ()
    #: Why each task stayed out of the dispatch. Without this, "Ready 4" looks
    #: like a shortage of work when it may just be the worker cap -- two
    #: situations that demand opposite actions from the owner.
    deferral_reasons: Counter = field(default_factory=Counter)
    anomalies: tuple[str, ...] = ()
    calls: int = 0
    provider_errors: int = 0
    mutations: int = 0
    plan: Plan | None = None
    mine: int = 0


def execute(
    provider: TaskProvider,
    limits: Limits,
    filtro: dict | None = None,
    eu: str | None = None,
) -> ShadowReport:
    r = ShadowReport(provider=provider.name)

    try:
        external_items: list[ExternalTask] = provider.list_tasks(filtro)
    except AdapterError as e:
        r.provider_errors += 1
        r.anomalies += (f"provider failed: {e}",)
        return r

    r.discovered = len(external_items)

    # Relevant = work that still exists. Finished work leaves the count, and
    # leaving the count differs from vanishing: the ignored figure stays in the
    # report.
    alive: list[ExternalTask] = []
    for t in external_items:
        r.by_status[t.status.value] += 1
        r.by_external_status[t.external_status or "(no status)"] += 1
        if t.status.finished:
            r.ignored += 1
            continue
        alive.append(t)
        r.normalized += 1
        if t.anomalies:
            r.anomalies += tuple(f"{t.key}: {a}" for a in t.anomalies)
        if eu and t.assignee == eu:
            r.mine += 1
    r.relevant = len(alive)

    # Graph with the SAME rule as the engine: only a blocking link becomes an edge.
    known = {t.key for t in alive}
    grafo = DependencyGraph()
    for t in alive:
        grafo.add(t.key)
    for t in alive:
        for v in t.links:
            if not v.blocking:
                r.non_blocking_links += 1
                continue
            if v.key not in known:
                r.dependencies_out_of_scope += 1
                continue
            grafo.link(t.key, v.key)
            r.dependencies += 1

    # A task somebody is already working on is not a candidate, but stays in the
    # graph: it is somebody else's blocker, and dropping it would be a lie.
    available = [t for t in alive if t.status.available]
    r.in_progress = sum(1 for t in alive if t.status.in_progress)

    candidates = [
        Candidate(task_id=t.key, priority=t.priority,
                  resources=frozenset(t.resources), key=t.key)
        for t in available
    ]
    # Nothing completed: the shadow has no history. The plan shows what the
    # engine would do on the FIRST tick against this board.
    plan = plan(candidates, grafo, completed=set(), running_now={},
                    limits=limits, nomes={t.key: t.key for t in alive})
    r.plan = plan
    r.ready = len(plan.dispatch)
    r.blocked = len(plan.deferred)
    r.in_cycle = plan.in_cycle
    for a in plan.deferred:
        # Group by CAUSE, not by text: "resource busy: parent:SG-1" and
        # "resource busy: parent:SG-2" are the same diagnosis.
        r.deferral_reasons[a.reason.split(":")[0].strip()] += 1

    layers = grafo.layers()
    r.parallel_groups = len(layers)
    r.largest_group = max((len(c) for c in layers), default=0)

    transport = getattr(provider, "transport", None)
    r.calls = len(getattr(transport, "chamadas", []) or [])
    return r


def render(r: ShadowReport, limite_anomalias: int = 12) -> str:
    def linha(rotulo: str, value) -> str:
        return f"  {rotulo:<28} {value}"

    output = [
        "TASK PROVIDER SHADOW REPORT",
        "",
        linha("Provider", r.provider),
        linha("Mutations performed", f"{r.mutations}   <- has to be 0"),
        "",
        linha("Tasks discovered", r.discovered),
        linha("Relevant", r.relevant),
        linha("Ignored (finished)", r.ignored),
        linha("Normalized successfully", r.normalized),
        linha("Normalization errors", r.normalization_errors),
        "",
        linha("Dependencies (blocking)", r.dependencies),
        linha("Non-blocking ignored", r.non_blocking_links),
        linha("Outside the JQL slice", r.dependencies_out_of_scope),
        "",
        linha("Ready", r.ready),
        linha("Blocked", r.blocked),
        linha("In progress (others)", r.in_progress),
        linha("Parallel groups", r.parallel_groups),
        linha("Largest parallel group", r.largest_group),
        "",
        linha("Provider calls", r.calls),
        linha("Provider errors", r.provider_errors),
    ]
    if r.mine:
        output.insert(6, linha("Mine", r.mine))

    output += ["", "  BY NORMALISED STATUS"]
    for k, v in r.by_status.most_common():
        output.append(f"    {k:<16} {v}")

    output += ["", "  BY SOURCE STATUS"]
    for k, v in r.by_external_status.most_common(10):
        output.append(f"    {k:<16} {v}")

    if r.deferral_reasons:
        output += ["", "  WHY THE OTHERS DID NOT GO OUT"]
        for k, v in r.deferral_reasons.most_common():
            output.append(f"    {k:<24} {v}")

    if r.plan and r.plan.dispatch:
        output += ["", f"  WOULD DISPATCH NOW ({len(r.plan.dispatch)} in parallel)"]
        for t in r.plan.dispatch:
            output.append(f"    {t}")

    if r.in_cycle:
        output += ["", "  IN A CYCLE (nobody can start)"]
        output += [f"    {t}" for t in r.in_cycle]

    if r.anomalies:
        output += ["", f"  ANOMALIES ({len(r.anomalies)})"]
        for a in r.anomalies[:limite_anomalias]:
            output.append(f"    {a}")
        if len(r.anomalies) > limite_anomalias:
            output.append(f"    ... {len(r.anomalies) - limite_anomalias} more")

    return "\n".join(output)
