# -*- coding: utf-8 -*-
"""The engine end to end, against the product's success criteria.

Every test here corresponds to a promise: discover work, build the graph, find
parallelism, isolate workers, persist state, detect failure, ask for
intervention and **resume interrupted execution**.

"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from regente.adapters.notify.console import Console
from regente.adapters.runner.scripted import ScriptedRunner
from regente.adapters.tasks.filesystem import FilesystemTasks
from regente.adapters.workspace.local import IsolatedDirectory
from regente.core.model import RunState, Workspace, now
from regente.core.policy import AutonomyLevel, PolicyEngine
from regente.core.risk import RiskEngine
from regente.core.scheduling import Limits
from regente.core.states import TaskState
from regente.engine.gate import Gate
from regente.engine.orchestrator import Orchestrator
from regente.engine.store_sqlite import SqliteStore
from regente.ports import AdapterError


def write_task(folder: Path, key: str, **fields) -> None:
    data = {"key": key, "title": fields.pop("title", f"work {key}"),
            "status": fields.pop("status", "TO DO"), **fields}
    (folder / f"{key}.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


@pytest.fixture
def bench(tmp_path):
    """Assembles a complete engine on temporary disk."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    def make(script=None, limits=None, lease_seconds=900):
        store = SqliteStore(tmp_path / "regente.db")
        store.migrate()
        ws = Workspace(id="wks_teste", client_id="cli_teste", name="teste",
                       max_autonomy=AutonomyLevel.L3)
        store.save_workspace(ws)
        risk = RiskEngine()
        orq = Orchestrator(
            store=store, workspace=ws,
            tasks_provider=FilesystemTasks(tasks_dir),
            area_provider=IsolatedDirectory(tmp_path / "areas"),
            runner=ScriptedRunner(script=script or {}),
            gate=Gate(store=store, policy=PolicyEngine.from_config([]), risk=risk),
            risk=risk, limits=limits or Limits(max_workers=3),
            notificador=Console(journal=tmp_path / "jornal.log"),
            lease_seconds=lease_seconds)
        return orq, store

    make.tasks = tasks_dir
    make.root = tmp_path
    return make


# ---- discovery and baseline ---------------------------------------------

def test_first_pass_records_is_not_dispatches(bench):
    """Switching the engine on against a full backlog must not become a worker storm."""
    for k in ("A-1", "A-2", "A-3"):
        write_task(bench.tasks, k)
    orq, store = bench()
    rel = orq.tick()

    assert rel.baseline
    assert rel.discovered == 3
    assert not rel.dispatched
    assert all(t.state is TaskState.DISCOVERED for t in store.tasks("wks_teste"))


def test_segundo_tick_analisa_is_dispatches(bench):
    write_task(bench.tasks, "A-1", resources=["repo:x"])
    orq, store = bench()
    orq.tick()                      # baseline
    rel = orq.tick()

    assert not rel.baseline
    assert rel.dispatched == ("A-1",)
    assert store.tasks("wks_teste")[0].state is TaskState.TESTING


def test_task_new_not_reopen_baseline(bench):
    write_task(bench.tasks, "A-1")
    orq, store = bench()
    orq.tick()
    write_task(bench.tasks, "A-2")
    rel = orq.tick()
    assert not rel.baseline
    assert rel.discovered == 1


def test_task_not_duplicate_between_ticks(bench):
    write_task(bench.tasks, "A-1")
    orq, store = bench()
    for _ in range(3):
        orq.tick()
    assert len([t for t in store.tasks("wks_teste") if t.key == "A-1"]) == 1


def test_failure_of_adapter_not_becomes_absence_of_work(bench):
    """The error has to show. An empty board and a broken board are different things."""
    write_task(bench.tasks, "A-1")
    orq, store = bench()
    orq.tick()
    (bench.tasks / "quebrado.yaml").write_text("isto: [nao\n fecha", encoding="utf-8")
    rel = orq.tick()
    assert rel.errors
    assert "adapter" in rel.errors[0]


# ---- graph and parallelism -----------------------------------------------

