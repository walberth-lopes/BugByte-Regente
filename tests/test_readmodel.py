# -*- coding: utf-8 -*-
"""A camada de leitura, testada contra a unica coisa que ela pode errar: mentir.

Um read model nao quebra ruidosamente. Ele devolve uma resposta bem formada e
errada, e a tela a mostra com confianca. Entao os testes aqui nao perguntam "o
campo existe?" -- perguntam "o campo diz a verdade quando a verdade e
desconfortavel?".

As tres perguntas que se repetem:

* ausencia continua ausencia? (sem PR, sem CI, sem evento, sem motivo)
* `UNKNOWN` continua `UNKNOWN`? (nunca saudavel, nunca verde)
* o escopo aguenta um id valido do tenant errado?
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from regente.core import ids
from regente.core.model import (Event, ExternalRef, Option, Run, RunState, Task,
                                Workspace)
from regente.core.policy import AutonomyLevel
from regente.core.risk import RiskLevel
from regente.core.states import TaskState
from regente.engine import escalation
from regente.engine.readmodel import ReadModel, UNNAMED, UNRECORDED
from regente.engine.store_sqlite import SqliteStore

T0 = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 40


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "rm.db", clock=lambda: T0)
    s.migrate()
    yield s
    s.close()


def a_workspace(store, wid="wks_a", client="cli_a", name="main",
                client_name="Acme") -> Workspace:
    store.save_client(client, "org", client_name)
    w = Workspace(id=wid, client_id=client, name=name,
                  max_autonomy=AutonomyLevel.L3)
    store.save_workspace(w)
    return w


def a_task(store, wid="wks_a", key="K-1", state=TaskState.READY,
           title="fazer a coisa") -> Task:
    task = Task(id=ids.new_id(ids.TASK), workspace_id=wid, project_id="prj",
                title=title, state=TaskState.READY, risk=RiskLevel.MEDIUM,
                externo=ExternalRef(provider="filesystem", key=key))
    store.save_task(task)
    road = {
        TaskState.READY: (),
        TaskState.IMPLEMENTING: (TaskState.ASSIGNED, TaskState.IMPLEMENTING),
        TaskState.TESTING: (TaskState.ASSIGNED, TaskState.IMPLEMENTING,
                            TaskState.TESTING),
        TaskState.CI_RUNNING: (TaskState.ASSIGNED, TaskState.IMPLEMENTING,
                               TaskState.TESTING, TaskState.PR_CREATED,
                               TaskState.CI_RUNNING),
        TaskState.BLOCKED: (TaskState.BLOCKED,),
        TaskState.WAITING_HUMAN: (TaskState.ASSIGNED, TaskState.WAITING_HUMAN),
        TaskState.DONE: (TaskState.ASSIGNED, TaskState.IMPLEMENTING,
                         TaskState.TESTING, TaskState.PR_CREATED,
                         TaskState.CI_RUNNING, TaskState.AI_REVIEW,
                         TaskState.WAITING_HUMAN, TaskState.APPROVED,
                         TaskState.MERGING, TaskState.DONE),
    }[state]
    for step in road:
        store.transition(task.id, step, actor="test", reason=f"para {step.value}",
                         workspace_id=wid)
    return store.task(task.id, wid)


def read(store, **kw) -> ReadModel:
    return ReadModel(store=store, clock=lambda: T0, **kw)


# ---------------------------------------------------------------------------
# Ausencia continua ausencia
# ---------------------------------------------------------------------------

def test_a_delivery_without_a_pull_request_says_so_and_never_shows_one(store):
    a_workspace(store)
    a_task(store, key="K-1")
    row = store.open_delivery("wks_a", "K-1", "run_1", "github", "acme/api",
                              "regente/k-1", SHA)
    store.record_push(row, "https://example.invalid/acme/api.git")

    d = read(store).deliveries("wks_a")[0]

    assert d.pull_request is None
    assert d.pull_request_url == ""
    assert "sem pull request" in d.pull_request_absent
    assert d.as_dict()["pull_request"] is None


def test_a_delivery_that_never_reached_the_remote_says_that_instead(store):
    a_workspace(store)
    a_task(store, key="K-1")
    store.open_delivery("wks_a", "K-1", "run_1", "github", "acme/api",
                        "regente/k-1", SHA)

    d = read(store).deliveries("wks_a")[0]
    assert d.pushed is False
    assert d.pull_request_absent == "nada chegou ao remoto"


def test_ci_never_observed_is_not_green_and_says_it_was_not_observed(store):
    a_workspace(store)
    a_task(store, key="K-1")
    store.open_delivery("wks_a", "K-1", "run_1", "github", "acme/api",
                        "regente/k-1", SHA)

    d = read(store).deliveries("wks_a")[0]
    assert d.ci_state == "NOT_OBSERVED"
    assert d.ci_result == ""
    assert d.ci_is_green is False
    assert d.as_dict()["ci_green"] is False


@pytest.mark.parametrize("state,result", [
    ("PENDING", None), ("UNAVAILABLE", None), ("NO_CHECKS", None),
    ("CONCLUDED", "UNKNOWN"), ("CONCLUDED", "REGRESSION"),
    ("CONCLUDED", "FAILED"),
])
def test_no_ci_short_of_a_real_pass_is_ever_green(store, state, result):
    """A regra que a tela nao pode ter de conhecer.

    `NO_CHECKS` e o caso perigoso: nada rodou, e a ausencia de vermelho parece
    verde para qualquer leitor apressado -- inclusive para um `if not red`.
    """
    a_workspace(store)
    a_task(store, key="K-1")
    row = store.open_delivery("wks_a", "K-1", "run_1", "github", "acme/api",
                              "regente/k-1", SHA)
    store.record_ci(row, state=state, result=result, reason="motivo", checks=[])

    d = read(store).deliveries("wks_a")[0]
    assert d.ci_is_green is False, f"{state}/{result} apareceu como verde"
    assert d.as_dict()["ci_green"] is False


def test_a_task_with_no_events_gets_an_empty_timeline_not_an_invented_one(store):
    a_workspace(store)
    task = a_task(store, key="K-1")
    detail = read(store).task("wks_a", task.id)

    invented = [e for e in detail.timeline
                if e.kind in ("pull_request", "ci", "merge", "deploy")]
    assert not invented, f"a linha do tempo inventou etapas: {invented}"
    assert detail.deliveries == ()
    assert detail.current_run is None and detail.last_run is None


def test_a_state_without_a_recorded_reason_says_so_instead_of_going_blank(store):
    """Campo vazio numa tela le-se como 'nada de errado'."""
    a_workspace(store)
    task = a_task(store, key="K-1", state=TaskState.BLOCKED)
    detail = read(store).task("wks_a", task.id)

    blocked = [b for b in detail.blockers if b.kind == "BLOCKED"]
    assert blocked and blocked[0].detail
    assert blocked[0].detail != ""


def test_a_client_nobody_named_is_not_shown_as_its_own_id(store):
    w = Workspace(id="wks_x", client_id="cli_opaco", name="main",
                  max_autonomy=AutonomyLevel.L2)
    store.save_workspace(w)

    card = read(store).workspaces()[0]
    assert card.client == UNNAMED
    assert card.client_id == "cli_opaco"
    assert card.client != "cli_opaco", (
        "um id opaco no lugar do nome parece um nome escolhido por alguem")


# ---------------------------------------------------------------------------
# Estado exibido corresponde ao estado real
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", list(TaskState))
def test_every_state_carries_a_name_a_meaning_and_an_owner(store, state):
    """Nenhum estado pode chegar a tela dependendo so de cor."""
    from regente.core.states import describe

    a_workspace(store)
    task = Task(id=ids.new_id(ids.TASK), workspace_id="wks_a", project_id="p",
                title="t", state=state,
                externo=ExternalRef(provider="filesystem", key=f"K-{state.value}"))
    store.save_task(task)

    card = read(store)._card(task, store.workspace("wks_a"))
    assert card.state.name == state.value
    assert card.state.meaning == describe(state) != ""
    assert card.state.owner in ("engine", "human", "external", "nobody")
    assert card.state.age


def test_a_task_stuck_in_testing_is_owned_by_nobody_and_says_why(store):
    """O achado do marco 8, agora visivel sem terminal."""
    a_workspace(store)
    task = a_task(store, key="K-1", state=TaskState.TESTING)

    card = read(store).tasks("wks_a")[0]
    assert card.state.name == "TESTING"
    assert card.state.owner == "nobody"
    assert "nao tem etapa" in card.state.next_action

    detail = read(store).task("wks_a", task.id)
    assert any(b.kind == "NO_ROUTE" for b in detail.blockers)


def test_a_task_waiting_on_ci_is_owned_by_the_outside_not_by_a_person(store):
    """Confundir espera com decisao humana faz a fila humana mentir."""
    a_workspace(store)
    a_task(store, key="K-1", state=TaskState.CI_RUNNING)

    card = read(store).tasks("wks_a")[0]
    assert card.state.owner == "external"
    assert card.needs_human is False


def test_a_task_waiting_for_a_person_is_owned_by_a_person(store):
    a_workspace(store)
    a_task(store, key="K-1", state=TaskState.WAITING_HUMAN)
    card = read(store).tasks("wks_a")[0]
    assert card.state.owner == "human" and card.needs_human is True


def test_a_state_filter_that_does_not_exist_returns_nothing_not_everything(store):
    """Filtro digitado errado que mostra o board inteiro parece ter funcionado."""
    a_workspace(store)
    a_task(store, key="K-1")
    a_task(store, key="K-2")

    assert len(read(store).tasks("wks_a")) == 2
    assert read(store).tasks("wks_a", "NAO_EXISTE") == ()


def test_the_counts_on_the_overview_match_the_tasks_that_exist(store):
    a_workspace(store)
    a_task(store, key="K-1", state=TaskState.READY)
    a_task(store, key="K-2", state=TaskState.TESTING)
    a_task(store, key="K-3", state=TaskState.TESTING)

    o = read(store).overview("wks_a")
    counted = {c.name: c.count for c in o.tasks_by_state}
    assert counted == {"READY": 1, "TESTING": 2}
    assert o.total_tasks == 3
    assert len(o.stalled) == 2


# ---------------------------------------------------------------------------
# Bloqueios sao repassados, nunca reconstruidos
# ---------------------------------------------------------------------------

def test_a_recorded_authentication_block_reaches_the_reader_verbatim(store):
    """O motor sabe por que parou. A leitura repete; nao redescobre.

    Reimplementar a leitura de prontidao aqui produziria uma segunda opiniao
    sobre estar autenticado -- e a da tela seria a que nunca tentou.
    """
    a_workspace(store)
    task = a_task(store, key="K-1")
    store.record_event(Event(
        id=ids.new_id(ids.EVENT), workspace_id="wks_a", kind="agente_indisponivel",
        task_id=task.id, summary="o agente configurado nao esta autenticado",
        data={"readiness": "BLOCKED_AUTHENTICATION",
              "detail": "a ferramenta diz que nao esta logada",
              "action": "autentique o agente configurado"}))

    detail = read(store).task("wks_a", task.id)
    blocked = [b for b in detail.blockers if b.kind == "BLOCKED_AUTHENTICATION"]
    assert blocked, [b.kind for b in detail.blockers]
    assert blocked[0].action == "autentique o agente configurado"
    assert "nao esta logada" in blocked[0].detail


def test_blocked_authentication_never_arrives_as_ready(store):
    a_workspace(store)
    task = a_task(store, key="K-1")
    store.record_event(Event(
        id=ids.new_id(ids.EVENT), workspace_id="wks_a", kind="agente",
        task_id=task.id, summary="bloqueado",
        data={"readiness": "BLOCKED_AUTHENTICATION"}))

    payload = read(store).task("wks_a", task.id).as_dict()
    text = str(payload)
    assert "BLOCKED_AUTHENTICATION" in text
    assert "READY" not in [b["kind"] for b in payload["blockers"]]


def test_ci_that_did_not_conclude_is_a_blocker_not_a_silent_pass(store):
    a_workspace(store)
    task = a_task(store, key="K-1")
    row = store.open_delivery("wks_a", "K-1", "run_1", "github", "acme/api",
                              "regente/k-1", SHA)
    store.record_ci(row, state="UNAVAILABLE", result=None,
                    reason="o provedor nao respondeu", checks=[])

    detail = read(store).task("wks_a", task.id)
    assert any(b.kind == "CI_UNAVAILABLE" for b in detail.blockers)


def test_an_open_escalation_appears_as_a_human_blocker_with_its_briefing(store):
    a_workspace(store)
    task = a_task(store, key="K-1", state=TaskState.WAITING_HUMAN)
    store.open_approval(escalation.build(
        task=task, what_happened="o CI ficou vermelho",
        why_it_matters="a mudanca nao pode seguir assim",
        attempts=("rodou duas vezes",), risk=RiskLevel.HIGH))

    detail = read(store).task("wks_a", task.id)
    human = [b for b in detail.blockers if b.kind == "HUMAN"]
    assert human and human[0].summary == "o CI ficou vermelho"
    assert detail.escalation is not None
    assert detail.escalation.what_was_tried == ("rodou duas vezes",)
    assert detail.escalation.risk == "HIGH"


# ---------------------------------------------------------------------------
# Saude vem do motor, nao de uma segunda implementacao
# ---------------------------------------------------------------------------

def test_health_is_the_engines_own_verdict(store):
    from regente.engine import health as health_module

    a_workspace(store)
    a_task(store, key="K-1", state=TaskState.TESTING)

    mine = read(store).health("wks_a")
    theirs = health_module.inspect(store, "wks_a", "main", when=T0)
    assert mine.level == theirs.level.value
    assert mine.healthy == theirs.healthy
    assert len(mine.signals) == len(theirs.signals)


def test_an_unknown_signal_never_reads_as_healthy(store):
    """`UNKNOWN` e mais grave que `ATTENTION` de proposito: nao examinado nao
    e o mesmo que examinado e limpo."""
    a_workspace(store)
    view = read(store).health("wks_a")
    if view.level == "UNKNOWN":
        assert view.healthy is False


def test_a_stuck_workspace_is_not_reported_as_healthy(store):
    a_workspace(store)
    a_task(store, key="K-1", state=TaskState.TESTING)

    view = read(store).health("wks_a")
    assert view.level == "STUCK"
    assert view.healthy is False
    assert read(store).workspaces()[0].health == "STUCK"


# ---------------------------------------------------------------------------
# Escopo
# ---------------------------------------------------------------------------

def test_a_valid_id_from_the_wrong_tenant_reads_as_absent(store):
    """Conhecer o id nao pode ser autoridade para le-lo."""
    a_workspace(store, "wks_a", "cli_a", "main", "Acme")
    a_workspace(store, "wks_b", "cli_b", "main", "Beta")
    mine = a_task(store, "wks_a", key="SAME-1")
    theirs = a_task(store, "wks_b", key="SAME-1")

    rm = read(store)
    assert rm.task("wks_a", mine.id) is not None
    assert rm.task("wks_a", theirs.id) is None
    assert rm.task("wks_b", mine.id) is None


def test_a_run_of_another_tenant_is_not_reachable_by_id(store):
    a_workspace(store, "wks_a")
    a_workspace(store, "wks_b", "cli_b", "main", "Beta")
    store.save_run(Run(id="run_b", task_id="", task_key="SAME-1",
                       workspace_id="wks_b", agent="coder"))

    rm = read(store)
    assert rm.run("wks_b", "run_b") is not None
    assert rm.run("wks_a", "run_b") is None


def test_a_delivery_of_one_tenant_never_appears_in_the_other(store):
    a_workspace(store, "wks_a")
    a_workspace(store, "wks_b", "cli_b", "main", "Beta")
    a_task(store, "wks_a", key="SAME-1")
    a_task(store, "wks_b", key="SAME-1")
    store.open_delivery("wks_a", "SAME-1", "run_a", "github", "acme/api",
                        "regente/x", SHA)
    store.open_delivery("wks_b", "SAME-1", "run_b", "github", "acme/api",
                        "regente/x", "b" * 40)

    rm = read(store)
    assert [d.commit_sha for d in rm.deliveries("wks_a", "SAME-1")] == [SHA]
    assert [d.commit_sha for d in rm.deliveries("wks_b", "SAME-1")] == ["b" * 40]


def test_a_workspace_that_does_not_exist_answers_empty_not_everything(store):
    a_workspace(store)
    a_task(store, key="K-1")

    rm = read(store)
    assert rm.overview("wks_ghost") is None
    assert rm.tasks("wks_ghost") == ()
    assert rm.runs("wks_ghost") == ()
    assert rm.deliveries("wks_ghost") == ()
    assert rm.events("wks_ghost") == ()
    assert rm.escalations("wks_ghost") == ()
    assert rm.health("wks_ghost") is None


def test_the_read_model_never_calls_the_store_without_a_scope():
    """A guarda arquitetural, verificada no codigo-fonte.

    Uma leitura escopada e uma nao-escopada tem a mesma cara na revisao de
    codigo: uma virgula de diferenca. Aqui a diferenca e um teste.
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "regente/engine/readmodel.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    scoped = {"task": 2, "run": 2, "task_runs": 2, "approval": 2,
              "events": 1, "tasks": 1, "deliveries": 1, "targets": 1,
              "leases": 1, "active_runs": 1, "open_approvals": 1,
              "workspace": 1, "runs_in_state": 2}
    naked = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute)
                and f.value.attr == "store"):
            continue
        need = scoped.get(f.attr)
        if need is not None and len(node.args) + len(node.keywords) < need:
            naked.append(f"linha {node.lineno}: store.{f.attr} sem escopo")
    assert not naked, "leitura sem tenant no read model:\n  " + "\n  ".join(naked)


