# -*- coding: utf-8 -*-
"""Orchestrator: o cerebro operacional. Um tick de ponta a ponta.

    recupera -> descobre -> analisa -> planeja -> despacha -> colhe

Cada fase e independente e idempotente. Isso nao e elegancia: e o que faz o motor
sobreviver a interrupcao. Matar o processo entre duas fases deixa o estado
consistente, e o proximo tick continua de onde parou -- o proprio tick recorrente
e o mecanismo de retentativa, sem codigo de retry no meio do fluxo.

**Baseline na primeira passada.** A primeira descoberta de um workspace registra
o backlog e nao despacha nada. Sem isso, ligar o motor num board com dezenas de
tasks abertas dispara uma tempestade de workers -- e o primeiro contato do dono
com o produto vira um incidente.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..core import ids
from ..core.graph import DependencyGraph
from ..core.model import (Dependency, Event, ExternalRef, Run, RunState, Task, Workspace,
                          agora)
from ..core.policy import AutonomyLevel
from ..core.risk import RiskEngine, RiskLevel
from ..core.scheduling import Candidata, Limites, Plano, planeja
from ..core.states import TaskState
from ..ports import AdapterErro
from ..ports.support import NotificationProvider
from ..ports.tasks import ExternalTask, SituacaoExterna, TaskProvider
from ..ports.workspace import AgentRunner, RunRequest, WorkspaceProvider
from ..ports.store import Store
from . import escalation, supervisor
from .gate import Escopo, Gate


def _saneia(chave: str) -> str:
    """Chave de task -> nome de diretorio seguro.

    Chave de fornecedor aceita coisas que caminho nao aceita (barra, dois
    pontos, espaco). Sem sanear, a area de uma task some no meio de uma
    arvore inesperada -- ou, pior, escapa da raiz.
    """
    limpo = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in chave)
    return limpo.strip("-.") or "sem-chave"


@dataclass(slots=True)
class Relatorio:
    """O que o tick fez. Curto de proposito: e o que o dono le."""
    workspace: str
    descobertas: int = 0
    analisadas: int = 0
    despachadas: tuple[str, ...] = ()
    concluidas: tuple[str, ...] = ()
    recuperadas: tuple[str, ...] = ()
    escalonadas: tuple[str, ...] = ()
    adiadas: tuple[tuple[str, str], ...] = ()
    ciclos: tuple[str, ...] = ()
    baseline: bool = False
    erros: tuple[str, ...] = ()
    #: O que veio torto da origem. Reportado, nunca corrigido em silencio.
    anomalias: tuple[str, ...] = ()
    #: Tasks que mudaram de situacao na origem desde a ultima passada.
    mudancas: tuple[tuple[str, str, str], ...] = ()
    #: Estavam bloqueadas pela origem e voltaram a fila.
    liberadas: tuple[str, ...] = ()

    def resumo(self) -> str:
        if self.baseline:
            return f"{self.workspace}: baseline com {self.descobertas} tasks; nada despachado"
        partes = []
        if self.recuperadas:
            partes.append(f"{len(self.recuperadas)} recuperada(s)")
        if self.descobertas:
            partes.append(f"{self.descobertas} nova(s)")
        if self.despachadas:
            partes.append(f"{len(self.despachadas)} despachada(s)")
        if self.concluidas:
            partes.append(f"{len(self.concluidas)} concluida(s)")
        if self.mudancas:
            partes.append(f"{len(self.mudancas)} mudaram na origem")
        if self.liberadas:
            partes.append(f"{len(self.liberadas)} liberada(s)")
        if self.escalonadas:
            partes.append(f"{len(self.escalonadas)} precisam de voce")
        if self.erros:
            partes.append(f"{len(self.erros)} erro(s)")
        return f"{self.workspace}: " + (", ".join(partes) if partes else "nada a fazer")


@dataclass(slots=True)
class Orchestrator:
    store: Store
    workspace: Workspace
    tasks_provider: TaskProvider
    area_provider: WorkspaceProvider
    runner: AgentRunner
    gate: Gate
    risco: RiskEngine
    limites: Limites = field(default_factory=Limites)
    orcamento: supervisor.Orcamento = field(default_factory=supervisor.Orcamento)
    notificador: NotificationProvider | None = None
    project_id: str = "prj_default"
    #: Segundos de vida de um lease. O worker renova; se morrer, vence e a
    #: recuperacao devolve a task a fila.
    lease_segundos: int = 900

    # ------------------------------------------------------------------
    def tick(self) -> Relatorio:
        rel = Relatorio(workspace=self.workspace.nome)
        self._anota("tick_inicio", resumo="tick iniciado")
        try:
            self._recupera(rel)
            primeira = self._descobre(rel)
            if primeira:
                rel.baseline = True
                self._anota("baseline", resumo=f"{rel.descobertas} tasks registradas sem despacho")
                return rel
            self._analisa(rel)
            self._despacha(rel)
        except AdapterErro as e:
            # Falha de adapter nunca vira "nao havia trabalho". O tick termina
            # com erro declarado e o proximo tenta de novo.
            rel.erros += (f"adapter: {e}",)
            self._anota("erro", resumo=str(e)[:300])
        self._anota("tick_fim", resumo=rel.resumo())
        return rel

    # ---- 1. recuperacao -------------------------------------------------
    def _recupera(self, rel: Relatorio) -> None:
        """Devolve a fila o trabalho de workers que morreram.

        A prova de que um worker morreu e o lease vencido, nao a ausencia de
        processo: o motor pode estar rodando noutra maquina, e 'nao vejo o
        processo' e um teste que so funciona por acaso.
        """
        vencidos = {l.dono: l for l in self.store.leases_vencidos(self.workspace.id)}
        for run in self.store.runs_ativos(self.workspace.id):
            if run.id not in vencidos:
                continue
            task = self.store.task(run.task_id)
            run.estado = RunState.INTERRUPTED
            run.encerrado_em = agora()
            run.motivo = "lease vencido: worker nao renovou"
            self.store.salva_run(run)
            self.store.solta_lease(run.id, run.id)
            for recurso in (task.recursos if task else ()):
                self.store.solta_lease(recurso, run.id)

            if task is None:
                continue
            destino = supervisor.estado_de_retomada(task.estado)
            if destino is not task.estado:
                self.store.transiciona(task.id, destino, ator="supervisor",
                                       motivo="worker interrompido")
            rel.recuperadas += (task.chave,)
            self._anota("recuperada", task_id=task.id, run_id=run.id,
                        resumo=f"worker morto; task volta como {destino.value}")

    # ---- 2. descoberta --------------------------------------------------
    def _descobre(self, rel: Relatorio) -> bool:
        """Devolve True quando esta foi a primeira passada (baseline)."""
        ja_tinha = bool(self.store.tasks(self.workspace.id))
        externas = self.tasks_provider.list_tasks()

        chaves: dict[str, str] = {}   # chave externa -> task_id
        for e in externas:
            if e.situacao.encerrada:
                # Trabalho terminado na origem nao vira trabalho aqui.
                continue
            task = self.store.task_por_chave(self.workspace.id, self.tasks_provider.nome, e.key)
            if task is None:
                task = self._nasce(e)
                rel.descobertas += 1
                self._anota("descoberta", task_id=task.id,
                            resumo=f"{e.key}: {e.titulo}"[:200],
                            situacao=e.situacao.value, anomalias=list(e.anomalias))
            else:
                self._atualiza(task, e, rel)
            chaves[e.key] = task.id
            if e.anomalias:
                rel.anomalias += tuple(f"{e.key}: {a}" for a in e.anomalias)

        # Vinculos so podem ser ligados depois que todas as tasks existem: o
        # bloqueador pode aparecer depois do bloqueado na mesma lista.
        #
        # **So vinculo BLOQUEANTE vira aresta.** Hierarquia e relacionamento sao
        # informacao, nao ordem de execucao: uma subtarefa nao espera a mae
        # terminar, ela e parte do que a mae e. Tratar os tres como iguais trava
        # o board inteiro -- e num board real hierarquia e relacionamento sao
        # muito mais comuns que bloqueio de verdade.
        for e in externas:
            for v in e.vinculos:
                if not v.bloqueante:
                    continue
                alvo = chaves.get(v.key)
                if alvo and alvo != chaves[e.key]:
                    self.store.liga_dependencia(Dependency(
                        task_id=chaves[e.key], depende_de=alvo, tipo=v.tipo,
                        motivo=f"declarado por {self.tasks_provider.nome}"))
        return not ja_tinha

    def _atualiza(self, task: Task, e: ExternalTask, rel: Relatorio) -> None:
        """Rele o que mudou na origem. O motor NAO herda estado dela.

        A origem manda no que e dela -- titulo, prioridade, quem esta na task.
        O estado do motor e do motor: se a pessoa moveu a issue no board, isso
        muda a *relevancia* do trabalho, nao a etapa em que o worker parou.
        """
        antes = task.dados.get("situacao_externa")
        agora_situacao = e.situacao.value
        task.titulo = e.titulo
        task.prioridade = e.prioridade
        task.dados.update({"situacao_externa": agora_situacao,
                           "estado_externo": e.estado_externo,
                           "rotulos": list(e.rotulos)})
        task.atualizada_em = agora()
        self.store.salva_task(task)
        if antes and antes != agora_situacao:
            rel.mudancas += ((task.chave, antes, agora_situacao),)
            self._anota("mudou_na_origem", task_id=task.id,
                        resumo=f"{antes} -> {agora_situacao} ({e.estado_externo})",
                        de=antes, para=agora_situacao)

    def _nasce(self, e: ExternalTask) -> Task:
        t = Task(
            id=ids.novo(ids.TASK), workspace_id=self.workspace.id,
            project_id=e.projeto or self.project_id, titulo=e.titulo,
            estado=TaskState.DISCOVERED,
            externo=ExternalRef(provider=self.tasks_provider.nome, key=e.key, url=e.url),
            descricao=e.descricao, prioridade=e.prioridade,
            recursos=tuple(e.recursos),
            dados={**dict(e.dados), "situacao_externa": e.situacao.value,
                   "estado_externo": e.estado_externo,
                   "rotulos": list(e.rotulos)})
        self.store.salva_task(t)
        return t

    # ---- 3. analise -----------------------------------------------------
    def _analisa(self, rel: Relatorio) -> None:
        """DISCOVERED -> ANALYZING -> READY, calculando risco e recursos.

        A analise deste milestone e deterministica: risco por sinais declarados e
        recursos por convencao. Um PlannerAgent entra aqui depois, pelo mesmo
        ponto -- ele enriquece `recursos` e `dependencias`, e o resto do motor nao
        muda.
        """
        # Trabalho que a origem diz estar bloqueado por terceiros pode voltar a
        # fila quando a origem mudar de ideia. Este e o unico caminho de volta:
        # task bloqueada por FALHA nao e desbloqueada por status externo.
        for t in self.store.tasks(self.workspace.id, [TaskState.BLOCKED]):
            if t.dados.get("bloqueada_por") != "origem":
                continue
            if self._situacao(t).disponivel:
                t.dados.pop("bloqueada_por", None)
                self.store.salva_task(t)
                self.store.transiciona(t.id, TaskState.READY, ator="planner",
                                       motivo="a origem liberou o trabalho")
                rel.liberadas += (t.chave,)

        for t in self.store.tasks(self.workspace.id, [TaskState.DISCOVERED]):
            self.store.transiciona(t.id, TaskState.ANALYZING, ator="planner",
                                   motivo="analise inicial")
            avaliacao = self.risco.avalia({
                "acao": "task.analyze",
                "ambiente": "local",
                "caminhos": list(t.recursos) + [t.titulo],
                "categoria": "task",
            })
            t = self.store.task(t.id)
            t.risco = avaliacao.nivel
            if not t.recursos:
                # Sem informacao melhor, a task segura o projeto inteiro. Errar
                # para o lado de nao paralelizar e barato; errar para o outro
                # produz dois workers no mesmo arquivo.
                t.recursos = (f"project:{t.project_id}",)
            # **A origem decide se o trabalho esta disponivel.**
            #
            # Sem esta guarda o motor despacha um worker sobre uma task que ja
            # tem gente nela -- medido contra o board real: duas issues em CODING
            # foram despachadas no primeiro tick. Um agente por cima de uma
            # pessoa e o pior defeito que este marco poderia deixar passar, e
            # nenhum teste com dado inventado o teria encontrado.
            situacao = self._situacao(t)
            if not situacao.disponivel:
                t.dados["bloqueada_por"] = "origem"
                self.store.salva_task(t)
                self.store.transiciona(
                    t.id, TaskState.BLOCKED, ator="planner",
                    motivo=f"a origem diz {t.dados.get('estado_externo') or situacao.value}")
                rel.analisadas += 1
                continue

            self.store.salva_task(t)
            self.store.transiciona(t.id, TaskState.READY, ator="planner",
                                   motivo=f"risco {avaliacao.nivel.name}",
                                   dados={"sinais": list(avaliacao.motivos)})
            rel.analisadas += 1

    # ---- 4/5. plano e despacho ------------------------------------------
    def _grafo(self) -> DependencyGraph:
        g = DependencyGraph()
        for t in self.store.tasks(self.workspace.id):
            g.adiciona(t.id)
        for d in self.store.dependencias(self.workspace.id):
            g.liga(d.task_id, d.depende_de)
        return g

    def plano(self) -> Plano:
        """Exposto para que a UI e os testes vejam a decisao sem executa-la."""
        todas = self.store.tasks(self.workspace.id)
        concluidas = {t.id for t in todas if t.estado is TaskState.DONE}
        ativos = self.store.runs_ativos(self.workspace.id)
        por_id = {t.id: t for t in todas}
        em_execucao = {
            r.task_id: frozenset(por_id[r.task_id].recursos)
            for r in ativos if r.task_id in por_id
        }
        candidatas = [
            Candidata(task_id=t.id, prioridade=t.prioridade,
                      recursos=frozenset(t.recursos), chave=t.chave)
            for t in todas if t.estado is TaskState.READY
        ]
        hoje = agora().strftime("%Y-%m-%d")
        return planeja(candidatas, self._grafo(), concluidas, em_execucao,
                       self.limites, self.store.conta_despachos(self.workspace.id, hoje),
                       nomes={t.id: t.chave for t in todas})

    def _despacha(self, rel: Relatorio) -> None:
        p = self.plano()
        rel.adiadas = tuple((self.store.task(a.task_id).chave, a.motivo) for a in p.adiadas)
        rel.ciclos = tuple(self.store.task(i).chave for i in p.em_ciclo)

        if p.em_ciclo:
            self._escala_ciclo(p, rel)

        for task_id in p.despachar:
            try:
                self._roda(task_id, rel)
            except Exception as e:   # noqa: BLE001 - um worker nao derruba o tick
                rel.erros += (f"{task_id}: {type(e).__name__}: {e}"[:200],)
                self._anota("erro", task_id=task_id, resumo=str(e)[:300])

    def _roda(self, task_id: str, rel: Relatorio) -> None:
        task = self.store.task(task_id)
        run = Run(id=ids.novo(ids.RUN), task_id=task.id, workspace_id=self.workspace.id,
                  agente="coder", estado=RunState.RUNNING)

        # Travar ANTES de transicionar: se a trava falhar, a task nao pode ter
        # saido de READY -- caso contrario ela fica ASSIGNED sem dono.
        travados: list[str] = []
        for recurso in task.recursos:
            if self.store.adquire_lease(recurso, run.id, self.workspace.id,
                                        self.lease_segundos) is None:
                for r in travados:
                    self.store.solta_lease(r, run.id)
                self._anota("adiada", task_id=task.id,
                            resumo=f"recurso {recurso} ficou ocupado entre o plano e o despacho")
                return
            travados.append(recurso)

        self.store.transiciona(task.id, TaskState.ASSIGNED, ator="orchestrator",
                               motivo=f"run {run.id}")
        area = self.area_provider.prepare(_saneia(task.chave),
                                          branch=f"regente/{task.chave.lower()}")
        run.workspace_path, run.branch = area.caminho, area.branch
        self.store.salva_run(run)
        self.store.marca_despacho(self.workspace.id, agora().strftime("%Y-%m-%d"))
        self.store.transiciona(task.id, TaskState.IMPLEMENTING, ator=run.agente,
                               motivo="worker iniciou")
        rel.despachadas += (task.chave,)
        self._anota("despachada", task_id=task.id, run_id=run.id,
                    resumo=f"{run.agente} em {area.caminho}")

        pedido = RunRequest(
            run_id=run.id, task_id=task.id, agente=run.agente,
            objetivo=task.titulo, area=area,
            contexto={"descricao": task.descricao, "chave": task.chave,
                      "risco": task.risco.name if task.risco else "LOW",
                      "recursos": list(task.recursos)},
            limite_iteracoes=self.orcamento.max_iteracoes,
            limite_tool_calls=self.orcamento.max_tool_calls,
            limite_custo_usd=self.orcamento.max_custo_usd,
            limite_segundos=self.orcamento.max_segundos)

        try:
            resultado = self.runner.run(pedido)
        except Exception as e:   # noqa: BLE001
            resultado = None
            run.motivo = f"{type(e).__name__}: {e}"[:300]

        self._colhe(task.id, run, resultado, travados, rel)

    # ---- 6. colheita ----------------------------------------------------
    def _colhe(self, task_id: str, run: Run, resultado, travados: list[str],
               rel: Relatorio) -> None:
        for r in travados:
            self.store.solta_lease(r, run.id)
        run.encerrado_em = agora()

        if resultado is None:
            self._falhou(task_id, run, run.motivo or "worker levantou excecao", rel)
            return

        run.custo_usd, run.tokens = resultado.custo_usd, resultado.tokens
        run.chamadas_tool, run.iteracoes = resultado.chamadas_tool, resultado.iteracoes

        if resultado.desfecho == "precisa_humano":
            run.estado, run.motivo = RunState.ABORTED, resultado.resumo
            self.store.salva_run(run)
            self.store.transiciona(task_id, TaskState.WAITING_HUMAN, ator=run.agente,
                                   motivo=resultado.resumo)
            self._escala(task_id, run, resultado, rel)
            return

        if resultado.ok:
            run.estado, run.motivo = RunState.SUCCEEDED, resultado.resumo
            self.store.salva_run(run)
            task = self.store.task(task_id)
            self.store.transiciona(task.id, TaskState.TESTING, ator=run.agente,
                                   motivo=resultado.resumo)
            rel.concluidas += (task.chave,)
            self._anota("implementada", task_id=task.id, run_id=run.id,
                        resumo=resultado.resumo[:200])
            return

        self._falhou(task_id, run, resultado.resumo, rel, desfecho=resultado.desfecho)

    def _falhou(self, task_id: str, run: Run, motivo: str, rel: Relatorio,
                desfecho: str = "erro") -> None:
        run.estado, run.motivo = RunState.FAILED, motivo
        self.store.salva_run(run)

        task = self.store.task(task_id)
        task.tentativas += 1
        self.store.salva_task(task)

        # A task passa por FAILED antes de qualquer recuperacao. Pular esse
        # degrau economizaria uma linha e apagaria da timeline o fato de que
        # houve falha -- que e exatamente o que alguem procura quando a mesma
        # task volta pela terceira vez.
        self.store.transiciona(task.id, TaskState.FAILED, ator=run.agente, motivo=motivo)

        degrau = supervisor.proximo_degrau(task, self.orcamento)
        if supervisor.sem_progresso(task, self.store.runs_da_task(task.id)).parar:
            degrau = "escalar"

        if degrau == "escalar":
            self.store.transiciona(task.id, TaskState.WAITING_HUMAN, ator="supervisor",
                                   motivo=motivo)
            self._escala_falha(task, run, motivo, degrau, rel)
        else:
            self.store.transiciona(task.id, TaskState.READY, ator="supervisor",
                                   motivo=f"{degrau} apos falha: {motivo}"[:300])
            self._anota("falhou", task_id=task.id, run_id=run.id,
                        resumo=f"{motivo[:160]} -> {degrau}")

    # ---- escalonamento ---------------------------------------------------
    def _escala(self, task_id: str, run: Run, resultado, rel: Relatorio) -> None:
        task = self.store.task(task_id)
        p = resultado.pergunta or {}
        approval = escalation.monta(
            task=task,
            o_que_aconteceu=p.get("o_que_aconteceu", resultado.resumo),
            por_que_importa=p.get("por_que_importa", "o agente parou sem conseguir decidir sozinho"),
            tentativas=tuple(p.get("tentativas", ())),
            recomendacao=p.get("recomendacao", escalation.SEGUIR.id),
            risco=task.risco or RiskLevel.MEDIUM,
            run_id=run.id)
        self._publica(approval, task, rel)

    def _escala_falha(self, task: Task, run: Run, motivo: str, degrau: str,
                      rel: Relatorio) -> None:
        tentativas = tuple(
            f"{r.agente}: {r.motivo or r.estado.value}"[:160]
            for r in self.store.runs_da_task(task.id)[-3:])
        approval = escalation.monta(
            task=task,
            o_que_aconteceu=f"{task.tentativas} tentativas falharam. Ultima: {motivo}"[:400],
            por_que_importa="a escada de recuperacao acabou; sem decisao sua a task nao sai do lugar",
            tentativas=tentativas,
            recomendacao=escalation.INVESTIGAR.id,
            risco=task.risco or RiskLevel.MEDIUM,
            run_id=run.id)
        self._publica(approval, task, rel)

    def _escala_ciclo(self, p: Plano, rel: Relatorio) -> None:
        chaves = [self.store.task(i).chave for i in p.em_ciclo]
        task = self.store.task(p.em_ciclo[0])
        if any(a.task_id == task.id for a in self.store.approvals_abertos(self.workspace.id)):
            return   # ja perguntei; nao repito a cada tick
        approval = escalation.monta(
            task=task,
            o_que_aconteceu=f"dependencias circulares entre {', '.join(chaves)}",
            por_que_importa="nenhuma dessas tasks pode comecar enquanto o ciclo existir",
            tentativas=("montei o grafo a partir dos vinculos declarados na origem",),
            recomendacao=escalation.INVESTIGAR.id,
            risco=RiskLevel.MEDIUM)
        self._publica(approval, task, rel)

    def _publica(self, approval, task: Task, rel: Relatorio) -> None:
        self.store.abre_approval(approval)
        rel.escalonadas += (task.chave,)
        if self.notificador:
            b = escalation.briefing(approval, task)
            self.notificador.notify(f"{task.chave} precisa de voce", b.o_que_aconteceu,
                                    urgencia="alta" if approval.risco >= RiskLevel.HIGH else "normal")

    # ---- utilidades ------------------------------------------------------
    def _anota(self, tipo: str, resumo: str = "", task_id: str | None = None,
               run_id: str | None = None, **dados: Any) -> None:
        self.store.anota(Event(
            id=ids.novo(ids.EVENT), workspace_id=self.workspace.id, tipo=tipo,
            task_id=task_id, run_id=run_id, resumo=resumo, dados=dados))

    def _situacao(self, t: Task) -> SituacaoExterna:
        """A situacao na origem, reconstruida do que foi persistido.

        Provedor sem nocao de situacao devolve DESCONHECIDA -- que NAO e
        disponivel. Conservador de proposito: nao saber se alguem esta na task
        precisa custar um adiamento, nunca um atropelo.
        """
        bruto = t.dados.get("situacao_externa")
        try:
            return SituacaoExterna(bruto)
        except ValueError:
            return SituacaoExterna.DESCONHECIDA

    def escopo(self, agente: str = "engine", task_id: str | None = None,
               run_id: str | None = None, project: str = "*") -> Escopo:
        return Escopo(workspace_id=self.workspace.id, workspace=self.workspace.nome,
                      project=project, autonomia=self.workspace.autonomia_maxima,
                      agente=agente, task_id=task_id, run_id=run_id)
