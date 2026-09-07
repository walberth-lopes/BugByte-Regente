# -*- coding: utf-8 -*-
"""O estado do motor, dito para fora.

Uma camada, e uma regra que a define inteira: **isto nao decide nada**. Nao
seleciona missao, nao resolve alvo, nao avalia policy, nao interpreta lease, nao
classifica CI. Tudo isso ja tem dono no motor. O que esta aqui le o que aqueles
donos gravaram e monta uma resposta que alguem de fora consegue consumir.

    Store / Engine  ->  Read Model  ->  API  ->  Mission Control

A tentacao que este arquivo existe para resistir e a de completar a historia. Uma
tela fica melhor quando a linha do tempo tem todas as etapas, quando todo estado
tem um motivo bonito e quando o painel fica verde. Todas as tres sao mentiras
baratas de escrever aqui e caras de descobrir depois: quem le a Mission Control
esta decidindo se precisa agir.

Por isso, tres invariantes:

1. **Ausencia nunca vira sucesso.** Sem PR, o campo diz que nao houve PR. Sem CI
   observado, diz que nao foi observado -- nunca verde.
2. **Nenhuma etapa e inferida.** A cadeia de entrega e montada a partir das
   colunas efetivamente preenchidas, e a linha do tempo a partir de eventos
   efetivamente gravados.
3. **Escopo nao e opcional.** Todo metodo comeca por `workspace_id`, e nenhuma
   leitura daqui chama o store sem ele. Um id vindo de fora nao carrega
   autoridade: conhecer o id de um run de outro cliente nao pode ser o
   suficiente para le-lo.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..core.model import Approval, Event, Lease, Run, RunState, Task, now
from ..core.states import (ACTIVE, AWAITING_EXTERNAL, MEANING, OWNED_ACTIVE,
                           TERMINAL, TaskState, describe, engine_can_advance,
                           is_terminus)
from ..ports.store import Store
from . import health as health_module

#: Texto usado quando o motor nao gravou motivo nenhum. Dizer isso e melhor que
#: inventar um motivo plausivel, e muito melhor que deixar em branco -- um campo
#: vazio numa tela le-se como "nada de errado".
UNRECORDED = "nenhum motivo registrado"

#: Usado quando um cliente existe mas ninguem gravou como ele se chama. Repetir
#: o id no lugar do nome seria pior: um id opaco parece um nome escolhido.
UNNAMED = "cliente sem nome gravado"


# ---------------------------------------------------------------------------
# Formas expostas. Sao o CONTRATO externo, separadas das linhas do banco de
# proposito: a tabela pode ser renomeada sem quebrar quem consome, e um campo
# interno novo nao vaza para fora so por existir.
# ---------------------------------------------------------------------------

def _dict(value: Any) -> Any:
    """Converte para tipos que atravessam JSON, sem perder precisao de tempo."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dict(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class View:
    """Base de tudo que sai daqui. `as_dict` e o unico caminho para a API."""

    def as_dict(self) -> dict:
        return {k: _dict(v) for k, v in asdict(self).items()}


@dataclass(frozen=True, slots=True)
class StateView(View):
    """Um estado, dito de forma que nao dependa de cor para ser entendido.

    Nome, significado, motivo, desde quando, ha quanto tempo, e de quem e o
    proximo passo. Um circulo vermelho sem essas seis coisas obriga quem olha a
    abrir o terminal -- que era o trabalho que a tela deveria ter poupado.
    """
    name: str
    meaning: str
    reason: str
    since: datetime | None
    age_seconds: int
    age: str
    #: Quem move isto daqui: `engine`, `human`, `external` ou `nobody`.
    owner: str
    next_action: str


@dataclass(frozen=True, slots=True)
class Blocker(View):
    """Por que algo nao anda, nas palavras de quem sabe.

    `kind` e vocabulario fechado para que a tela possa agrupar sem interpretar
    texto. `detail` e o que o motor gravou, sem reescrita.
    """
    kind: str
    summary: str
    detail: str = ""
    since: datetime | None = None
    action: str = ""