# ---------------------------------------------------------------------------
# Cadeia e identidade
# ---------------------------------------------------------------------------

def test_a_run_detail_answers_every_identity_question(store):
    a_workspace(store)
    task = a_task(store, key="K-1", state=TaskState.TESTING)
    store.save_run(Run(id="run_1", task_id=task.id, task_key="K-1",
                       workspace_id="wks_a", agent="coder",
                       state=RunState.SUCCEEDED, branch="regente/k-1",
                       workspace_path="/areas/K-1",
                       started_at=T0 - timedelta(minutes=9), ended_at=T0))
    row = store.open_delivery("wks_a", "K-1", "run_1", "github", "acme/api",
                              "regente/k-1", SHA)
    store.record_push(row, "https://example.invalid/acme/api.git")

    d = read(store).run("wks_a", "run_1")
    for question, value in (
            ("qual workspace", d.run.workspace_id),
            ("qual cliente", d.run.client),
            ("qual task", d.run.task_key),
            ("qual agente", d.run.agent),
            ("qual branch", d.run.branch),
            ("qual area", d.workspace_path),
            ("quanto durou", d.run.duration)):
        assert value, f"o run nao responde: {question}"
    assert d.delivery is not None and d.delivery.commit_sha == SHA
    assert d.run.duration == "9min"


