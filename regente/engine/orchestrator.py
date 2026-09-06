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
                          now)
from ..core.policy import AutonomyLevel
from ..core.risk import RiskEngine, RiskLevel
from ..core.scheduling import Candidate, Limits, Plan, plan
from ..core.states import TaskState
from ..ports import AdapterError
from ..ports.support import NotificationProvider
from ..ports.tasks import ExternalTask, ExternalStatus, TaskProvider
from ..ports.workspace import AgentRunner, RunRequest, WorkspaceProvider
from ..ports.store import Store
from . import escalation, supervisor
from .gate import Scope, Gate


def _sanitize(key: str) -> str:
    """Chave de task -> nome de diretorio seguro.

    Chave de fornecedor aceita coisas que caminho nao aceita (barra, dois
    pontos, espaco). Sem sanear, a area de uma task some no meio de uma
    arvore inesperada -- ou, pior, escapa da raiz.
    """
    limpo = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in key)
    return limpo.strip("-.") or "sem-chave"


@dataclass(slots=True)
class TickReport:
    """O que o tick fez. Curto de proposito: e o que o dono le."""
    workspace: str
    discovered: int = 0
    analyzed: int = 0
    dispatched: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    recovered: tuple[str, ...] = ()
    escalated: tuple[str, ...] = ()
    deferred: tuple[tuple[str, str], ...] = ()
    cycles: tuple[str, ...] = ()
    baseline: bool = False
    errors: tuple[str, ...] = ()
    #: O que veio torto da origem. Reportado, nunca corrigido em silencio.
    anomalies: tuple[str, ...] = ()
    #: Tasks que mudaram de situacao na origem desde a ultima passada.
    changes: tuple[tuple[str, str, str], ...] = ()
    #: Estavam bloqueadas pela origem e voltaram a fila.
    unblocked_tasks: tuple[str, ...] = ()

    def summary(self) -> str:
        if self.baseline:
            return f"{self.workspace}: baseline com {self.discovered} tasks; nada despachado"
        partes = []
        if self.recovered:
            partes.append(f"{len(self.recovered)} recuperada(s)")
        if self.discovered:
            partes.append(f"{self.discovered} nova(s)")
        if self.dispatched:
            partes.append(f"{len(self.dispatched)} despachada(s)")
        if self.completed:
            partes.append(f"{len(self.completed)} concluida(s)")
        if self.changes:
            partes.append(f"{len(self.changes)} mudaram na origem")
        if self.unblocked_tasks:
            partes.append(f"{len(self.unblocked_tasks)} liberada(s)")
        if self.escalated:
            partes.append(f"{len(self.escalated)} precisam de voce")
        if self.errors:
            partes.append(f"{len(self.errors)} error(s)")
        return f"{self.workspace}: " + (", ".join(partes) if partes else "nada a fazer")