@dataclass(frozen=True, slots=True)
class DeliveryView(View):
    """A cadeia task -> run -> commit -> push -> PR -> CI.

    Cada elo so aparece preenchido se a coluna correspondente foi escrita. Nao
    existe "provavelmente subiu": ou ha registro, ou o campo diz que nao ha.
    """
    id: str
    task_key: str
    run_id: str
    repository: str
    branch: str
    commit_sha: str
    pushed: bool
    push_target: str
    pull_request: int | None
    pull_request_url: str
    pull_request_head: str
    #: Por que nao ha PR. So preenchido quando realmente nao ha.
    pull_request_absent: str
    ci_state: str
    ci_result: str
    ci_reason: str
    ci_observations: int
    ci_observed_at: datetime | None
    created_at: datetime | None

    @property
    def ci_is_green(self) -> bool:
        """Verde exige `CONCLUDED` E resultado bom. Nada menos."""
        return self.ci_state == "CONCLUDED" and self.ci_result in (
            "PASSED", "PREEXISTING_FAILURE")

    def as_dict(self) -> dict:
        """Leva o veredito junto, para a tela nao precisar chegar nele sozinha.

        Se a UI decidisse o que e verde, existiriam duas definicoes de verde no
        sistema -- e a que o operador ve seria a que nao conhece
        `PREEXISTING_FAILURE`, `NO_CHECKS` nem `UNAVAILABLE`.
        """
        return {**super().as_dict(), "ci_green": self.ci_is_green}


@dataclass(frozen=True, slots=True)
class TimelineEntry(View):
    """Um evento que realmente foi gravado. Nunca um que faltou."""
    at: datetime
    kind: str
    actor: str
    summary: str
    run_id: str
    data: dict


@dataclass(frozen=True, slots=True)
class RunCard(View):
    id: str
    task_key: str
    task_id: str
    workspace_id: str
    client: str
    agent: str
    state: str
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    duration: str
    reason: str
    cost_usd: float
    tokens: int
    branch: str


@dataclass(frozen=True, slots=True)
class RunDetail(View):
    run: RunCard
    workspace_name: str
    repository: str
    workspace_path: str
    iterations: int
    tool_calls: int
    #: Posse: o lease que este run segura AGORA. Vazio nao significa que morreu,
    #: significa que nao segura nada -- e a diferenca importa.
    leases: tuple[str, ...]
    blockers: tuple[Blocker, ...]
    delivery: DeliveryView | None
    timeline: tuple[TimelineEntry, ...]


@dataclass(frozen=True, slots=True)
class TaskCard(View):
    id: str
    key: str
    title: str
    workspace_id: str
    workspace_name: str
    client: str
    state: StateView
    priority: int
    risk: str
    attempts: int
    repository: str
    created_at: datetime | None
    updated_at: datetime | None
    needs_human: bool
    blocked: bool


@dataclass(frozen=True, slots=True)
class TargetView(View):
    """O alvo escolhido e a evidencia que o sustenta.

    Evidencia junto de proposito: um repositorio escolhido sem evidencia falha
    em silencio -- tudo da certo, no lugar errado.
    """
    repository: str
    provider: str
    source: str
    confidence: str
    strength: float
    #: `VALIDATED` significa que uma execucao real confirmou o alvo. E o proprio
    #: `source` que carrega isso -- nao ha coluna separada, e inventar uma aqui
    #: criaria um segundo lugar onde "confirmado" pode discordar de si mesmo.
    validated: bool
    confirmations: int
    evidence: tuple[str, ...]
    alternatives: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EscalationView(View):
    id: str
    task_id: str
    task_key: str
    run_id: str
    what_happened: str
    why_it_matters: str
    what_was_tried: tuple[str, ...]
    options: tuple[dict, ...]
    recommendation: str
    risk: str
    created_at: datetime | None
    waiting_seconds: int
    waiting: str