def test_dependency_declared_becomes_graph(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    write_task(bench.tasks, "A-2", resources=["repo:b"])
    write_task(bench.tasks, "A-3", resources=["repo:c"],
                 depends_on=[{"key": "A-1"}, {"key": "A-2"}])
    orq, store = bench()
    orq.tick()

    plan = orq.plan()  # still in DISCOVERED: nothing ready
    assert not plan.dispatch

    orq._analyze(type("R", (), {"analyzed": 0})())
    plan = orq.plan()
    keys = {store.task(i).key for i in plan.dispatch}
    assert keys == {"A-1", "A-2"}, "A-3 depende das outras duas"


def test_resource_shared_serializes(bench):
    """Two tasks on the same migration must not go out in the same tick."""
    write_task(bench.tasks, "A-1", resources=["migration:api"], priority=1)
    write_task(bench.tasks, "A-2", resources=["migration:api"], priority=2)
    orq, store = bench(limits=Limits(max_workers=4))
    orq.tick()
    orq._analyze(type("R", (), {"analyzed": 0})())

    plan = orq.plan()
    assert len(plan.dispatch) == 1
    assert store.task(plan.dispatch[0]).key == "A-1"
    assert any("resource busy" in a.reason for a in plan.deferred)


def test_cycle_escalates_in_instead_of_blocking(bench):
    write_task(bench.tasks, "A-1", depends_on=[{"key": "A-2"}])
    write_task(bench.tasks, "A-2", depends_on=[{"key": "A-1"}])
    orq, store = bench()
    orq.tick()
    rel = orq.tick()

    assert set(rel.cycles) == {"A-1", "A-2"}
    assert not rel.dispatched
    open_items = store.open_approvals("wks_teste")
    assert len(open_items) == 1
    assert "circular" in open_items[0].what_happened


def test_cycle_not_reasking_the_each_tick(bench):
    write_task(bench.tasks, "A-1", depends_on=[{"key": "A-2"}])
    write_task(bench.tasks, "A-2", depends_on=[{"key": "A-1"}])
    orq, store = bench()
    for _ in range(4):
        orq.tick()
    assert len(store.open_approvals("wks_teste")) == 1


# ---- isolamento ----------------------------------------------------------

def test_each_worker_tem_area_own(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    write_task(bench.tasks, "A-2", resources=["repo:b"])
    orq, store = bench()
    orq.tick()
    orq.tick()

    areas = {r.workspace_path for r in
             [x for t in store.tasks("wks_teste") for x in store.task_runs(t.id)]}
    assert len(areas) == 2
    for a in areas:
        assert (Path(a) / "run.json").is_file()


# ---- persistence and resumption -----------------------------------------

def test_state_survives_to_process(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench()
    orq.tick()
    orq.tick()
    store.close()

    orq2, store2 = bench()          # new process, same database
    t = store2.tasks("wks_teste")[0]
    assert t.key == "A-1"
    assert t.state is TaskState.TESTING


def test_worker_dead_returns_the_task_to_the_queue(bench):
    """The criterion for death is the expired lease, not the absence of a process."""
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench(script={"A-1": {"ok": True, "resumo": "feito"}})
    orq.tick()

    # Simulates a worker that hung: a live run, a lease already expired.
    orq._analyze(type("R", (), {"analyzed": 0})())
    task = store.tasks("wks_teste")[0]
    store.transition(task.id, TaskState.ASSIGNED, actor="teste")
    from regente.core.ids import RUN, new_id
    from regente.core.model import Run
    run = Run(id=new_id(RUN), task_id=task.id, workspace_id="wks_teste", agent="coder")
    store.save_run(run)
    store.acquire_lease("repo:a", run.id, "wks_teste", seconds=1)
    store._con.execute("UPDATE leases SET expires_at=? WHERE resource=?",
                       ("2000-01-01T00:00:00.000000Z", "repo:a"))

    rel = orq.tick()
    assert rel.recovered == ("A-1",)
    assert store.run(run.id).state is RunState.INTERRUPTED
    # Recovering means returning to the queue, and the queue moves in the same
    # tick: the work starts moving on its own, without waiting for the next cycle
    # or for intervention.
    assert rel.dispatched == ("A-1",)
    assert store.task(task.id).state is TaskState.TESTING
    assert store.acquire_lease("repo:a", "outro", "wks_teste", 60) is not None, \
        "o lease do worker morto precisa ter sido solto"


def test_lease_prevents_two_owners(bench):
    orq, store = bench()
    assert store.acquire_lease("repo:a", "run_1", "wks_teste", 60) is not None
    assert store.acquire_lease("repo:a", "run_2", "wks_teste", 60) is None
    store.release_lease("repo:a", "run_1")
    assert store.acquire_lease("repo:a", "run_2", "wks_teste", 60) is not None


def test_lease_expired_can_ser_taken(bench):
    orq, store = bench()
    store.acquire_lease("repo:a", "run_1", "wks_teste", 60)
    store._con.execute("UPDATE leases SET expires_at=? WHERE resource=?",
                       ("2000-01-01T00:00:00.000000Z", "repo:a"))
    assert store.acquire_lease("repo:a", "run_2", "wks_teste", 60) is not None


# ---- failure, ladder and escalation --------------------------------------

def test_first_failure_retries_without_bothering_the_owner(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench(script={"A-1": {"ok": False, "resumo": "teste vermelho"}})
    orq.tick()
    rel = orq.tick()

    task = store.tasks("wks_teste")[0]
    assert task.state is TaskState.READY
    assert task.attempts == 1
    assert not rel.escalated, "falha unica e trabalho, nao pergunta"


def test_ladder_ends_is_escalates(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench(script={"A-1": {"ok": False, "resumo": "mesmo error"}})
    for _ in range(5):
        orq.tick()

    task = store.tasks("wks_teste")[0]
    assert task.state is TaskState.WAITING_HUMAN
    open_items = store.open_approvals("wks_teste")
    assert len(open_items) == 1
    assert open_items[0].why_it_matters
    assert open_items[0].what_was_tried


def test_worker_can_pedir_decision_human(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench(script={"A-1": {
        "ok": False, "desfecho": "precisa_humano",
        "resumo": "contrato da API publica e ambiguo",
        "pergunta": {"o_que_aconteceu": "contrato da API publica e ambiguo",
                     "por_que_importa": "escolher errado quebra cliente em producao",
                     "tentativas": ["li os dois consumidores", "procurei ADR"],
                     "recomendacao": "seguir"}}})
    orq.tick()
    rel = orq.tick()

    assert rel.escalated == ("A-1",)
    task = store.tasks("wks_teste")[0]
    assert task.state is TaskState.WAITING_HUMAN
    assert task.paused_at is TaskState.IMPLEMENTING

    a = store.open_approvals("wks_teste")[0]
    assert a.recommendation == "seguir"
    assert len(a.options) >= 3


def test_decision_human_is_recorded_is_resumes(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench(script={"A-1": {
        "ok": False, "desfecho": "precisa_humano", "resumo": "ambiguo",
        "pergunta": {"por_que_importa": "afeta contrato publico"}}})
    orq.tick()
    orq.tick()

    a = store.open_approvals("wks_teste")[0]
    decided = store.decide_approval(a.id, "seguir", by="walberth", note="manter compat")
    assert decided.choice == "seguir"
    assert not store.open_approvals("wks_teste")

    task = store.task(a.task_id)
    store.transition(task.id, TaskState.IMPLEMENTING, actor="walberth", reason="decidido")
    assert store.task(task.id).state is TaskState.IMPLEMENTING


def test_choice_outside_of_options_is_refused(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench(script={"A-1": {
        "ok": False, "desfecho": "precisa_humano", "resumo": "x",
        "pergunta": {"por_que_importa": "y"}}})
    orq.tick()
    orq.tick()
    a = store.open_approvals("wks_teste")[0]
    with pytest.raises(Exception):
        store.decide_approval(a.id, "opcao_inventada", by="walberth")


def test_worker_that_blowing_up_not_bringing_down_the_tick(bench):
    class Explode(ScriptedRunner):
        def run(self, request):
            raise RuntimeError("estourou")

    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench()
    orq.runner = Explode()
    orq.tick()
    rel = orq.tick()

    assert store.tasks("wks_teste")[0].state is TaskState.READY
    assert not rel.errors, "excecao do worker vira falha da task, nao error do tick"


# ---- trilha --------------------------------------------------------------

def test_timeline_tells_the_story(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench()
    orq.tick()
    orq.tick()

    task = store.tasks("wks_teste")[0]
    kinds = [e.kind for e in store.events("wks_teste", task_id=task.id, limit=50)]
    assert "descoberta" in kinds
    assert "despachada" in kinds
    assert kinds.count("transicao") >= 4


def test_every_transition_leaves_trail(bench):
    write_task(bench.tasks, "A-1")
    orq, store = bench()
    orq.tick()
    task = store.tasks("wks_teste")[0]
    store.transition(task.id, TaskState.ANALYZING, actor="teste", reason="porque sim")
    event = store.events("wks_teste", task_id=task.id, limit=1)[0]
    assert event.kind == "transicao"
    assert event.data["reason"] == "porque sim"
    assert event.actor == "teste"


def test_task_recovered_returns_the_ser_schedulable(bench):
    """The hole that nearly got through: recovering by changing the label without
    returning to the queue.

    A task that comes back from a crash into an ACTIVE state is dispatched by
    nobody -- it stays alive on paper and stopped in practice, with no error to
    flag it.

    """
    from regente.core.states import ACTIVE
    from regente.engine.supervisor import resume_state

    for state in ACTIVE:
        assert resume_state(state) is TaskState.READY, state


def test_area_of_work_is_of_task_is_survives_the_resume(bench):
    """The previous attempt's WIP has to be there when the worker comes back."""
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench(script={"A-1": {"ok": False, "resumo": "caiu"}})
    orq.tick()
    orq.tick()

    task = store.tasks("wks_teste")[0]
    first_pass = Path(store.task_runs(task.id)[0].workspace_path)
    (first_pass / "wip.txt").write_text("commit pela metade", encoding="utf-8")

    orq.tick()   # segunda tentativa
    runs = store.task_runs(task.id)
    assert len(runs) == 2
    assert runs[1].workspace_path == str(first_pass), "a retomada abriu area nova"
    assert (Path(runs[1].workspace_path) / "wip.txt").is_file(), "o WIP foi jogado fora"


# ---- relevance: what the SOURCE says about the work ----------------------
# The two tests below lock down defects found by running against a real board.
# No invented data would have revealed them: they only appear when the provider
# describes work that already has people on it.

def test_not_dispatches_work_that_already_tem_someone(bench):
    """An agent on top of a person is the worst possible outcome.

    Measured against the real board: two issues in CODING were dispatched on the
    first tick before this guard existed.

    """
    write_task(bench.tasks, "A-1", status="TO DO", resources=["repo:a"])
    write_task(bench.tasks, "A-2", status="CODING", resources=["repo:b"])
    write_task(bench.tasks, "A-3", status="REVIEWING", resources=["repo:c"])
    orq, store = bench()
    orq.tick()
    rel = orq.tick()

    assert rel.dispatched == ("A-1",)
    by_key = {t.key: t for t in store.tasks("wks_teste")}
    assert by_key["A-2"].state is TaskState.BLOCKED
    assert by_key["A-3"].state is TaskState.BLOCKED
    assert by_key["A-2"].data["bloqueada_por"] == "origem"


def test_status_unknown_not_is_dispatched(bench):
    """Not knowing whether somebody is on the task costs a deferral, never a collision."""
    write_task(bench.tasks, "A-9", status="AGUARDANDO JURIDICO", resources=["repo:x"])
    orq, store = bench()
    orq.tick()
    rel = orq.tick()
    assert not rel.dispatched
    assert store.tasks("wks_teste")[0].state is TaskState.BLOCKED
    assert rel.anomalies and "unmapped status" in rel.anomalies[0]


def test_change_in_source_releases_the_work(bench):
    """The person returned the task to the board; the engine has to notice on its own."""
    write_task(bench.tasks, "A-1", status="REVIEWING", resources=["repo:a"])
    orq, store = bench()
    orq.tick(); orq.tick()
    assert store.tasks("wks_teste")[0].state is TaskState.BLOCKED

    write_task(bench.tasks, "A-1", status="TO DO", resources=["repo:a"])
    rel = orq.tick()
    assert rel.changes and rel.changes[0][0] == "A-1"
    assert rel.unblocked_tasks == ("A-1",)
    assert store.tasks("wks_teste")[0].state is TaskState.TESTING


def test_block_by_failure_not_is_undone_by_status_external(bench):
    """Only what was blocked BY THE SOURCE comes back through a change at the source."""
    write_task(bench.tasks, "A-1", status="TO DO", resources=["repo:a"])
    orq, store = bench()
    orq.tick(); orq.tick()
    task = store.tasks("wks_teste")[0]
    store.transition(task.id, TaskState.BLOCKED, actor="humano", reason="parei na mao")

    orq.tick()
    assert store.task(task.id).state is TaskState.BLOCKED, (
        "bloqueio humano nao pode ser desfeito por status de board")


def test_work_finished_in_source_not_enters(bench):
    write_task(bench.tasks, "A-1", status="TO DO", resources=["repo:a"])
    write_task(bench.tasks, "A-2", status="DONE")
    orq, store = bench()
    rel = orq.tick()
    assert rel.discovered == 1
    assert {t.key for t in store.tasks("wks_teste")} == {"A-1"}


def test_hierarchy_not_becomes_dependency(bench):
    """95 of 100 issues on the real board had a parent. If hierarchy blocked, the
    engine would dispatch nothing.

    """
    write_task(bench.tasks, "MAE-1", status="TO DO", resources=["repo:m"])
    write_task(bench.tasks, "F-1", status="TO DO", resources=["repo:a"],
                 related=["MAE-1"])
    write_task(bench.tasks, "F-2", status="TO DO", resources=["repo:b"],
                 related=["MAE-1"])
    orq, store = bench(limits=Limits(max_workers=3))
    orq.tick()
    rel = orq.tick()
    assert set(rel.dispatched) == {"MAE-1", "F-1", "F-2"}
    assert not store.dependencies("wks_teste"), "relacionamento virou aresta"


def test_database_old_migrates_in_instead_of_refusing(tmp_path):
    """A schema bump without a migration turns the state promise into a trap."""
    import sqlite3
    from regente.engine.store_sqlite import SqliteStore

    path = tmp_path / "velho.db"
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE meta (chave TEXT PRIMARY KEY, valor TEXT NOT NULL);
        INSERT INTO meta VALUES('esquema','1');
        CREATE TABLE leases (
          recurso TEXT PRIMARY KEY, dono TEXT NOT NULL, workspace_id TEXT NOT NULL,
          expira_em TEXT NOT NULL, renovado_em TEXT NOT NULL);
        INSERT INTO leases VALUES('repo:x','run_1','wks_a','2099-01-01T00:00:00.000000Z',
                                  '2026-01-01T00:00:00.000000Z');
    """)
    con.commit(); con.close()

    s = SqliteStore(path)
    s.migrate()
    s.verify()
    # And the NEW behaviour holds after the upgrade.
    assert s.acquire_lease("repo:x", "run_a", "wks_a", 60) is not None
    assert s.acquire_lease("repo:x", "run_b", "wks_b", 60) is not None
    s.close()


def test_database_of_version_future_is_refused(tmp_path):
    """Silently downgrading a version would corrupt the state."""
    import sqlite3
    import pytest as _pytest
    from regente.core.errors import CorruptedState
    from regente.engine.store_sqlite import SqliteStore

    path = tmp_path / "futuro.db"
    con = sqlite3.connect(str(path))
    con.executescript("CREATE TABLE meta (chave TEXT PRIMARY KEY, valor TEXT NOT NULL);"
                      "INSERT INTO meta VALUES('esquema','99');")
    con.commit(); con.close()
    with _pytest.raises(CorruptedState, match="newer engine|no path to"):
        SqliteStore(path).migrate()