def test_a_run_is_linked_from_its_task_in_both_directions(store):
    a_workspace(store)
    task = a_task(store, key="K-1", state=TaskState.TESTING)
    store.save_run(Run(id="run_1", task_id=task.id, task_key="K-1",
                       workspace_id="wks_a", agent="coder",
                       state=RunState.SUCCEEDED, reason="verde"))

    detail = read(store).task("wks_a", task.id)
    assert detail.last_run is not None and detail.last_run.id == "run_1"
    assert detail.last_result == "verde"
    assert read(store).run("wks_a", "run_1").run.task_id == task.id


def test_a_run_holding_no_lease_says_so_rather_than_showing_nothing(store):
    a_workspace(store)
    store.save_run(Run(id="run_1", task_id="", task_key="K-1",
                       workspace_id="wks_a", agent="coder"))
    assert read(store).run("wks_a", "run_1").leases == ()


def test_the_reader_answers_budget_with_the_same_ceilings_the_terminal_uses(store):
    """Duas superficies, uma resposta.

    Sem os tetos, a pergunta de orcamento responde `UNKNOWN` na tela e um numero
    no terminal -- a mesma pergunta com duas respostas, e a menos informada e a
    que o operador olha. Encontrado abrindo a Mission Control sobre dados reais.
    """
    from regente.engine import health as health_module

    a_workspace(store)
    view = ReadModel(store=store, clock=lambda: T0,
                     budget_usd=5.0, max_dispatches=20).health("wks_a")
    theirs = health_module.inspect(store, "wks_a", "main", budget_usd=5.0,
                                   max_dispatches=20, when=T0)
    mine = next(s for s in view.signals if s["question"] == "budget")
    engine = next(s for s in theirs.signals if s.question == "budget")
    assert mine["detail"] == engine.detail
    assert mine["level"] == engine.level.value != "UNKNOWN"