@dataclass(frozen=True, slots=True)
class TaskDetail(View):
    card: TaskCard
    previous_state: str
    description: str
    resources: tuple[str, ...]
    current_run: RunCard | None
    last_run: RunCard | None
    last_result: str
    target: TargetView | None
    deliveries: tuple[DeliveryView, ...]
    blockers: tuple[Blocker, ...]
    escalation: EscalationView | None
    timeline: tuple[TimelineEntry, ...]


@dataclass(frozen=True, slots=True)
class Counted(View):
    name: str
    count: int


@dataclass(frozen=True, slots=True)
class HealthView(View):
    """A saude do motor, exatamente como o motor a calcula.

    Reexposta, nunca recalculada: uma segunda implementacao de saude na borda e
    uma segunda opiniao sobre estar tudo bem, e a que aparece na tela seria a
    que nao viu o problema.
    """
    level: str
    healthy: bool
    at: datetime
    signals: tuple[dict, ...]
    measurements: dict


@dataclass(frozen=True, slots=True)
class Overview(View):
    workspace_id: str
    workspace_name: str
    client: str
    organization: str
    at: datetime
    health: HealthView
    tasks_by_state: tuple[Counted, ...]
    total_tasks: int
    active_runs: int
    workers: int
    leases_live: int
    leases_expired: int
    needs_human: int
    blocked: int
    stalled: tuple[TaskCard, ...]
    deliveries_in_flight: int
    last_activity: datetime | None
    last_activity_age: str
    last_tick: datetime | None
    last_tick_age: str


@dataclass(frozen=True, slots=True)
class WorkspaceCard(View):
    id: str
    name: str
    client: str
    client_id: str
    organization: str
    autonomy: str
    tasks: int
    active_runs: int
    needs_human: int
    health: str


@dataclass(frozen=True, slots=True)
class ClientCard(View):
    id: str
    name: str
    organization: str
    workspaces: tuple[WorkspaceCard, ...]


# ---------------------------------------------------------------------------


def _age(then: datetime | None, at: datetime) -> tuple[int, str]:
    """Segundos e uma forma legivel. `-1` quando nao ha instante para medir."""
    if then is None:
        return -1, "desconhecido"
    seconds = int(max(0.0, (at - then).total_seconds()))
    if seconds < 60:
        return seconds, f"{seconds}s"
    if seconds < 3600:
        return seconds, f"{seconds // 60}min"
    if seconds < 86400:
        return seconds, f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"
    return seconds, f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


