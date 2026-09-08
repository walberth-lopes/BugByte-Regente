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

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable

from ..core import ids
from ..core.errors import AlreadyExists, InvalidTransition
from ..core.graph import DependencyGraph
from ..core.model import (Dependency, Event, ExternalRef, Run, RunState, Task, Workspace,
                          now)
from ..core.policy import AutonomyLevel
from ..core.risk import RiskEngine, RiskLevel
from ..core.selection import Selection, Verdict
from ..core.scheduling import Candidate, Limits, Plan, plan
from ..core.states import (_AVANCOS, ACTIVE, AWAITING_EXTERNAL, OWNED_ACTIVE,
                           TaskState, engine_can_advance, is_terminus,
                           resumable_from)
from ..ports import AdapterError
from ..ports.support import NotificationProvider
from ..ports.tasks import ExternalTask, ExternalStatus, TaskProvider
from ..ports.agent import (AgentRunner, Budget as AgentBudget, ContextItem,
                           ContextPackage, Mission, Outcome, ProcessStatus)
from ..ports.workspace import WorkspaceProvider
from ..ports.store import Store
from ..ports.repository import RepoRef
from . import escalation, supervisor
from .ci import CIState
from .remote import RemoteDelivery, RemoteIdentity
from .gate import Scope, Gate
from .ownership import Heartbeat, Ownership


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
    #: Entregas cujos checks foram lidos neste tick. Observacao, nunca disparo.
    ci_observed: tuple[str, ...] = ()

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
    #: Where "now" comes from. Injectable because the day boundary decides when
    #: a daily budget resets, and a test that waited for midnight would be a
    #: test nobody runs. Production passes nothing and gets the real clock.
    clock: Callable[[], datetime] = now
    limits: Limits = field(default_factory=Limits)
    budget: supervisor.Budget = field(default_factory=supervisor.Budget)
    notificador: NotificationProvider | None = None
    project_id: str = "prj_default"
    #: Segundos de vida de um lease. O worker renova; se morrer, vence e a
    #: recuperacao devolve a task a fila.
    lease_seconds: int = 900
    #: Regras de elegibilidade e prioridade do workspace. Vazio = tudo elegivel,
    #: na prioridade que a origem deu. Ausencia de regra nao filtra.
    selection: "Selection" = field(default_factory=lambda: Selection())
    #: Preenchido por `plan()`; so existe para o plano poder DIZER o que ficou
    #: de fora por regra, em vez de omitir em silencio.
    _excluded: tuple = ()
    #: Reads the checks of deliveries in flight. `None` means this workspace
    #: has no remote at all -- in which case a task that somehow reaches
    #: `CI_RUNNING` is escalated rather than watched, because an engine that
    #: cannot look must not pretend to be waiting.
    delivery: "RemoteDelivery | None" = None
    #: How many times the engine asks about one commit's checks before handing
    #: the delivery to a person. A pipeline that never concludes is a real
    #: outcome, and polling it for ever is how it stays invisible.
    max_ci_observations: int = 20
    #: Who this workspace belongs to, as POLICY names them. Given by
    #: composition, never derived from an internal id: a client id is opaque by
    #: design, and a rule written about "acme" would silently stop matching if
    #: the engine started answering with `cli_9f3a` instead.
    organization: str = "*"
    client: str = "*"

    # ------------------------------------------------------------------
    def tick(self) -> TickReport:
        rel = TickReport(workspace=self.workspace.name)
        self._record("tick_inicio", summary="tick iniciado")
        try:
            self._recover(rel)
            self._resume_decided(rel)
            # Before looking for new work: a delivery already in flight is
            # closer to done than anything still in the queue, and leaving it
            # unwatched is how CI_RUNNING would become the next dead end.
            self._watch_ci(rel)
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
        expired = {l.owner: l
                   for l in self.store.expired_leases(self.workspace.id,
                                                     when=self.clock())}
        for run in self.store.active_runs(self.workspace.id):
            if run.id not in expired:
                continue
            task = self.store.task(run.task_id)
            run.state = RunState.INTERRUPTED
            run.ended_at = self.clock()
            run.reason = "lease vencido: worker nao renovou"
            self.store.save_run(run)
            self.store.release_lease(run.id, run.id, self.workspace.id)
            for resource in (task.resources if task else ()):
                self.store.release_lease(resource, run.id, self.workspace.id)

            if task is None:
                continue
            destination = supervisor.resume_state(task.state)
            if destination is not task.state:
                self._transition(task.id, destination, actor="supervisor",
                                       reason="worker interrompido")
            rel.recovered += (task.key,)
            self._record("recuperada", task_id=task.id, run_id=run.id,
                        summary=f"worker morto; task volta como {destination.value}")

        self._rescue_stranded(rel)

        # Leases held by nobody. With an atomic claim these should not appear,
        # but a database written by an older version can contain them and a
        # blocked resource that no report can explain is worse than the cost of
        # looking.
        for orphan in self.store.orphan_leases(self.workspace.id,
                                               when=self.clock()):
            self.store.release_lease(orphan.resource, orphan.owner,
                                     self.workspace.id)
            self._record("lease_orfao", summary=(
                f"'{orphan.resource}' estava preso por '{orphan.owner}', que "
                f"nao e um run ativo"))

    def _watch_ci(self, rel: TickReport) -> None:
        """Come back to deliveries that are waiting on somebody else's checks.

        Moving a task to `CI_RUNNING` and never looking again would relocate the
        dead end rather than remove it: the state is active, the scheduler skips
        it, and every later tick reads clean. So each tick re-reads the checks
        for the commit -- an observation, nothing more. It never triggers, never
        re-runs and never cancels anything.

        Where it stops is the point of the design. Whatever CI says, the next
        step belongs to a person: this engine has no reviewer and no authority
        to merge. A conclusive answer therefore escalates, carrying the pull
        request and the checks; what changes with the colour of the CI is what
        the human is told, never who decides.
        """
        if not self.store.tasks(self.workspace.id, [TaskState.CI_RUNNING]):
            return
        for task in self.store.tasks(self.workspace.id, [TaskState.CI_RUNNING]):
            row = self._delivery_of(task)
            if row is None:
                self._escalate_delivery(
                    task, rel, "a task waits on CI with no delivery recorded; "
                    "there is nothing to come back to",
                    escalation.BLOCK.id)
                continue
            if self.delivery is None:
                self._escalate_delivery(
                    task, rel, f"pull request #{row['pr_number']} is open and "
                    f"this workspace has no way to read its checks; a person "
                    f"has to look", escalation.BLOCK.id)
                continue

            identity = RemoteIdentity(
                workspace_id=self.workspace.id, workspace_name=self.workspace.name,
                organization=self.organization, client=self.client,
                task_key=task.key, run_id=row["run_id"],
                repo=RepoRef(provider=row["repo_provider"], key=row["repo_key"]),
                branch=row["branch"], commit_sha=row["commit_sha"])
            observation = self.delivery.observe_ci(identity, row["id"])
            rel.ci_observed += (task.key,)

            if observation.state is CIState.CONCLUDED or observation.allows_progress:
                self._escalate_delivery(
                    task, rel,
                    f"pull request #{row['pr_number']} -- CI "
                    f"{observation.state.value}"
                    + (f"/{observation.result.value}" if observation.result else "")
                    + f": {observation.reason}",
                    # Green: the engine has no next step -- a person reviews
                    # and merges, and the task leaves the work queue until they
                    # do. Red: the change itself is the problem, and asking for
                    # more work on it is a legitimate thing for the engine to
                    # do again. Neither recommendation decides anything; the
                    # person still picks from the whole list.
                    escalation.BLOCK.id if observation.allows_progress
                    else escalation.INVESTIGATE.id,
                    url=row["pr_url"] or "")
                continue

            # Pending, unavailable or unknown: none of those is a result, and
            # none of them is a reason to keep asking for ever.
            seen = int(row.get("ci_observations") or 0) + 1
            if seen >= self.max_ci_observations:
                self._escalate_delivery(
                    task, rel,
                    f"asked {seen} times about the checks for "
                    f"{identity.commit_sha[:12]} and the answer is still "
                    f"{observation.state.value}: {observation.reason}",
                    escalation.BLOCK.id, url=row["pr_url"] or "")

    def _delivery_of(self, task: Task) -> dict | None:
        """The newest delivery for this task that actually reached a remote."""
        rows = [r for r in self.store.deliveries(self.workspace.id, task.key)
                if r.get("pr_number") and r.get("commit_sha")]
        return rows[-1] if rows else None

    def _escalate_delivery(self, task: Task, rel: TickReport, reason: str,
                           recommendation: str, url: str = "") -> None:
        """Hand a delivered change to a person, with what is known about it."""
        self._transition(task.id, TaskState.WAITING_HUMAN, actor="orchestrator",
                               reason=reason[:300])
        approval = escalation.build(
            task=task,
            what_happened="the change was delivered and its checks were read",
            why_it_matters=reason,
            attempts=((url,) if url else ()),
            recommendation=recommendation,
            risk=task.risk or RiskLevel.MEDIUM)
        self._publish(approval, task, rel)
        self._record("ci_observada", task_id=task.id,
                    summary=reason[:200], pull_request=url)

    def _rescue_stranded(self, rel: TickReport) -> None:
        """Tasks in an owned state with nobody inside them.

        Recovery above proves a worker died by its EXPIRED LEASE. That proof is
        unavailable for one window, and a real one: `_collect` releases the
        leases, saves the run as finished, and only then transitions the task.
        A process killed between the second and third of those leaves a task in
        an owned active state with no lease to expire and no run still active.
        Nothing reaches it afterwards -- the scheduler skips active states, and
        matching expired leases finds nothing to match. The task is lost while
        every report stays clean, which is the failure mode this engine keeps
        finding and the reason this clause exists.

        Found by the multi-tenant kill harness on one round in several: the
        window is a few milliseconds wide, which is exactly how long a defect
        needs to survive a thousand ticks.
        """
        alive = {r.task_id for r in self.store.active_runs(self.workspace.id)}
        held = {l.owner for l in self.store.leases(self.workspace.id)
                if l.expires_at and l.expires_at > self.clock()}
        for task in self.store.tasks(self.workspace.id):
            if task.state not in OWNED_ACTIVE or task.id in alive:
                continue
            # A live lease means a worker IS inside and merely has not written
            # its run yet. Returning that task to the queue would hand live work
            # to a second worker -- worse than the strand being fixed.
            runs = self.store.task_runs(task.id, self.workspace.id)
            if any(r.id in held for r in runs):
                continue

            destination = supervisor.resume_state(task.state)
            reason = (f"task ficou em {task.state.value} sem run ativo e sem "
                      f"lease: o worker morreu depois de encerrar o run e antes "
                      f"de mover a task. Nada a alcancaria de novo")
            self._transition(task.id, destination, actor="supervisor",
                                   reason=reason)
            rel.recovered += (task.key,)
            self._record("resgatada", task_id=task.id,
                        summary=f"{reason}; volta como {destination.value}")

    def _resume_decided(self, rel: TickReport) -> None:
        """Act on decisions a person already made.

        Without this the escalation queue is a one-way door: `regente decide`
        recorded the choice, printed that the next tick would resume the task,
        and no tick ever read it back. Everything that ever escalated stayed in
        WAITING_HUMAN for good, while the CLI said otherwise.

        It went unnoticed because the unit test performed the resuming
        transition itself, so it proved the store could record a decision and
        proved nothing about the engine acting on one. A soak run found it by
        running out of work.

        Where a task goes is decided by the option chosen, not by guessing:

          seguir      -> onward if the engine has a stage; otherwise BLOCKED,
                         which is honest where DONE would be a lie
          investigar  -> back to be worked again
          bloquear    -> out of the queue until someone unblocks it
          cancelar    -> closed

        Every destination is checked against `resumable_from(paused_at)` before
        it is used. See `DECISION_ROUTES`.
        """
        for approval in self.store.decided_approvals(self.workspace.id):
            task = self.store.task(approval.task_id, self.workspace.id)
            if task is None or task.state is not TaskState.WAITING_HUMAN:
                continue

            destination, why = self._destination_for(task, approval)
            if destination is None:
                continue
            if not self._moved_by_another(task.id, destination,
                                          actor=approval.decided_by or "humano",
                                          reason=why):
                continue          # another worker applied the same decision
            rel.unblocked_tasks += (task.key,)
            self._record("decisao_aplicada", task_id=task.id,
                        run_id=approval.run_id,
                        summary=f"{approval.choice} -> {destination.value}",
                        approval_id=approval.id, choice=approval.choice)

    #: What each decision means, as an ordered preference. The engine takes the
    #: first destination the state machine actually permits from where the task
    #: paused -- it never assumes one.
    #:
    #: Assuming was the bug. A first version sent every `investigar` to READY,
    #: which is not reachable from a task paused in TESTING, and the tick died
    #: with `InvalidTransition` on a perfectly ordinary decision. The state
    #: machine already knows the answer; asking it is both shorter and correct.
    DECISION_ROUTES = {
        escalation.CANCEL.id: (TaskState.CANCELLED,),
        escalation.BLOCK.id: (TaskState.BLOCKED,),
        escalation.INVESTIGATE.id: (TaskState.READY, TaskState.IMPLEMENTING,
                                    TaskState.FAILED, TaskState.BLOCKED),
    }

    def _destination_for(self, task: Task,
                         approval) -> tuple[TaskState | None, str]:
        """Where a decided task goes, and why, in words a person can check."""
        choice = (approval.choice or "").lower()
        paused = task.paused_at
        who = approval.decided_by or "humano"
        if paused is None:
            # A pause with no record of where it came from cannot be resumed
            # anywhere safely. Health reports this separately; here it is left
            # alone rather than sent somewhere invented.
            return None, ""

        allowed = resumable_from(paused)

        if choice in self.DECISION_ROUTES:
            for candidate in self.DECISION_ROUTES[choice]:
                if candidate in allowed:
                    return candidate, f"{choice} por {who}: {paused.value} -> {candidate.value}"

        # `seguir`, and anything the engine does not recognise. Unrecognised
        # lands here on purpose: a decision that cannot be parsed must still
        # move the task, or the queue silently stops draining -- which is the
        # defect this whole path exists to fix.
        if engine_can_advance(paused) and paused in allowed:
            return paused, f"retomada em {paused.value} por {who}"
        for candidate in _AVANCOS.get(paused, frozenset()):
            if candidate in allowed and engine_can_advance(candidate):
                return candidate, (f"seguir por {who}: {paused.value} -> "
                                   f"{candidate.value}")
        if TaskState.BLOCKED in allowed:
            return (TaskState.BLOCKED,
                    f"parada por {who}: o trabalho chegou a {paused.value} e "
                    f"este motor nao tem etapa seguinte. Fica fora da fila, com "
                    f"motivo, ate existir a etapa ou alguem destravar")
        return None, ""

    # ---- 2. descoberta --------------------------------------------------
    def _selection_of(self, e) -> "Verdict":
        """O veredito das regras do workspace para esta task.

        Aplicado na FRONTEIRA, onde os campos externos existem -- e nao dentro
        do scheduler, que nao deve aprender o que e um rotulo ou um titulo.
        """
        from ..core.selection import Selectable

        campos = Selectable(
            title=e.title, key=e.key, project=e.project, status=e.status.value,
            labels=list(e.labels), priority=e.priority, assignee=e.assignee,
            type=str(e.data.get("tipo") or e.data.get("type") or ""),
            components=list(e.data.get("componentes")
                            or e.data.get("components") or ()))
        return self.selection.evaluate(campos, base_priority=e.priority)

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

        # As regras do workspace sao reaplicadas AQUI, e nao so na descoberta.
        #
        # Ate o marco de configuracao esta linha era `task.priority = e.priority`
        # -- a regra valia na criacao e era desfeita no tick seguinte. A ordem
        # voltava sozinha para a da origem, sem nada nos eventos, e a unica
        # pista era a prioridade nao bater com o motivo gravado ao lado dela.
        #
        # Reaplicar tambem e o que faz uma regra EDITADA pela tela valer sem
        # redescobrir o board: o proximo tick reavalia o que ja existe.
        veredito = self._selection_of(e)
        task.priority = veredito.priority
        task.data.update({"situacao_externa": current_status,
                           "estado_externo": e.external_status,
                           "rotulos": list(e.labels),
                           "elegivel": veredito.eligible,
                           "selecao": list(veredito.reasons),
                           "excluida_por": veredito.excluded_by,

                           # A prioridade que a ORIGEM deu, ao lado da efetiva. Sem ela,
                           # reavaliar as regras sobre a prioridade ja ajustada comporia
                           # os deltas a cada passagem, e a previa da tela mostraria uma
                           # ordem que o motor nunca produziria.
                           "prioridade_origem": e.priority})
        task.updated_at = self.clock()
        self.store.save_task(task)
        if before and before != current_status:
            rel.changes += ((task.key, before, current_status),)
            self._record("mudou_na_origem", task_id=task.id,
                        summary=f"{before} -> {current_status} ({e.external_status})",
                        de=before, to_state=current_status)

    def _create_task(self, e: ExternalTask) -> Task:
        veredito = self._selection_of(e)
        t = Task(
            id=ids.new_id(ids.TASK), workspace_id=self.workspace.id,
            project_id=e.project or self.project_id, title=e.title,
            state=TaskState.DISCOVERED,
            externo=ExternalRef(provider=self.tasks_provider.name, key=e.key, url=e.url),
            description=e.description, priority=veredito.priority,
            resources=tuple(e.resources),
            data={**dict(e.data), "situacao_externa": e.status.value,
                   "estado_externo": e.external_status,
                   "rotulos": list(e.labels),
                   # Guardado com o PORQUE. Uma ordem que ninguem consegue
                   # explicar e uma ordem em que ninguem confia, e a primeira
                   # pergunta de quem ve o board reordenado e "por que essa
                   # primeiro?".
                   "elegivel": veredito.eligible,
                   "selecao": list(veredito.reasons),
                   "excluida_por": veredito.excluded_by,
                   "prioridade_origem": e.priority})
        try:
            self.store.save_task(t)
        except AlreadyExists:
            # Another worker discovered the same external task in the same
            # instant and inserted it first. The unique index on
            # (workspace_id, provider, external_key) did its job; losing the
            # race is not an error, and letting it kill the whole tick was.
            #
            # Observed on the first three-process run: two workers crashed out
            # of their entire tick because a third created a task they had both
            # just seen.
            existing = self.store.task_by_key(self.workspace.id,
                                              self.tasks_provider.name, e.key)
            if existing is None:
                raise
            self._record("descoberta_concorrente", task_id=existing.id,
                        summary=f"{e.key} ja registrada por outro worker")
            return existing
        return t

    # ---- 3. analise -----------------------------------------------------
    def _transition(self, task_id: str, destination: TaskState, actor: str,
                    reason: str = "", **extra):
        """Move a task, always saying which tenant is asking.

        A helper rather than a convention, because a convention is a comment.
        `store.transition` accepts an optional workspace and a call site that
        forgets it can move ANOTHER client's task -- which is exactly what
        happened: reads were scoped and writes were not, ten lines apart, so a
        client that could not see a task could still cancel it.
        """
        return self.store.transition(task_id, destination, actor=actor,
                                     reason=reason,
                                     workspace_id=self.workspace.id, **extra)

    def _moved_by_another(self, task_id: str, destination: TaskState,
                          actor: str, reason: str,
                          expected: TaskState | None = None, **extra) -> bool:
        """Transition, and treat losing the race as a normal outcome.

        Two workers analysing the same board reach the same conclusion at the
        same moment; one of them gets there first and the other's transition is
        refused. That is the state machine doing its job, and it used to take
        down the loser's entire tick with `InvalidTransition` -- so a correct
        refusal cost a whole cycle of work.

        Returns True when this worker made the move, False when somebody else
        already had. Never swallows an invalid transition from a state nobody
        else touched: it re-reads the row and only forgives the case where the
        task genuinely moved on.
        """
        before = self.store.task(task_id, self.workspace.id)
        if before is None:
            # Either the task is gone, or it belongs to another tenant and this
            # engine simply cannot see it. Both mean: not ours to move.
            return False
        if (expected is not None and before is not None
                and before.state is not expected):
            # The task is not where this worker last saw it, so the decision to
            # move it was made about a world that no longer exists. Another
            # worker got there between the read and the write -- which is
            # ordinary, and used to arrive as `InvalidTransition` killing a
            # whole tick.
            self._record("corrida_perdida", task_id=task_id,
                        summary=f"esperava {expected.value}, encontrou "
                                f"{before.state.value}")
            return False
        if before is not None and before.state is destination:
            # Already where this worker wanted to put it. Another worker did
            # the same job first, which is not a failure and must not be an
            # exception -- `READY -> READY` is an invalid transition and used to
            # take a whole tick down for the crime of agreeing.
            return False
        try:
            self._transition(task_id, destination, actor=actor,
                             reason=reason, **extra)
            return True
        except InvalidTransition:
            after = self.store.task(task_id, self.workspace.id)
            if after is not None and before is not None and after.state is not before.state:
                self._record("corrida_perdida", task_id=task_id,
                            summary=f"outro worker ja moveu para {after.state.value}")
                return False
            raise

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
                self._moved_by_another(t.id, TaskState.READY, actor="planner",
                                       reason="a origem liberou o trabalho")
                rel.unblocked_tasks += (t.key,)

        for t in self.store.tasks(self.workspace.id, [TaskState.DISCOVERED]):
            # Two workers reading the same board reach this line together. The
            # loser used to take its whole tick down with `InvalidTransition`,
            # so a correct refusal cost a full cycle of work; now it simply
            # leaves the task to whoever claimed it.
            if not self._moved_by_another(t.id, TaskState.ANALYZING,
                                          actor="planner",
                                          reason="analise inicial",
                                          expected=TaskState.DISCOVERED):
                continue
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
                self._transition(
                    t.id, TaskState.BLOCKED, actor="planner",
                    reason=f"a origem diz {t.data.get('estado_externo') or status.value}")
                rel.analyzed += 1
                continue

            self.store.save_task(t)
            self._moved_by_another(t.id, TaskState.READY, actor="planner",
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
        # Inelegivel nao vira candidata -- e tambem NAO SOME. Ela continua no
        # board, com o motivo gravado, e aparece como adiada. Trabalho que
        # desaparece sem explicacao e como um board perde tarefas sem ninguem
        # perceber; uma regra de exclusao tem de ser visivel para poder ser
        # discutida.
        prontas = [t for t in todas if t.state is TaskState.READY]
        elegiveis = [t for t in prontas if t.data.get("elegivel") is not False]
        self._excluded = tuple(
            (t.id, f"fora por regra de selecao: "
                   f"{t.data.get('excluida_por') or 'nao elegivel'}")
            for t in prontas if t.data.get("elegivel") is False)
        candidates = [
            Candidate(task_id=t.id, priority=t.priority,
                      resources=frozenset(t.resources), key=t.key)
            for t in elegiveis
        ]
        hoje = self.clock().strftime("%Y-%m-%d")
        p = plan(candidates, self._graph(), completed, running_now,
                    self.limits, self.store.dispatch_count(self.workspace.id, hoje),
                    nomes={t.id: t.key for t in todas})
        if self._excluded:
            from ..core.scheduling import Deferred

            p = replace(p, deferred=p.deferred + tuple(
                Deferred(i, motivo) for i, motivo in self._excluded))
        return p

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
        task = self.store.task(task_id, self.workspace.id)
        run = Run(id=ids.new_id(ids.RUN), task_id=task.id, task_key=task.key,
                  workspace_id=self.workspace.id,
                  agent="coder", state=RunState.RUNNING, started_at=self.clock())

        # The run row and every lease it needs are written in ONE transaction.
        #
        # They used to be separate writes -- each lease its own transaction, the
        # run saved afterwards -- which left two windows where a `SIGKILL`
        # orphaned a live lease whose owner had no run row. Recovery finds dead
        # workers by matching expired leases against active runs, so an orphan
        # matched nothing and blocked its resource until it expired, with no
        # report able to explain why. It appeared in 2 of 25 contention rounds.
        #
        # Sorted inside `claim`, so every worker asks for the same resource
        # first. Acquisition never blocks, so a classic deadlock is impossible;
        # LIVELOCK is not -- two tasks wanting {A,B} in opposite orders take one
        # each, both fail, both release, both retry for ever. A global order
        # breaks the symmetry: both ask for A first and exactly one proceeds.
        held: list[str] = sorted(task.resources)
        try:
            claimed = self.store.claim(
                run, tuple(held), self.lease_seconds, when=self.clock(),
                task_id=task.id, destination=TaskState.ASSIGNED,
                actor="orchestrator", reason=f"run {run.id}")
        except InvalidTransition:
            # Another worker moved this task between the plan and the dispatch.
            # Nothing was written -- the transaction rolled back -- so there is
            # no claim to unwind.
            self._record("adiada", task_id=task.id,
                        summary="a task mudou de state entre o plano e o despacho")
            return
        if not claimed:
            self._record("adiada", task_id=task.id,
                        summary="recurso ocupado entre o plano e o despacho")
            return

        area = self.area_provider.prepare(_sanitize(task.key),
                                          branch=f"regente/{task.key.lower()}")
        run.workspace_path, run.branch = area.path, area.branch
        self.store.save_run(run)          # now an update: the row already exists
        self.store.mark_dispatch(self.workspace.id,
                                 self.clock().strftime("%Y-%m-%d"))
        self._transition(task.id, TaskState.IMPLEMENTING, actor=run.agent,
                               reason="worker iniciou")
        rel.dispatched += (task.key,)
        self._record("despachada", task_id=task.id, run_id=run.id,
                    summary=f"{run.agent} em {area.path}")

        request = Mission(
            workspace_id=self.workspace.id, workspace_name=self.workspace.name,
            task_key=task.key, run_id=run.id, branch=area.branch or "",
            allowed_root=area.path, goal=task.title, agent=run.agent,
            context=ContextPackage(
                goal=task.title,
                items=(ContextItem(
                    kind="task", ref=task.key,
                    reason="the work itself; without it there is no mission",
                    content=task.description or ""),)),
            budget=AgentBudget(
                max_iterations=self.budget.max_iterations,
                max_tool_calls=self.budget.max_tool_calls,
                max_cost_usd=self.budget.max_cost_usd,
                max_seconds=self.budget.max_seconds,
                max_process_seconds=self.budget.max_seconds))

        owner = Ownership(store=self.store, workspace_id=self.workspace.id,
                          run_id=run.id, resources=tuple(held),
                          lease_seconds=self.lease_seconds, clock=self.clock)

        # The mission runs with its leases being renewed underneath it. Without
        # this, a mission longer than one lease window loses its resources while
        # doing everything right, and recovery hands its task to somebody else.
        with Heartbeat(ownership=owner,
                       cancel=getattr(self.runner, "cancel", None)) as beat:
            try:
                resultado = self.runner.run(request)
            except Exception as e:   # noqa: BLE001
                resultado = None
                run.reason = f"{type(e).__name__}: {e}"[:300]

        # Possession is checked AGAIN, here, because the world was allowed to
        # move while the mission ran. A worker that lost its lease must not
        # write: on the first real contention run one did exactly that, and was
        # stopped only because the transition it attempted happened to be
        # illegal. Had the task been in the ordinary next state, the write would
        # have been legal and would have overwritten another worker's run.
        if beat.lost or not owner.held():
            self._abandon(task.id, run, held, rel)
            return

        self._collect(task.id, run, resultado, held, rel)

    def _abandon(self, task_id: str, run: Run, held: list[str],
                 rel: TickReport) -> None:
        """This run lost ownership while working. Write nothing about the work.

        Nothing this run produced is written, and the run itself is recorded --
        what happened is always recorded.

        The task, though, cannot simply be left. Losing possession has two very
        different causes and only one of them leaves the task in good hands:

          - another worker TOOK the resources: it owns the task and will drive
            it, and this run must not interfere;
          - the lease merely EXPIRED with nobody else picking it up: the task is
            then sitting in an active state that the scheduler skips, owned by a
            run that just marked itself interrupted, and recovery cannot reach
            it either -- recovery finds dead workers by matching expired leases
            against active runs, and this path has already released the leases
            and ended the run.

        The second case stranded a task for good. Found under two tenants
        contending, and it is not a tenancy defect at all: it needed a mission
        that outlived its lease with nobody waiting, which is rare enough to
        have survived a thousand-tick soak and forty contention rounds.

        So the task goes back to the queue only when no other active run owns
        it. If somebody else does, it is theirs and this run keeps its hands off.
        """
        for resource in held:
            self.store.release_lease(resource, run.id, self.workspace.id)
        run.state = RunState.INTERRUPTED
        run.ended_at = self.clock()
        run.reason = ("perdeu a posse durante a missao: outro worker assumiu ou "
                      "o lease venceu")
        self.store.save_run(run)
        rel.errors += (f"{run.id}: ownership lost",)
        self._record("posse_perdida", task_id=task_id, run_id=run.id,
                    summary=run.reason)

        task = self.store.task(task_id, self.workspace.id)
        if task is None or task.state not in ACTIVE:
            return
        taken_by_another = any(r.task_id == task_id and r.id != run.id
                               for r in self.store.active_runs(self.workspace.id))
        if taken_by_another:
            return
        destination = supervisor.resume_state(task.state)
        if destination is not task.state:
            self._moved_by_another(
                task.id, destination, actor="supervisor",
                reason="posse perdida e ninguem assumiu; volta para a fila")
            rel.recovered += (task.key,)

    # ---- 6. colheita ----------------------------------------------------
    def _collect(self, task_id: str, run: Run, resultado, held: list[str],
               rel: TickReport) -> None:
        for r in held:
            self.store.release_lease(r, run.id, self.workspace.id)
        run.ended_at = self.clock()

        if resultado is None:
            self._failed(task_id, run, run.reason or "worker levantou excecao", rel)
            return

        run.cost_usd, run.tokens = resultado.cost_usd, resultado.tokens
        run.tool_calls, run.iterations = resultado.tool_calls, 1

        if (resultado.status is ProcessStatus.NEEDS_HUMAN
                or resultado.escalation_requested):
            run.state, run.reason = RunState.ABORTED, resultado.summary
            self.store.save_run(run)
            self._transition(task_id, TaskState.WAITING_HUMAN, actor=run.agent,
                                   reason=resultado.summary)
            self._escalate(task_id, run, resultado, rel)
            return

        if resultado.status is ProcessStatus.FINISHED:
            run.state, run.reason = RunState.SUCCEEDED, resultado.summary
            self.store.save_run(run)
            task = self.store.task(task_id, self.workspace.id)
            self._transition(task.id, TaskState.TESTING, actor=run.agent,
                                   reason=resultado.summary)
            rel.completed += (task.key,)
            self._record("implementada", task_id=task.id, run_id=run.id,
                        summary=resultado.summary[:200])
            # The process finished. Nothing in this engine advances a task out
            # of TESTING yet -- the stages that would are later milestones. So
            # the task is handed to a person instead of being left to look busy
            # forever. Silence here was the defect: the scheduler skips an
            # active task, nothing else touches it, and every tick afterwards
            # is clean and empty.
            self._park_or_escalate(task, run, rel)
            return

        self._failed(task_id, run, resultado.summary, rel,
                     outcome=resultado.status.value)

    def _park_or_escalate(self, task: Task, run: Run, rel: TickReport) -> None:
        """Refuse to leave a task where no tick can pick it up again.

        The check asks the state machine which states this engine has code to
        advance, rather than trusting a list written from memory. A milestone
        that adds a stage adds its state there, and until then the road ends
        here honestly instead of silently.
        """
        current = self.store.task(task.id, self.workspace.id)
        if current is None or not is_terminus(current.state):
            return

        reason = (f"the work finished and reached {current.state.value}, which "
                  f"this engine has no stage to advance; it needs you rather "
                  f"than a queue that looks busy")
        self._transition(current.id, TaskState.WAITING_HUMAN,
                               actor="orchestrator", reason=reason)
        approval = escalation.build(
            task=current,
            what_happened=f"{run.agent} finished and the task reached "
                          f"{current.state.value}",
            why_it_matters=reason,
            attempts=(run.reason or "",),
            recommendation=escalation.BLOCK.id,
            risk=current.risk or RiskLevel.MEDIUM,
            run_id=run.id)
        self._publish(approval, current, rel)
        self._record("terminus", task_id=current.id, run_id=run.id,
                    summary=reason[:200], state=current.state.value)

    def _failed(self, task_id: str, run: Run, reason: str, rel: TickReport,
                outcome: str = "error") -> None:
        run.state, run.reason = RunState.FAILED, reason
        self.store.save_run(run)

        task = self.store.task(task_id, self.workspace.id)
        task.attempts += 1
        self.store.save_task(task)

        # A task passa por FAILED antes de qualquer recuperacao. Pular esse
        # degrau economizaria uma linha e apagaria da timeline o fato de que
        # houve falha -- que e exatamente o que alguem procura quando a mesma
        # task volta pela terceira vez.
        self._transition(task.id, TaskState.FAILED, actor=run.agent, reason=reason)

        step_name = supervisor.next_recovery_step(task, self.budget)
        if supervisor.no_progress(task, self.store.task_runs(task.id, self.workspace.id)).stop:
            step_name = "escalar"

        if step_name == "escalar":
            self._transition(task.id, TaskState.WAITING_HUMAN, actor="supervisor",
                                   reason=reason)
            self._escalate_failure(task, run, reason, step_name, rel)
        else:
            self._transition(task.id, TaskState.READY, actor="supervisor",
                                   reason=f"{step_name} apos falha: {reason}"[:300])
            self._record("falhou", task_id=task.id, run_id=run.id,
                        summary=f"{reason[:160]} -> {step_name}")

    # ---- escalonamento ---------------------------------------------------
    def _escalate(self, task_id: str, run: Run, resultado, rel: TickReport) -> None:
        task = self.store.task(task_id, self.workspace.id)
        approval = escalation.build(
            task=task,
            what_happened=resultado.summary,
            why_it_matters=(resultado.escalation_reason
                            or "o agente parou sem conseguir decidir sozinho"),
            attempts=tuple(resultado.questions),
            recommendation=escalation.FOLLOW.id,
            risk=task.risk or RiskLevel.MEDIUM,
            run_id=run.id)
        self._publish(approval, task, rel)

    def _escalate_failure(self, task: Task, run: Run, reason: str, step_name: str,
                      rel: TickReport) -> None:
        attempts = tuple(
            f"{r.agent}: {r.reason or r.state.value}"[:160]
            for r in self.store.task_runs(task.id, self.workspace.id)[-3:])
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