def test_a_blocked_task_with_no_transition_event_still_states_a_reason(store):
    """O caso que a fallback existe para cobrir.

    A primeira versao deste teste passava com a fallback removida: a task tinha
    chegado a BLOCKED por transicao, e a transicao grava um evento com resumo.
    Uma task que JA NASCE bloqueada nao tem esse evento -- e e exatamente ali
    que um campo em branco apareceria na tela, lido como "nada de errado".
    """
    a_workspace(store)
    task = Task(id=ids.new_id(ids.TASK), workspace_id="wks_a", project_id="p",
                title="nasceu bloqueada", state=TaskState.BLOCKED,
                externo=ExternalRef(provider="filesystem", key="K-MUDA"))
    store.save_task(task)

    detail = read(store).task("wks_a", task.id)
    blocked = [b for b in detail.blockers if b.kind == "BLOCKED"]
    assert blocked, [b.kind for b in detail.blockers]
    assert blocked[0].detail == UNRECORDED
    assert blocked[0].detail != ""


def test_a_ghost_workspace_never_borrows_another_ones_tasks(store):
    """Escopo inexistente responde vazio, e vazio de verdade."""
    a_workspace(store)
    a_task(store, key="K-1")
    a_task(store, key="K-2")

    rm = read(store)
    assert rm.tasks("wks_ghost") == ()
    assert rm.runs("wks_ghost") == ()
    assert len(rm.tasks("wks_a")) == 2