@dataclass(slots=True)
class ReadModel:
    """Leituras escopadas. Toda entrada comeca por `workspace_id`.

    A assinatura e a guarda: nao existe metodo aqui que aceite um id sozinho.
    Conhecer o id de um run de outro cliente nao pode bastar para le-lo, e a
    forma de garantir isso e nao oferecer a chamada.
    """
    store: Store
    clock: Callable[[], datetime] = now
    #: Raiz das areas de trabalho, quando a composicao a conhece. So a saude usa.
    areas_root: str | None = None
    #: Nome da organizacao, como a composicao a chama. `*` quando nao configurada.
    organization: str = "*"
    #: Tetos configurados. Sem eles a pergunta de orcamento responde `UNKNOWN`,
    #: e a mesma pergunta passa a ter duas respostas no sistema: a do terminal,
    #: que recebe os tetos, e a da tela, que nao recebia. Uma superficie
    #: estruturalmente menos informada que a outra nao e um detalhe -- e a que o
    #: operador olha.
    budget_usd: float | None = None
    max_dispatches: int | None = None

    # ---- inventario -------------------------------------------------
    def clients(self) -> tuple[ClientCard, ...]:
        """Clientes e seus workspaces. A raiz da navegacao.

        O nome vem da tabela de clientes quando a composicao ja o gravou. Quando
        nao gravou, o campo diz que nao se sabe, em vez de repetir o id: um
        identificador opaco no lugar do nome parece um nome escolhido por
        alguem, e quem le nao tem como perceber a diferenca.
        """
        named = {c["id"]: c for c in self.store.clients()}
        by_client: dict[str, list[WorkspaceCard]] = {}
        for w in self.store.workspaces():
            by_client.setdefault(w.client_id, []).append(self._workspace_card(w))
        return tuple(
            ClientCard(id=client,
                       name=(named.get(client) or {}).get("name", UNNAMED),
                       organization=(named.get(client) or {}).get(
                           "organization", self.organization),
                       workspaces=tuple(sorted(cards, key=lambda c: c.name)))
            for client, cards in sorted(by_client.items()))

    def workspaces(self) -> tuple[WorkspaceCard, ...]:
        return tuple(self._workspace_card(w) for w in self.store.workspaces())

    def _client_of(self, client_id: str) -> dict:
        """Como este cliente se chama, ou o silencio admitido."""
        for row in self.store.clients():
            if row["id"] == client_id:
                return row
        return {}

    def _workspace_card(self, w) -> WorkspaceCard:
        tasks = self.store.tasks(w.id)
        report = self._health(w.id, w.name)
        named = self._client_of(w.client_id)
        return WorkspaceCard(
            id=w.id, name=w.name, client=named.get("name", UNNAMED),
            client_id=w.client_id,
            organization=named.get("organization", self.organization),
            autonomy=w.max_autonomy.name,
            tasks=len(tasks), active_runs=len(self.store.active_runs(w.id)),
            needs_human=len(self.store.open_approvals(w.id)),
            health=report.level)

    # ---- painel -----------------------------------------------------
    def overview(self, workspace_id: str) -> Overview | None:
        w = self.store.workspace(workspace_id)
        if w is None:
            return None
        at = self.clock()
        tasks = self.store.tasks(workspace_id)
        runs = self.store.active_runs(workspace_id)
        leases = self.store.leases(workspace_id)
        live = [l for l in leases if l.expires_at and l.expires_at > at]
        events = self.store.events(workspace_id, limit=200)

        counts: dict[str, int] = {}
        for t in tasks:
            counts[t.state.value] = counts.get(t.state.value, 0) + 1

        approvals = self.store.open_approvals(workspace_id)
        blocked = [t for t in tasks if t.state is TaskState.BLOCKED]
        in_flight = [t for t in tasks if t.state in AWAITING_EXTERNAL]

        # Estacionada: em estado ativo que ESTE motor nao sabe avancar. A
        # pergunta e feita a maquina de estados, nao a uma lista escrita de
        # memoria -- foi assim que `TESTING` ficou invisivel por um marco
        # inteiro.
        stalled = [t for t in tasks if is_terminus(t.state)]

        last_event = events[0].ts if events else None
        ticks = [e for e in events if e.kind == "tick_fim"]
        last_tick = ticks[0].ts if ticks else None

        return Overview(
            workspace_id=w.id, workspace_name=w.name,
            client=self._client_name(w),
            organization=self._client_of(w.client_id).get(
                "organization", self.organization),
            at=at,
            health=self._health(w.id, w.name),
            tasks_by_state=tuple(Counted(name=k, count=v)
                                 for k, v in sorted(counts.items())),
            total_tasks=len(tasks), active_runs=len(runs),
            workers=len({r.worker for r in runs if r.worker}),
            leases_live=len(live), leases_expired=len(leases) - len(live),
            needs_human=len(approvals), blocked=len(blocked),
            stalled=tuple(self._card(t, w, self._client_name(w))
                          for t in stalled[:20]),
            deliveries_in_flight=len(in_flight),
            last_activity=last_event,
            last_activity_age=_age(last_event, at)[1],
            last_tick=last_tick, last_tick_age=_age(last_tick, at)[1])

    def health(self, workspace_id: str) -> HealthView | None:
        w = self.store.workspace(workspace_id)
        return None if w is None else self._health(w.id, w.name)

    def _health(self, workspace_id: str, name: str) -> HealthView:
        report = health_module.inspect(
            self.store, workspace_id, workspace_name=name,
            areas_root=self.areas_root, budget_usd=self.budget_usd,
            max_dispatches=self.max_dispatches, when=self.clock())
        return HealthView(
            level=report.level.value, healthy=report.healthy, at=report.at,
            signals=tuple({"question": s.question, "level": s.level.value,
                           "detail": s.detail, "evidence": list(s.evidence)}
                          for s in report.signals),
            measurements=dict(report.measurements))

    # ---- tasks ------------------------------------------------------
    def tasks(self, workspace_id: str, state: str | None = None
              ) -> tuple[TaskCard, ...]:
        w = self.store.workspace(workspace_id)
        if w is None:
            return ()
        wanted = None
        if state:
            try:
                wanted = [TaskState(state)]
            except ValueError:
                # Um estado que nao existe devolve vazio, nunca tudo. Filtro
                # digitado errado que ignora o filtro mostra o board inteiro e
                # parece ter funcionado.
                return ()
        client = self._client_name(w)
        return tuple(self._card(t, w, client)
                     for t in self.store.tasks(workspace_id, wanted))

    def task(self, workspace_id: str, task_id: str) -> TaskDetail | None:
        w = self.store.workspace(workspace_id)
        if w is None:
            return None
        task = self.store.task(task_id, workspace_id)
        if task is None:
            return None

        runs = self.store.task_runs(task.id, workspace_id)
        current = next((r for r in runs if r.state is RunState.RUNNING), None)
        finished = [r for r in runs if r.state is not RunState.RUNNING]
        last = finished[-1] if finished else None

        events = self.store.events(workspace_id, task_id=task.id, limit=500)
        rows = self.store.deliveries(workspace_id, task.key)
        approvals = [a for a in self.store.open_approvals(workspace_id)
                     if a.task_id == task.id]

        return TaskDetail(
            card=self._card(task, w),
            previous_state=(task.paused_at.value if task.paused_at else ""),
            description=task.description,
            resources=tuple(task.resources),
            current_run=self._run_card(current, w) if current else None,
            last_run=self._run_card(last, w) if last else None,
            last_result=(last.reason if last else ""),
            target=self._target(workspace_id, task.key),
            deliveries=tuple(self._delivery(r) for r in rows),
            blockers=self._task_blockers(task, rows, approvals, events),
            escalation=(self._escalation(approvals[0], task.key)
                        if approvals else None),
            timeline=tuple(self._entry(e) for e in reversed(events)))

    def _card(self, task: Task, w, client: str | None = None) -> TaskCard:
        return TaskCard(
            id=task.id, key=task.key, title=task.title, workspace_id=w.id,
            workspace_name=w.name,
            client=client if client is not None else self._client_name(w),
            state=self._state_of(task),
            priority=task.priority,
            risk=(task.risk.name if task.risk else ""),
            attempts=task.attempts,
            repository=str(task.data.get("repository") or ""),
            created_at=task.created_at, updated_at=task.updated_at,
            needs_human=task.state is TaskState.WAITING_HUMAN,
            blocked=task.state is TaskState.BLOCKED)

    def _state_of(self, task: Task) -> StateView:
        """Estado com dono e proximo passo, derivados da maquina de estados.

        `owner` responde a pergunta que a tela precisa fazer antes de qualquer
        outra: isto anda sozinho, ou esta esperando por mim? As quatro respostas
        vem de conjuntos que o Core ja mantem, nao de uma lista paralela aqui.
        """
        state = task.state
        if state is TaskState.WAITING_HUMAN:
            owner, action = "human", "uma decisao esta na fila"
        elif state in TERMINAL:
            owner, action = "nobody", ""
        elif state in AWAITING_EXTERNAL:
            owner, action = "external", "o motor le o resultado a cada tick"
        elif is_terminus(state):
            owner, action = ("nobody",
                             "este motor nao tem etapa que avance daqui; "
                             "precisa de uma pessoa")
        elif engine_can_advance(state):
            owner, action = "engine", "o proximo tick pega"
        else:
            owner, action = "engine", ""

        seconds, human = _age(task.updated_at, self.clock())
        return StateView(
            name=state.value, meaning=describe(state),
            reason=str(task.data.get("state_reason") or ""),
            since=task.updated_at, age_seconds=seconds, age=human,
            owner=owner, next_action=action)

    # ---- runs -------------------------------------------------------
    def runs(self, workspace_id: str, limit: int = 100) -> tuple[RunCard, ...]:
        w = self.store.workspace(workspace_id)
        if w is None:
            return ()
        seen = list(self.store.active_runs(workspace_id))
        for state in (RunState.SUCCEEDED, RunState.FAILED, RunState.ABORTED,
                      RunState.INTERRUPTED):
            seen += self.store.runs_in_state(workspace_id, state.value,
                                             limit=limit)
        seen.sort(key=lambda r: r.started_at or self.clock(), reverse=True)
        client = self._client_name(w)
        return tuple(self._run_card(r, w, client) for r in seen[:limit])

    def run(self, workspace_id: str, run_id: str) -> RunDetail | None:
        w = self.store.workspace(workspace_id)
        if w is None:
            return None
        run = self.store.run(run_id, workspace_id)
        if run is None:
            return None

        at = self.clock()
        held = tuple(l.resource for l in self.store.leases(workspace_id)
                     if l.owner == run.id and l.expires_at and l.expires_at > at)
        rows = [r for r in self.store.deliveries(workspace_id)
                if r.get("run_id") == run.id]
        events = [e for e in self.store.events(workspace_id, limit=500)
                  if e.run_id == run.id]

        return RunDetail(
            run=self._run_card(run, w), workspace_name=w.name,
            repository=str(run.data.get("repository") or ""),
            workspace_path=run.workspace_path or "",
            iterations=run.iterations, tool_calls=run.tool_calls,
            leases=held,
            blockers=self._run_blockers(run, rows),
            delivery=self._delivery(rows[-1]) if rows else None,
            timeline=tuple(self._entry(e) for e in reversed(events)))

    def _client_name(self, w) -> str:
        return self._client_of(w.client_id).get("name", UNNAMED)

    def _run_card(self, run: Run, w, client: str | None = None) -> RunCard:
        seconds: int | None = None
        human = "em andamento"
        if run.ended_at and run.started_at:
            seconds = int(max(0.0, (run.ended_at - run.started_at).total_seconds()))
            human = _age(run.started_at, run.ended_at)[1]
        elif run.started_at:
            seconds, human = _age(run.started_at, self.clock())
        return RunCard(
            id=run.id, task_key=run.task_key or run.task_id, task_id=run.task_id,
            workspace_id=run.workspace_id,
            client=client if client is not None else self._client_name(w),
            agent=run.agent,
            state=run.state.value, started_at=run.started_at,
            ended_at=run.ended_at, duration_seconds=seconds, duration=human,
            reason=run.reason, cost_usd=run.cost_usd, tokens=run.tokens,
            branch=run.branch or "")

    # ---- entrega ----------------------------------------------------
    def deliveries(self, workspace_id: str,
                   task_key: str | None = None) -> tuple[DeliveryView, ...]:
        if self.store.workspace(workspace_id) is None:
            return ()
        return tuple(self._delivery(r)
                     for r in self.store.deliveries(workspace_id, task_key))

    def _delivery(self, row: dict) -> DeliveryView:
        """A linha do banco vira cadeia, sem completar o que falta.

        `pull_request_absent` so e preenchido quando realmente nao ha PR -- e
        diz o que se sabe, que e pouco: a linha guarda o que aconteceu, nao o
        motivo de algo nao ter acontecido. Inventar um motivo aqui seria pior
        que admitir que ele nao foi gravado.
        """
        number = row.get("pr_number")
        return DeliveryView(
            id=str(row.get("id") or ""),
            task_key=str(row.get("task_key") or ""),
            run_id=str(row.get("run_id") or ""),
            repository=str(row.get("repo_key") or ""),
            branch=str(row.get("branch") or ""),
            commit_sha=str(row.get("commit_sha") or ""),
            pushed=bool(row.get("pushed_at")),
            push_target=str(row.get("push_target") or ""),
            pull_request=int(number) if number else None,
            pull_request_url=str(row.get("pr_url") or ""),
            pull_request_head=str(row.get("pr_head_sha") or ""),
            pull_request_absent=("" if number else
                                 ("empurrado, sem pull request registrado"
                                  if row.get("pushed_at")
                                  else "nada chegou ao remoto")),
            ci_state=str(row.get("ci_state") or "NOT_OBSERVED"),
            ci_result=str(row.get("ci_result") or ""),
            ci_reason=str(row.get("ci_reason") or ""),
            ci_observations=int(row.get("ci_observations") or 0),
            ci_observed_at=_when(row.get("ci_observed_at")),
            created_at=_when(row.get("created_at")))

    # ---- bloqueios --------------------------------------------------
    def _task_blockers(self, task: Task, rows: list[dict],
                       approvals: list[Approval],
                       events: list[Event]) -> tuple[Blocker, ...]:
        """O que impede esta task de andar, dito por quem sabe.

        Nada aqui e deduzido de nome, convencao ou heuristica: cada bloqueio sai
        de um registro -- um estado, uma aprovacao aberta, uma coluna de CI, um
        evento gravado pelo motor.
        """
        found: list[Blocker] = []
        at = self.clock()

        for a in approvals:
            found.append(Blocker(
                kind="HUMAN", summary=a.what_happened,
                detail=a.why_it_matters, since=a.created_at,
                action="decida na fila"))

        if task.state is TaskState.BLOCKED:
            found.append(Blocker(
                kind="BLOCKED", summary="a task esta bloqueada",
                detail=self._last_reason(events, task) or UNRECORDED,
                since=task.updated_at))

        if is_terminus(task.state):
            found.append(Blocker(
                kind="NO_ROUTE",
                summary=f"{task.state.value} nao tem etapa de saida neste motor",
                detail=self._last_reason(events, task) or UNRECORDED,
                since=task.updated_at,
                action="precisa de uma pessoa"))

        for row in rows:
            found += self._ci_blockers(row, at)

        found += self._agent_blockers(events)
        return tuple(found)

    def _run_blockers(self, run: Run, rows: list[dict]) -> tuple[Blocker, ...]:
        found: list[Blocker] = []
        if run.state in (RunState.FAILED, RunState.ABORTED,
                         RunState.INTERRUPTED):
            found.append(Blocker(
                kind=run.state.value, summary=f"run {run.state.value}",
                detail=run.reason or UNRECORDED, since=run.ended_at))
        for row in rows:
            found += self._ci_blockers(row, self.clock())
        return tuple(found)

    @staticmethod
    def _ci_blockers(row: dict, at: datetime) -> list[Blocker]:
        """CI que nao concluiu e um bloqueio, e nunca um sucesso silencioso."""
        state = str(row.get("ci_state") or "")
        if not state or state == "CONCLUDED":
            return []
        return [Blocker(
            kind=f"CI_{state}",
            summary={"PENDING": "os checks ainda nao concluiram",
                     "UNAVAILABLE": "o provedor de CI nao respondeu",
                     "NO_CHECKS": "nao havia check nenhum para rodar",
                     }.get(state, f"CI em {state}"),
            detail=str(row.get("ci_reason") or UNRECORDED),
            since=_when(row.get("ci_observed_at")),
            action=("" if state == "PENDING" else "precisa de uma pessoa"))]

    @staticmethod
    def _agent_blockers(events: list[Event]) -> list[Blocker]:
        """Prontidao do agente, quando o motor a gravou.

        O texto vem do evento; a leitura de prontidao pertence ao diagnostico do
        motor. Reimplementar aqui produziria uma segunda opiniao sobre estar
        autenticado, e a da tela seria a que nao tentou.
        """
        for e in events:
            blocked = str(e.data.get("readiness") or e.data.get("blocked") or "")
            if blocked.startswith("BLOCKED_"):
                return [Blocker(
                    kind=blocked, summary=e.summary or blocked,
                    detail=str(e.data.get("detail") or ""), since=e.ts,
                    action=str(e.data.get("action") or ""))]
        return []

    @staticmethod
    def _last_reason(events: list[Event], task: Task) -> str:
        for e in events:
            if e.kind in ("transicao", "transition") and e.summary:
                return e.summary
        return ""

    # ---- fila humana ------------------------------------------------
    def escalations(self, workspace_id: str) -> tuple[EscalationView, ...]:
        if self.store.workspace(workspace_id) is None:
            return ()
        out = []
        for a in self.store.open_approvals(workspace_id):
            task = self.store.task(a.task_id, workspace_id)
            out.append(self._escalation(a, task.key if task else a.task_id))
        return tuple(out)

    def _escalation(self, a: Approval, task_key: str) -> EscalationView:
        seconds, human = _age(a.created_at, self.clock())
        return EscalationView(
            id=a.id, task_id=a.task_id, task_key=task_key,
            run_id=a.run_id or "", what_happened=a.what_happened,
            why_it_matters=a.why_it_matters,
            what_was_tried=tuple(a.what_was_tried),
            options=tuple({"id": o.id, "label": o.label, "effect": o.effect}
                          for o in a.options),
            recommendation=a.recommendation or "",
            risk=a.risk.name, created_at=a.created_at,
            waiting_seconds=seconds, waiting=human)

    # ---- eventos ----------------------------------------------------
    def events(self, workspace_id: str, limit: int = 100
               ) -> tuple[TimelineEntry, ...]:
        if self.store.workspace(workspace_id) is None:
            return ()
        return tuple(self._entry(e)
                     for e in self.store.events(workspace_id, limit=limit))

    @staticmethod
    def _entry(e: Event) -> TimelineEntry:
        return TimelineEntry(at=e.ts, kind=e.kind, actor=e.actor,
                             summary=e.summary, run_id=e.run_id or "",
                             data=dict(e.data))

    # ---- alvo -------------------------------------------------------
    def _target(self, workspace_id: str, task_key: str) -> TargetView | None:
        rows = self.store.targets(workspace_id, task_key)
        if not rows:
            return None
        row = rows[-1]
        return TargetView(
            repository=str(row.get("repo_key") or ""),
            provider=str(row.get("provider") or ""),
            source=str(row.get("source") or ""),
            confidence=str(row.get("confidence") or "UNKNOWN"),
            strength=float(row.get("strength") or 0.0),
            validated=str(row.get("source") or "") == "VALIDATED",
            confirmations=int(row.get("confirmations") or 0),
            evidence=tuple(row.get("evidence") or ()),
            alternatives=tuple(row.get("alternatives") or ()))


def _when(raw: Any) -> datetime | None:
    """Converte um instante gravado, ou admite que nao foi possivel ler.

    Devolver `None` e a resposta honesta para um texto ilegivel. A alternativa
    -- devolver agora -- faria um registro corrompido parecer recentissimo.
    """
    if isinstance(raw, datetime):
        return raw
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
