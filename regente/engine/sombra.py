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
from ..core.scheduling import Candidata, Limites, Plano, planeja
from ..ports import AdapterErro
from ..ports.tasks import ExternalTask, SituacaoExterna, TaskProvider


@dataclass(slots=True)
class RelatorioSombra:
    provedor: str
    descobertas: int = 0
    relevantes: int = 0
    ignoradas: int = 0
    normalizadas: int = 0
    erros_de_normalizacao: int = 0
    por_situacao: Counter = field(default_factory=Counter)
    por_status_externo: Counter = field(default_factory=Counter)
    dependencias: int = 0
    vinculos_nao_bloqueantes: int = 0
    #: Dependencia declarada para uma task que a consulta nao trouxe. Nao e erro
    #: do motor: e o recorte do JQL menor que o grafo real, e precisa aparecer.
    dependencias_fora_do_recorte: int = 0
    prontas: int = 0
    bloqueadas: int = 0
    em_andamento: int = 0
    grupos_paralelos: int = 0
    maior_grupo: int = 0
    em_ciclo: tuple[str, ...] = ()
    #: Por que cada task ficou fora do despacho. Sem isto, "Ready 4" parece
    #: escassez de trabalho quando pode ser apenas o teto de workers -- duas
    #: situacoes que exigem acoes opostas do dono.
    motivos_de_adiamento: Counter = field(default_factory=Counter)
    anomalias: tuple[str, ...] = ()
    chamadas: int = 0
    erros_do_provedor: int = 0
    mutacoes: int = 0
    plano: Plano | None = None
    minhas: int = 0


def executa(
    provedor: TaskProvider,
    limites: Limites,
    filtro: dict | None = None,
    eu: str | None = None,
) -> RelatorioSombra:
    r = RelatorioSombra(provedor=provedor.nome)

    try:
        externas: list[ExternalTask] = provedor.list_tasks(filtro)
    except AdapterErro as e:
        r.erros_do_provedor += 1
        r.anomalias += (f"provedor falhou: {e}",)
        return r

    r.descobertas = len(externas)

    # Relevante = trabalho que ainda existe. Encerrado sai da conta, e sair da
    # conta e diferente de sumir: o numero de ignoradas fica no relatorio.
    vivas: list[ExternalTask] = []
    for t in externas:
        r.por_situacao[t.situacao.value] += 1
        r.por_status_externo[t.estado_externo or "(sem status)"] += 1
        if t.situacao.encerrada:
            r.ignoradas += 1
            continue
        vivas.append(t)
        r.normalizadas += 1
        if t.anomalias:
            r.anomalias += tuple(f"{t.key}: {a}" for a in t.anomalias)
        if eu and t.responsavel == eu:
            r.minhas += 1
    r.relevantes = len(vivas)

    # Grafo com a MESMA regra do motor: so vinculo bloqueante vira aresta.
    conhecidas = {t.key for t in vivas}
    grafo = DependencyGraph()
    for t in vivas:
        grafo.adiciona(t.key)
    for t in vivas:
        for v in t.vinculos:
            if not v.bloqueante:
                r.vinculos_nao_bloqueantes += 1
                continue
            if v.key not in conhecidas:
                r.dependencias_fora_do_recorte += 1
                continue
            grafo.liga(t.key, v.key)
            r.dependencias += 1

    # Quem ja tem alguem trabalhando nela nao e candidata, mas continua no
    # grafo: ela e o bloqueio de outra pessoa, e some-la seria mentir.
    disponiveis = [t for t in vivas if t.situacao.disponivel]
    r.em_andamento = sum(1 for t in vivas if t.situacao.em_andamento)

    candidatas = [
        Candidata(task_id=t.key, prioridade=t.prioridade,
                  recursos=frozenset(t.recursos), chave=t.key)
        for t in disponiveis
    ]
    # Nada concluido: a sombra nao tem historico. O plano mostra o que o motor
    # faria no PRIMEIRO tick contra este board.
    plano = planeja(candidatas, grafo, concluidas=set(), em_execucao={},
                    limites=limites, nomes={t.key: t.key for t in vivas})
    r.plano = plano
    r.prontas = len(plano.despachar)
    r.bloqueadas = len(plano.adiadas)
    r.em_ciclo = plano.em_ciclo
    for a in plano.adiadas:
        # Agrupar pela CAUSA, nao pelo texto: "recurso ocupado: parent:SG-1" e
        # "recurso ocupado: parent:SG-2" sao o mesmo diagnostico.
        r.motivos_de_adiamento[a.motivo.split(":")[0].strip()] += 1

    camadas = grafo.camadas()
    r.grupos_paralelos = len(camadas)
    r.maior_grupo = max((len(c) for c in camadas), default=0)

    transporte = getattr(provedor, "transporte", None)
    r.chamadas = len(getattr(transporte, "chamadas", []) or [])
    return r


def texto(r: RelatorioSombra, limite_anomalias: int = 12) -> str:
    def linha(rotulo: str, valor) -> str:
        return f"  {rotulo:<28} {valor}"

    saida = [
        "TASK PROVIDER SHADOW REPORT",
        "",
        linha("Provider", r.provedor),
        linha("Mutations performed", f"{r.mutacoes}   <- tem de ser 0"),
        "",
        linha("Tasks discovered", r.descobertas),
        linha("Relevant", r.relevantes),
        linha("Ignored (encerradas)", r.ignoradas),
        linha("Normalized successfully", r.normalizadas),
        linha("Normalization errors", r.erros_de_normalizacao),
        "",
        linha("Dependencies (bloqueio)", r.dependencias),
        linha("Nao-bloqueantes ignorados", r.vinculos_nao_bloqueantes),
        linha("Fora do recorte do JQL", r.dependencias_fora_do_recorte),
        "",
        linha("Ready", r.prontas),
        linha("Blocked", r.bloqueadas),
        linha("Em andamento (terceiros)", r.em_andamento),
        linha("Parallel groups", r.grupos_paralelos),
        linha("Maior grupo paralelo", r.maior_grupo),
        "",
        linha("Provider calls", r.chamadas),
        linha("Provider errors", r.erros_do_provedor),
    ]
    if r.minhas:
        saida.insert(6, linha("Minhas", r.minhas))

    saida += ["", "  POR SITUACAO NORMALIZADA"]
    for k, v in r.por_situacao.most_common():
        saida.append(f"    {k:<16} {v}")

    saida += ["", "  POR STATUS DE ORIGEM"]
    for k, v in r.por_status_externo.most_common(10):
        saida.append(f"    {k:<16} {v}")

    if r.motivos_de_adiamento:
        saida += ["", "  POR QUE AS OUTRAS NAO SAIRAM"]
        for k, v in r.motivos_de_adiamento.most_common():
            saida.append(f"    {k:<24} {v}")

    if r.plano and r.plano.despachar:
        saida += ["", f"  DESPACHARIA AGORA ({len(r.plano.despachar)} em paralelo)"]
        for t in r.plano.despachar:
            saida.append(f"    {t}")

    if r.em_ciclo:
        saida += ["", "  EM CICLO (ninguem pode comecar)"]
        saida += [f"    {t}" for t in r.em_ciclo]

    if r.anomalias:
        saida += ["", f"  ANOMALIAS ({len(r.anomalias)})"]
        for a in r.anomalias[:limite_anomalias]:
            saida.append(f"    {a}")
        if len(r.anomalias) > limite_anomalias:
            saida.append(f"    ... mais {len(r.anomalias) - limite_anomalias}")

    return "\n".join(saida)
