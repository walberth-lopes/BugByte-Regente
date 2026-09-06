# -*- coding: utf-8 -*-
"""Sombra: descobrir, normalizar e planejar -- sem mutar nada, em lugar nenhum.

O relatorio responde a uma pergunta so: **o motor enxerga o trabalho real?**

Ele nao usa o Store nem a maquina de estados. Isso e deliberado: uma execucao de
sombra precisa ser repetivel e descartavel, e gravar estado a transformaria numa
execucao de verdade pela metade. O que ela compartilha com o motor de verdade e
o que importa provar -- o grafo e o scheduler sao os MESMOS objetos.

`mutacoes` no relatorio e sempre zero, e nao por confianca: o adapter de sombra
nao tem caminho de escrita. O campo existe para que o numero apareca ao lado dos
demais, onde alguem notaria se um dia deixasse de ser zero.
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
    #: Dependencia declarada para uma task que a consulta nao trouxe. Nao e error
    #: do motor: e o recorte do JQL menor que o grafo real, e precisa aparecer.
    dependencies_out_of_scope: int = 0
    ready: int = 0
    blocked: int = 0
    in_progress: int = 0
    parallel_groups: int = 0
    largest_group: int = 0
    in_cycle: tuple[str, ...] = ()
    #: Por que cada task ficou fora do despacho. Sem isto, "Ready 4" parece
    #: escassez de trabalho quando pode ser apenas o teto de workers -- duas
    #: situacoes que exigem acoes opostas do dono.
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
        r.anomalies += (f"provedor falhou: {e}",)
        return r

    r.discovered = len(external_items)

    # Relevante = trabalho que ainda existe. Encerrado sai da conta, e sair da
    # conta e diferente de sumir: o numero de ignoradas fica no relatorio.
    alive: list[ExternalTask] = []
    for t in external_items:
        r.by_status[t.status.value] += 1
        r.by_external_status[t.external_status or "(sem status)"] += 1
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

    # Grafo com a MESMA regra do motor: so vinculo bloqueante vira aresta.
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

    # Quem ja tem alguem trabalhando nela nao e candidata, mas continua no
    # grafo: ela e o bloqueio de outra pessoa, e some-la seria mentir.
    available = [t for t in alive if t.status.available]
    r.in_progress = sum(1 for t in alive if t.status.in_progress)

    candidates = [
        Candidate(task_id=t.key, priority=t.priority,
                  resources=frozenset(t.resources), key=t.key)
        for t in available
    ]
    # Nada concluido: a sombra nao tem historico. O plano mostra o que o motor
    # faria no PRIMEIRO tick contra este board.
    plan = plan(candidates, grafo, completed=set(), running_now={},
                    limits=limits, nomes={t.key: t.key for t in alive})
    r.plan = plan
    r.ready = len(plan.dispatch)
    r.blocked = len(plan.deferred)
    r.in_cycle = plan.in_cycle
    for a in plan.deferred:
        # Agrupar pela CAUSA, nao pelo texto: "recurso ocupado: parent:SG-1" e
        # "recurso ocupado: parent:SG-2" sao o mesmo diagnostico.
        r.deferral_reasons[a.reason.split(":")[0].strip()] += 1

    layers = grafo.layers()
    r.parallel_groups = len(layers)
    r.largest_group = max((len(c) for c in layers), default=0)

    transporte = getattr(provider, "transporte", None)
    r.calls = len(getattr(transporte, "chamadas", []) or [])
    return r


def render(r: ShadowReport, limite_anomalias: int = 12) -> str:
    def linha(rotulo: str, valor) -> str:
        return f"  {rotulo:<28} {valor}"

    output = [
        "TASK PROVIDER SHADOW REPORT",
        "",
        linha("Provider", r.provider),
        linha("Mutations performed", f"{r.mutations}   <- tem de ser 0"),
        "",
        linha("Tasks discovered", r.discovered),
        linha("Relevant", r.relevant),
        linha("Ignored (encerradas)", r.ignored),
        linha("Normalized successfully", r.normalized),
        linha("Normalization errors", r.normalization_errors),
        "",
        linha("Dependencies (bloqueio)", r.dependencies),
        linha("Nao-bloqueantes ignorados", r.non_blocking_links),
        linha("Fora do recorte do JQL", r.dependencies_out_of_scope),
        "",
        linha("Ready", r.ready),
        linha("Blocked", r.blocked),
        linha("Em andamento (terceiros)", r.in_progress),
        linha("Parallel groups", r.parallel_groups),
        linha("Maior grupo paralelo", r.largest_group),
        "",
        linha("Provider calls", r.calls),
        linha("Provider errors", r.provider_errors),
    ]
    if r.mine:
        output.insert(6, linha("Minhas", r.mine))

    output += ["", "  POR SITUACAO NORMALIZADA"]
    for k, v in r.by_status.most_common():
        output.append(f"    {k:<16} {v}")

    output += ["", "  POR STATUS DE ORIGEM"]
    for k, v in r.by_external_status.most_common(10):
        output.append(f"    {k:<16} {v}")

    if r.deferral_reasons:
        output += ["", "  POR QUE AS OUTRAS NAO SAIRAM"]
        for k, v in r.deferral_reasons.most_common():
            output.append(f"    {k:<24} {v}")

    if r.plan and r.plan.dispatch:
        output += ["", f"  DESPACHARIA AGORA ({len(r.plan.dispatch)} em paralelo)"]
        for t in r.plan.dispatch:
            output.append(f"    {t}")

    if r.in_cycle:
        output += ["", "  EM CICLO (ninguem pode comecar)"]
        output += [f"    {t}" for t in r.in_cycle]

    if r.anomalies:
        output += ["", f"  ANOMALIAS ({len(r.anomalies)})"]
        for a in r.anomalies[:limite_anomalias]:
            output.append(f"    {a}")
        if len(r.anomalies) > limite_anomalias:
            output.append(f"    ... mais {len(r.anomalies) - limite_anomalias}")

    return "\n".join(output)