@dataclass(slots=True)
class Orchestrator:
    store: Store
    workspace: Workspace
    tasks_provider: TaskProvider
    area_provider: WorkspaceProvider
    runner: AgentRunner
    gate: Gate
    risk: RiskEngine
    limits: Limits = field(default_factory=Limits)
    budget: supervisor.Budget = field(default_factory=supervisor.Budget)
    notificador: NotificationProvider | None = None
    project_id: str = "prj_default"
    #: Segundos de vida de um lease. O worker renova; se morrer, vence e a
    #: recuperacao devolve a task a fila.
    lease_seconds: int = 900

    # ------------------------------------------------------------------
    def tick(self) -> TickReport:
        rel = TickReport(workspace=self.workspace.name)
        self._record("tick_inicio", summary="tick iniciado")
        try:
            self._recover(rel)
            first_pass = self._discover(rel)
            if first_pass:
                rel.baseline = True
                self._record("baseline", summary=f"{rel.discovered} tasks registradas sem despacho")
                return rel
            self._analyze(rel)
            self._dispatch(rel)
        except AdapterError as e:
            # Falha de adapter nunca vira "nao havia trabalho". O tick termina
            # com error declarado e o proximo tenta de novo.
            rel.errors += (f"adapter: {e}",)
            self._record("error", summary=str(e)[:300])
        self._record("tick_fim", summary=rel.summary())
        return rel

    # ---- 1. recuperacao -------------------------------------------------
    def _recover(self, rel: TickReport) -> None:
        """Devolve a fila o trabalho de workers que morreram.

        A prova de que um worker morreu e o lease vencido, nao a ausencia de
        processo: o motor pode estar rodando noutra maquina, e 'nao vejo o
        processo' e um teste que so funciona por acaso.
        """
        expired = {l.owner: l for l in self.store.expired_leases(self.workspace.id)}
        for run in self.store.active_runs(self.workspace.id):
            if run.id not in expired:
                continue
            task = self.store.task(run.task_id)
            run.state = RunState.INTERRUPTED
            run.ended_at = now()
            run.reason = "lease vencido: worker nao renovou"
            self.store.save_run(run)
            self.store.release_lease(run.id, run.id, self.workspace.id)
            for resource in (task.resources if task else ()):
                self.store.release_lease(resource, run.id, self.workspace.id)

            if task is None:
                continue
            destination = supervisor.resume_state(task.state)
            if destination is not task.state:
                self.store.transition(task.id, destination, actor="supervisor",
                                       reason="worker interrompido")
            rel.recovered += (task.key,)
            self._record("recuperada", task_id=task.id, run_id=run.id,
                        summary=f"worker morto; task volta como {destination.value}")

    # ---- 2. descoberta --------------------------------------------------
    def _discover(self, rel: TickReport) -> bool:
        """Devolve True quando esta foi a primeira passada (baseline)."""
        ja_tinha = bool(self.store.tasks(self.workspace.id))
        external_items = self.tasks_provider.list_tasks()

        chaves: dict[str, str] = {}   # chave externa -> task_id
        for e in external_items:
            if e.status.finished:
                # Trabalho terminado na origem nao vira trabalho aqui.
                continue
            task = self.store.task_by_key(self.workspace.id, self.tasks_provider.name, e.key)
            if task is None:
                task = self._create_task(e)
                rel.discovered += 1
                self._record("descoberta", task_id=task.id,
                            summary=f"{e.key}: {e.title}"[:200],
                            status=e.status.value, anomalies=list(e.anomalies))
            else:
                self._refresh(task, e, rel)
            chaves[e.key] = task.id
            if e.anomalies:
                rel.anomalies += tuple(f"{e.key}: {a}" for a in e.anomalies)

        # Vinculos so podem ser ligados depois que todas as tasks existem: o
        # bloqueador pode aparecer depois do bloqueado na mesma lista.
        #
        # **So vinculo BLOQUEANTE vira aresta.** Hierarquia e relacionamento sao
        # informacao, nao ordem de execucao: uma subtarefa nao espera a mae
        # terminar, ela e parte do que a mae e. Tratar os tres como iguais trava
        # o board inteiro -- e num board real hierarquia e relacionamento sao
        # muito mais comuns que bloqueio de verdade.
        for e in external_items:
            for v in e.links:
                if not v.blocking:
                    continue
                target = chaves.get(v.key)
                if target and target != chaves[e.key]:
                    self.store.link_dependency(Dependency(
                        task_id=chaves[e.key], depends_on=target, kind=v.kind,
                        reason=f"declarado por {self.tasks_provider.name}"))
        return not ja_tinha

    def _refresh(self, task: Task, e: ExternalTask, rel: TickReport) -> None:
        """Rele o que mudou na origem. O motor NAO herda estado dela.

        A origem manda no que e dela -- titulo, prioridade, quem esta na task.
        O estado do motor e do motor: se a pessoa moveu a issue no board, isso
        muda a *relevancia* do trabalho, nao a etapa em que o worker parou.
        """
        before = task.data.get("situacao_externa")
        current_status = e.status.value
        task.title = e.title
        task.priority = e.priority
        task.data.update({"situacao_externa": current_status,
                           "estado_externo": e.external_status,
                           "rotulos": list(e.labels)})
        task.updated_at = now()
        self.store.save_task(task)
        if before and before != current_status:
            rel.changes += ((task.key, before, current_status),)
            self._record("mudou_na_origem", task_id=task.id,
                        summary=f"{before} -> {current_status} ({e.external_status})",
                        de=before, to_state=current_status)

    def _create_task(self, e: ExternalTask) -> Task:
        t = Task(
            id=ids.new_id(ids.TASK), workspace_id=self.workspace.id,
            project_id=e.project or self.project_id, title=e.title,
            state=TaskState.DISCOVERED,
            externo=ExternalRef(provider=self.tasks_provider.name, key=e.key, url=e.url),
            description=e.description, priority=e.priority,
            resources=tuple(e.resources),
            data={**dict(e.data), "situacao_externa": e.status.value,
                   "estado_externo": e.external_status,
                   "rotulos": list(e.labels)})
        self.store.save_task(t)
        return t

    # ---- 3. analise -----------------------------------------------------
    def _analyze(self, rel: TickReport) -> None:
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
            if t.data.get("bloqueada_por") != "origem":
                continue
            if self._status_of(t).available:
                t.data.pop("bloqueada_por", None)
                self.store.save_task(t)
                self.store.transition(t.id, TaskState.READY, actor="planner",
                                       reason="a origem liberou o trabalho")
                rel.unblocked_tasks += (t.key,)

        for t in self.store.tasks(self.workspace.id, [TaskState.DISCOVERED]):
            self.store.transition(t.id, TaskState.ANALYZING, actor="planner",
                                   reason="analise inicial")
            assessment = self.risk.assess({
                "action": "task.analyze",
                "environment": "local",
                "paths": list(t.resources) + [t.title],
                "category": "task",
            })
            t = self.store.task(t.id)
            t.risk = assessment.level
            if not t.resources:
                # Sem informacao melhor, a task segura o projeto inteiro. Errar
                # para o lado de nao paralelizar e barato; errar para o outro
                # produz dois workers no mesmo arquivo.
                t.resources = (f"project:{t.project_id}",)
            # **A origem decide se o trabalho esta disponivel.**
            #
            # Sem esta guarda o motor despacha um worker sobre uma task que ja
            # tem gente nela -- medido contra o board real: duas issues em CODING
            # foram despachadas no primeiro tick. Um agente por cima de uma
            # pessoa e o pior defeito que este marco poderia deixar passar, e
            # nenhum teste com dado inventado o teria encontrado.
            status = self._status_of(t)
            if not status.available:
                t.data["bloqueada_por"] = "origem"
                self.store.save_task(t)
                self.store.transition(
                    t.id, TaskState.BLOCKED, actor="planner",
                    reason=f"a origem diz {t.data.get('estado_externo') or status.value}")
                rel.analyzed += 1
                continue

            self.store.save_task(t)
            self.store.transition(t.id, TaskState.READY, actor="planner",
                                   reason=f"risco {assessment.level.name}",
                                   data={"sinais": list(assessment.reasons)})
            rel.analyzed += 1

    # ---- 4/5. plano e despacho ------------------------------------------
    def _graph(self) -> DependencyGraph:
        g = DependencyGraph()
        for t in self.store.tasks(self.workspace.id):
            g.add(t.id)
        for d in self.store.dependencies(self.workspace.id):
            g.link(d.task_id, d.depends_on)
        return g

    def plan(self) -> Plan:
        """Exposto para que a UI e os testes vejam a decisao sem executa-la."""
        todas = self.store.tasks(self.workspace.id)
        completed = {t.id for t in todas if t.state is TaskState.DONE}
        ativos = self.store.active_runs(self.workspace.id)
        por_id = {t.id: t for t in todas}
        running_now = {
            r.task_id: frozenset(por_id[r.task_id].resources)
            for r in ativos if r.task_id in por_id
        }
        candidates = [
            Candidate(task_id=t.id, priority=t.priority,
                      resources=frozenset(t.resources), key=t.key)
            for t in todas if t.state is TaskState.READY
        ]
        hoje = now().strftime("%Y-%m-%d")
        return plan(candidates, self._graph(), completed, running_now,
                       self.limits, self.store.dispatch_count(self.workspace.id, hoje),
                       nomes={t.id: t.key for t in todas})

    def _dispatch(self, rel: TickReport) -> None:
        p = self.plan()
        rel.deferred = tuple((self.store.task(a.task_id).key, a.reason) for a in p.deferred)
        rel.cycles = tuple(self.store.task(i).key for i in p.in_cycle)

        if p.in_cycle:
            self._escalate_cycle(p, rel)

        for task_id in p.dispatch:
            try:
                self._run_one(task_id, rel)
            except Exception as e:   # noqa: BLE001 - um worker nao derruba o tick
                rel.errors += (f"{task_id}: {type(e).__name__}: {e}"[:200],)
                self._record("error", task_id=task_id, summary=str(e)[:300])

    def _run_one(self, task_id: str, rel: TickReport) -> None:
        task = self.store.task(task_id)
        run = Run(id=ids.new_id(ids.RUN), task_id=task.id, workspace_id=self.workspace.id,
                  agent="coder", state=RunState.RUNNING)

        # Travar ANTES de transicionar: se a trava falhar, a task nao pode ter
        # saido de READY -- caso contrario ela fica ASSIGNED sem dono.
        held: list[str] = []
        for resource in task.resources:
            if self.store.acquire_lease(resource, run.id, self.workspace.id,
                                        self.lease_seconds) is None:
                for r in held:
                    self.store.release_lease(r, run.id, self.workspace.id)
                self._record("adiada", task_id=task.id,
                            summary=f"recurso {resource} ficou ocupado entre o plano e o despacho")
                return
            held.append(resource)

        self.store.transition(task.id, TaskState.ASSIGNED, actor="orchestrator",
                               reason=f"run {run.id}")
        area = self.area_provider.prepare(_sanitize(task.key),
                                          branch=f"regente/{task.key.lower()}")
        run.workspace_path, run.branch = area.path, area.branch
        self.store.save_run(run)
        self.store.mark_dispatch(self.workspace.id, now().strftime("%Y-%m-%d"))
        self.store.transition(task.id, TaskState.IMPLEMENTING, actor=run.agent,
                               reason="worker iniciou")
        rel.dispatched += (task.key,)
        self._record("despachada", task_id=task.id, run_id=run.id,
                    summary=f"{run.agent} em {area.path}")

        request = RunRequest(
            run_id=run.id, task_id=task.id, agent=run.agent,
            goal=task.title, area=area,
            contexto={"descricao": task.description, "chave": task.key,
                      "risco": task.risk.name if task.risk else "LOW",
                      "resources": list(task.resources)},
            limit_iterations=self.budget.max_iterations,
            limit_tool_calls=self.budget.max_tool_calls,
            limit_cost_usd=self.budget.max_cost_usd,
            limit_seconds=self.budget.max_seconds)

        try:
            resultado = self.runner.run(request)
        except Exception as e:   # noqa: BLE001
            resultado = None
            run.reason = f"{type(e).__name__}: {e}"[:300]

        self._collect(task.id, run, resultado, held, rel)

    # ---- 6. colheita ----------------------------------------------------
    def _collect(self, task_id: str, run: Run, resultado, held: list[str],
               rel: TickReport) -> None:
        for r in held:
            self.store.release_lease(r, run.id, self.workspace.id)
        run.ended_at = now()

        if resultado is None:
            self._failed(task_id, run, run.reason or "worker levantou excecao", rel)
            return

        run.cost_usd, run.tokens = resultado.cost_usd, resultado.tokens
        run.tool_calls, run.iterations = resultado.tool_calls, resultado.iterations

        if resultado.outcome == "precisa_humano":
            run.state, run.reason = RunState.ABORTED, resultado.summary
            self.store.save_run(run)
            self.store.transition(task_id, TaskState.WAITING_HUMAN, actor=run.agent,
                                   reason=resultado.summary)
            self._escalate(task_id, run, resultado, rel)
            return

        if resultado.ok:
            run.state, run.reason = RunState.SUCCEEDED, resultado.summary
            self.store.save_run(run)
            task = self.store.task(task_id)
            self.store.transition(task.id, TaskState.TESTING, actor=run.agent,
                                   reason=resultado.summary)
            rel.completed += (task.key,)
            self._record("implementada", task_id=task.id, run_id=run.id,
                        summary=resultado.summary[:200])
            return

        self._failed(task_id, run, resultado.summary, rel, outcome=resultado.outcome)

    def _failed(self, task_id: str, run: Run, reason: str, rel: TickReport,
                outcome: str = "error") -> None:
        run.state, run.reason = RunState.FAILED, reason
        self.store.save_run(run)

        task = self.store.task(task_id)
        task.attempts += 1
        self.store.save_task(task)

        # A task passa por FAILED antes de qualquer recuperacao. Pular esse
        # degrau economizaria uma linha e apagaria da timeline o fato de que
        # houve falha -- que e exatamente o que alguem procura quando a mesma
        # task volta pela terceira vez.
        self.store.transition(task.id, TaskState.FAILED, actor=run.agent, reason=reason)

        step_name = supervisor.next_recovery_step(task, self.budget)
        if supervisor.no_progress(task, self.store.task_runs(task.id)).stop:
            step_name = "escalar"

        if step_name == "escalar":
            self.store.transition(task.id, TaskState.WAITING_HUMAN, actor="supervisor",
                                   reason=reason)
            self._escalate_failure(task, run, reason, step_name, rel)
        else:
            self.store.transition(task.id, TaskState.READY, actor="supervisor",
                                   reason=f"{step_name} apos falha: {reason}"[:300])
            self._record("falhou", task_id=task.id, run_id=run.id,
                        summary=f"{reason[:160]} -> {step_name}")

    # ---- escalonamento ---------------------------------------------------
    def _escalate(self, task_id: str, run: Run, resultado, rel: TickReport) -> None:
        task = self.store.task(task_id)
        p = resultado.question or {}
        approval = escalation.build(
            task=task,
            what_happened=p.get("o_que_aconteceu", resultado.summary),
            why_it_matters=p.get("por_que_importa", "o agente parou sem conseguir decidir sozinho"),
            attempts=tuple(p.get("tentativas", ())),
            recommendation=p.get("recomendacao", escalation.FOLLOW.id),
            risk=task.risk or RiskLevel.MEDIUM,
            run_id=run.id)
        self._publish(approval, task, rel)

    def _escalate_failure(self, task: Task, run: Run, reason: str, step_name: str,
                      rel: TickReport) -> None:
        attempts = tuple(
            f"{r.agent}: {r.reason or r.state.value}"[:160]
            for r in self.store.task_runs(task.id)[-3:])
        approval = escalation.build(
            task=task,
            what_happened=f"{task.attempts} tentativas falharam. Ultima: {reason}"[:400],
            why_it_matters="a escada de recuperacao acabou; sem decisao sua a task nao sai do lugar",
            attempts=attempts,
            recommendation=escalation.INVESTIGATE.id,
            risk=task.risk or RiskLevel.MEDIUM,
            run_id=run.id)
        self._publish(approval, task, rel)

    def _escalate_cycle(self, p: Plan, rel: TickReport) -> None:
        chaves = [self.store.task(i).key for i in p.in_cycle]
        task = self.store.task(p.in_cycle[0])
        if any(a.task_id == task.id for a in self.store.open_approvals(self.workspace.id)):
            return   # ja perguntei; nao repito a cada tick
        approval = escalation.build(
            task=task,
            what_happened=f"dependencias circulares entre {', '.join(chaves)}",
            why_it_matters="nenhuma dessas tasks pode comecar enquanto o ciclo existir",
            attempts=("montei o grafo a partir dos vinculos declarados na origem",),
            recommendation=escalation.INVESTIGATE.id,
            risk=RiskLevel.MEDIUM)
        self._publish(approval, task, rel)

    def _publish(self, approval, task: Task, rel: TickReport) -> None:
        self.store.open_approval(approval)
        rel.escalated += (task.key,)
        if self.notificador:
            b = escalation.briefing(approval, task)
            self.notificador.notify(f"{task.key} precisa de voce", b.what_happened,
                                    urgency="alta" if approval.risk >= RiskLevel.HIGH else "normal")

    # ---- utilidades ------------------------------------------------------
    def _record(self, kind: str, summary: str = "", task_id: str | None = None,
               run_id: str | None = None, **data: Any) -> None:
        self.store.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id=self.workspace.id, kind=kind,
            task_id=task_id, run_id=run_id, summary=summary, data=data))

    def _status_of(self, t: Task) -> ExternalStatus:
        """A situacao na origem, reconstruida do que foi persistido.

        Provedor sem nocao de situacao devolve DESCONHECIDA -- que NAO e
        disponivel. Conservador de proposito: nao saber se alguem esta na task
        precisa custar um adiamento, nunca um atropelo.
        """
        raw = t.data.get("situacao_externa")
        try:
            return ExternalStatus(raw)
        except ValueError:
            return ExternalStatus.UNKNOWN

    def escopo(self, agent: str = "engine", task_id: str | None = None,
               run_id: str | None = None, project: str = "*") -> Scope:
        return Scope(workspace_id=self.workspace.id, workspace=self.workspace.name,
                      project=project, autonomy=self.workspace.max_autonomy,
                      agent=agent, task_id=task_id, run_id=run_id)
