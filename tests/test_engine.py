# -*- coding: utf-8 -*-
"""The engine end to end, against the product's success criteria.

Each test here matches a promise: discover work, build the graph, find
parallelism, isolate workers, persist state, detect failure, ask for
intervention and **resume interrupted execution**.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from regente.adapters.notify.console import Console
from regente.adapters.runner.scripted import ScriptedAgent
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
        ws = Workspace(id="wks_test", client_id="cli_test", name="test",
                       max_autonomy=AutonomyLevel.L3)
        store.save_workspace(ws)
        risk = RiskEngine()
        orch = Orchestrator(
            store=store, workspace=ws,
            tasks_provider=FilesystemTasks(tasks_dir),
            area_provider=IsolatedDirectory(tmp_path / "areas"),
            # The adapter's own default is NO_PROGRESS -- an agent never
            # declares its own success. These tests exercise orchestration, so
            # the bench declares a finished process explicitly rather than
            # relying on an optimistic default that no longer exists.
            runner=ScriptedAgent(
                script=script or {},
                default_value={"status": "FINISHED", "claim": "COMPLETE",
                               "summary": "declared by the bench"}),
            gate=Gate(store=store, policy=PolicyEngine.from_config([]), risk=risk),
            risk=risk, limits=limits or Limits(max_workers=3),
            notifier=Console(journal=tmp_path / "journal.log"),
            lease_seconds=lease_seconds)
        return orch, store

    make.tasks = tasks_dir
    make.root = tmp_path
    return make


# ---- discovery and baseline ---------------------------------------------

def test_first_pass_records_is_not_dispatches(bench):
    """Switching the engine on against a full backlog must not become a worker storm."""
    for k in ("A-1", "A-2", "A-3"):
        write_task(bench.tasks, k)
    orch, store = bench()
    rel = orch.tick()

    assert rel.baseline
    assert rel.discovered == 3
    assert not rel.dispatched
    assert all(t.state is TaskState.DISCOVERED for t in store.tasks("wks_test"))


def test_the_second_tick_analyzes_and_dispatches(bench):
    write_task(bench.tasks, "A-1", resources=["repo:x"])
    orch, store = bench()
    orch.tick()                      # baseline
    rel = orch.tick()

    assert not rel.baseline
    assert rel.dispatched == ("A-1",)
    assert store.tasks("wks_test")[0].state is TaskState.TESTING


def test_task_new_not_reopen_baseline(bench):
    write_task(bench.tasks, "A-1")
    orch, store = bench()
    orch.tick()
    write_task(bench.tasks, "A-2")
    rel = orch.tick()
    assert not rel.baseline
    assert rel.discovered == 1


def test_task_not_duplicate_between_ticks(bench):
    write_task(bench.tasks, "A-1")
    orch, store = bench()
    for _ in range(3):
        orch.tick()
    assert len([t for t in store.tasks("wks_test") if t.key == "A-1"]) == 1


def test_failure_of_adapter_not_becomes_absence_of_work(bench):
    """The error has to show. An empty board and a broken board are different things."""
    write_task(bench.tasks, "A-1")
    orch, store = bench()
    orch.tick()
    (bench.tasks / "broken.yaml").write_text("this: [not\n closed", encoding="utf-8")
    rel = orch.tick()
    assert rel.errors
    assert "adapter" in rel.errors[0]


# ---- graph and parallelism -----------------------------------------------

def test_dependency_declared_becomes_graph(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    write_task(bench.tasks, "A-2", resources=["repo:b"])
    write_task(bench.tasks, "A-3", resources=["repo:c"],
                 depends_on=[{"key": "A-1"}, {"key": "A-2"}])
    orch, store = bench()
    orch.tick()

    plan = orch.plan()  # still in DISCOVERED: nothing ready
    assert not plan.dispatch

    orch._analyze(type("R", (), {"analyzed": 0})())
    plan = orch.plan()
    keys = {store.task(i).key for i in plan.dispatch}
    assert keys == {"A-1", "A-2"}, "A-3 depends on the other two"


def test_resource_shared_serializes(bench):
    """Two tasks on the same migration must not go out in the same tick."""
    write_task(bench.tasks, "A-1", resources=["migration:api"], priority=1)
    write_task(bench.tasks, "A-2", resources=["migration:api"], priority=2)
    orch, store = bench(limits=Limits(max_workers=4))
    orch.tick()
    orch._analyze(type("R", (), {"analyzed": 0})())

    plan = orch.plan()
    assert len(plan.dispatch) == 1
    assert store.task(plan.dispatch[0]).key == "A-1"
    assert any("resource busy" in a.reason for a in plan.deferred)


def test_cycle_escalates_in_instead_of_blocking(bench):
    write_task(bench.tasks, "A-1", depends_on=[{"key": "A-2"}])
    write_task(bench.tasks, "A-2", depends_on=[{"key": "A-1"}])
    orch, store = bench()
    orch.tick()
    rel = orch.tick()

    assert set(rel.cycles) == {"A-1", "A-2"}
    assert not rel.dispatched
    open_items = store.open_approvals("wks_test")
    assert len(open_items) == 1
    assert "circular" in open_items[0].what_happened


def test_cycle_not_reasking_the_each_tick(bench):
    write_task(bench.tasks, "A-1", depends_on=[{"key": "A-2"}])
    write_task(bench.tasks, "A-2", depends_on=[{"key": "A-1"}])
    orch, store = bench()
    for _ in range(4):
        orch.tick()
    assert len(store.open_approvals("wks_test")) == 1


# ---- isolamento ----------------------------------------------------------

def test_each_worker_gets_its_own_area(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    write_task(bench.tasks, "A-2", resources=["repo:b"])
    orch, store = bench()
    orch.tick()
    orch.tick()

    areas = {r.workspace_path for r in
             [x for t in store.tasks("wks_test") for x in store.task_runs(t.id)]}
    assert len(areas) == 2
    for a in areas:
        assert (Path(a) / "run.json").is_file()


# ---- persistence and resumption -----------------------------------------

def test_state_survives_to_process(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orch, store = bench()
    orch.tick()
    orch.tick()
    store.close()

    orq2, store2 = bench()          # new process, same database
    t = store2.tasks("wks_test")[0]
    assert t.key == "A-1"
    assert t.state is TaskState.TESTING


def test_worker_dead_returns_the_task_to_the_queue(bench):
    """The criterion for death is the expired lease, not the absence of a process."""
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orch, store = bench(script={"A-1": {"status": "FINISHED", "claim": "COMPLETE", "summary": "feito"}})
    orch.tick()

    # Simulates a worker that hung: a live run, a lease already expired.
    orch._analyze(type("R", (), {"analyzed": 0})())
    task = store.tasks("wks_test")[0]
    store.transition(task.id, TaskState.ASSIGNED, actor="test")
    from regente.core.ids import RUN, new_id
    from regente.core.model import Run
    run = Run(id=new_id(RUN), task_id=task.id, workspace_id="wks_test", agent="coder")
    store.save_run(run)
    store.acquire_lease("repo:a", run.id, "wks_test", seconds=1)
    store._conn.execute("UPDATE leases SET expires_at=? WHERE resource=?",
                       ("2000-01-01T00:00:00.000000Z", "repo:a"))

    rel = orch.tick()
    assert rel.recovered == ("A-1",)
    assert store.run(run.id).state is RunState.INTERRUPTED
    # Recovering and returning to the queue, and the queue moving, happen in the
    # same tick: the work starts moving again on its own, without waiting for the
    # next cycle or for intervention.
    assert rel.dispatched == ("A-1",)
    assert store.task(task.id).state is TaskState.TESTING
    assert store.acquire_lease("repo:a", "another", "wks_test", 60) is not None, \
        "the dead worker's lease has to have been released"


def test_lease_prevents_two_owners(bench):
    orch, store = bench()
    assert store.acquire_lease("repo:a", "run_1", "wks_test", 60) is not None
    assert store.acquire_lease("repo:a", "run_2", "wks_test", 60) is None
    store.release_lease("repo:a", "run_1")
    assert store.acquire_lease("repo:a", "run_2", "wks_test", 60) is not None


def test_an_expired_lease_can_be_taken(bench):
    orch, store = bench()
    store.acquire_lease("repo:a", "run_1", "wks_test", 60)
    store._conn.execute("UPDATE leases SET expires_at=? WHERE resource=?",
                       ("2000-01-01T00:00:00.000000Z", "repo:a"))
    assert store.acquire_lease("repo:a", "run_2", "wks_test", 60) is not None


# ---- failure, ladder and escalation --------------------------------------

def test_first_failure_retries_without_bothering_the_owner(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orch, store = bench(script={"A-1": {"status": "ERROR", "summary": "teste vermelho"}})
    orch.tick()
    rel = orch.tick()

    task = store.tasks("wks_test")[0]
    assert task.state is TaskState.READY
    assert task.attempts == 1
    assert not rel.escalated, "a single failure is work, not a question"


def test_the_ladder_ends_and_escalates(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orch, store = bench(script={"A-1": {"status": "ERROR", "summary": "same error"}})
    for _ in range(5):
        orch.tick()

    task = store.tasks("wks_test")[0]
    assert task.state is TaskState.WAITING_HUMAN
    open_items = store.open_approvals("wks_test")
    assert len(open_items) == 1
    assert open_items[0].why_it_matters
    assert open_items[0].what_was_tried


def test_a_worker_can_ask_for_a_human_decision(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orch, store = bench(script={"A-1": {
        "status": "NEEDS_HUMAN", "claim": "NEEDS_HUMAN",
        "summary": "the public API contract is ambiguous",
        "escalation_reason": "choosing wrong breaks a client in production",
        "questions": ["li os dois consumidores", "procurei ADR"]}})
    orch.tick()
    rel = orch.tick()

    assert rel.escalated == ("A-1",)
    task = store.tasks("wks_test")[0]
    assert task.state is TaskState.WAITING_HUMAN
    assert task.paused_at is TaskState.IMPLEMENTING

    a = store.open_approvals("wks_test")[0]
    assert a.recommendation == "follow"
    assert len(a.options) >= 3


def test_decision_human_is_recorded_is_resumes(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orch, store = bench(script={"A-1": {
        "status": "NEEDS_HUMAN", "summary": "ambiguo",
        "question": {"why_it_matters": "affects a public contract"}}})
    orch.tick()
    orch.tick()

    a = store.open_approvals("wks_test")[0]
    decided = store.decide_approval(a.id, "follow", by="walberth", note="keep compatibility")
    assert decided.choice == "follow"
    assert not store.open_approvals("wks_test")

    task = store.task(a.task_id)
    store.transition(task.id, TaskState.IMPLEMENTING, actor="walberth", reason="decidido")
    assert store.task(task.id).state is TaskState.IMPLEMENTING


def test_choice_outside_of_options_is_refused(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orch, store = bench(script={"A-1": {
        "status": "NEEDS_HUMAN", "summary": "x",
        "question": {"why_it_matters": "y"}}})
    orch.tick()
    orch.tick()
    a = store.open_approvals("wks_test")[0]
    with pytest.raises(Exception):
        store.decide_approval(a.id, "opcao_inventada", by="walberth")


def test_worker_that_blowing_up_not_bringing_down_the_tick(bench):
    class Explode(ScriptedAgent):
        def run(self, request):
            raise RuntimeError("estourou")

    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orch, store = bench()
    orch.runner = Explode()
    orch.tick()
    rel = orch.tick()

    assert store.tasks("wks_test")[0].state is TaskState.READY
    assert not rel.errors, "a worker exception becomes a task failure, not a tick error"


# ---- trilha --------------------------------------------------------------

def test_timeline_tells_the_story(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orch, store = bench()
    orch.tick()
    orch.tick()

    task = store.tasks("wks_test")[0]
    kinds = [e.kind for e in store.events("wks_test", task_id=task.id, limit=50)]
    assert "discovered" in kinds
    assert "dispatched" in kinds
    assert kinds.count("transition") >= 4


def test_every_transition_leaves_trail(bench):
    write_task(bench.tasks, "A-1")
    orch, store = bench()
    orch.tick()
    task = store.tasks("wks_test")[0]
    store.transition(task.id, TaskState.ANALYZING, actor="test", reason="because I said so")
    event = store.events("wks_test", task_id=task.id, limit=1)[0]
    assert event.kind == "transition"
    assert event.data["reason"] == "because I said so"
    assert event.actor == "test"


def test_a_recovered_task_becomes_schedulable_again(bench):
    """The hole that nearly got through: recovering by changing the label
    without returning the task to the queue.

    A task that comes back from a crash into an ACTIVE state is dispatched by
    nobody -- alive on paper and genuinely stopped, with no error to show it.
    """
    from regente.core.states import ACTIVE
    from regente.engine.supervisor import resume_state

    for state in ACTIVE:
        assert resume_state(state) is TaskState.READY, state


def test_the_work_area_belongs_to_the_task_and_survives_the_resume(bench):
    """The previous attempt's WIP has to be there when the worker comes back."""
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orch, store = bench(script={"A-1": {"status": "ERROR", "summary": "crashed"}})
    orch.tick()
    orch.tick()

    task = store.tasks("wks_test")[0]
    first_pass = Path(store.task_runs(task.id)[0].workspace_path)
    (first_pass / "wip.txt").write_text("half-finished commit", encoding="utf-8")

    orch.tick()   # second attempt
    runs = store.task_runs(task.id)
    assert len(runs) == 2
    assert runs[1].workspace_path == str(first_pass), "the resume opened a new area"
    assert (Path(runs[1].workspace_path) / "wip.txt").is_file(), "the WIP was thrown away"


# ---- relevance: what the SOURCE says about the work ----------------------
# The two tests below lock down defects found by running against a real board.
# No invented data would have revealed them: they only appear when the provider
# describes work that already has people on it.

def test_does_not_dispatch_work_that_already_has_someone_on_it(bench):
    """An agent on top of a person is the worst possible outcome.

    Measured against the real board: two issues in CODING were dispatched on the
    first tick before this guard existed.
    """
    write_task(bench.tasks, "A-1", status="TO DO", resources=["repo:a"])
    write_task(bench.tasks, "A-2", status="CODING", resources=["repo:b"])
    write_task(bench.tasks, "A-3", status="REVIEWING", resources=["repo:c"])
    orch, store = bench()
    orch.tick()
    rel = orch.tick()

    assert rel.dispatched == ("A-1",)
    by_key = {t.key: t for t in store.tasks("wks_test")}
    assert by_key["A-2"].state is TaskState.BLOCKED
    assert by_key["A-3"].state is TaskState.BLOCKED
    assert by_key["A-2"].data["blocked_by"] == "source"


def test_status_unknown_not_is_dispatched(bench):
    """Not knowing whether somebody is on the task costs a deferral, never a collision."""
    write_task(bench.tasks, "A-9", status="AGUARDANDO JURIDICO", resources=["repo:x"])
    orch, store = bench()
    orch.tick()
    rel = orch.tick()
    assert not rel.dispatched
    assert store.tasks("wks_test")[0].state is TaskState.BLOCKED
    assert rel.anomalies and "unmapped status" in rel.anomalies[0]


def test_change_in_source_releases_the_work(bench):
    """The person returned the task to the board; the engine has to notice on its own."""
    write_task(bench.tasks, "A-1", status="REVIEWING", resources=["repo:a"])
    orch, store = bench()
    orch.tick(); orch.tick()
    assert store.tasks("wks_test")[0].state is TaskState.BLOCKED

    write_task(bench.tasks, "A-1", status="TO DO", resources=["repo:a"])
    rel = orch.tick()
    assert rel.changes and rel.changes[0][0] == "A-1"
    assert rel.unblocked_tasks == ("A-1",)
    assert store.tasks("wks_test")[0].state is TaskState.TESTING


def test_block_by_failure_not_is_undone_by_status_external(bench):
    """Only what was blocked BY THE SOURCE comes back through a change at the source."""
    write_task(bench.tasks, "A-1", status="TO DO", resources=["repo:a"])
    orch, store = bench()
    orch.tick(); orch.tick()
    task = store.tasks("wks_test")[0]
    store.transition(task.id, TaskState.BLOCKED, actor="humano", reason="stopped by hand")

    orch.tick()
    assert store.task(task.id).state is TaskState.BLOCKED, (
        "a human block must not be undone by a board status")


def test_work_finished_in_source_not_enters(bench):
    write_task(bench.tasks, "A-1", status="TO DO", resources=["repo:a"])
    write_task(bench.tasks, "A-2", status="DONE")
    orch, store = bench()
    rel = orch.tick()
    assert rel.discovered == 1
    assert {t.key for t in store.tasks("wks_test")} == {"A-1"}


def test_hierarchy_not_becomes_dependency(bench):
    """95 of 100 issues on the real board had a parent. If hierarchy blocked,
    the engine would dispatch nothing."""
    write_task(bench.tasks, "PARENT-1", status="TO DO", resources=["repo:m"])
    write_task(bench.tasks, "F-1", status="TO DO", resources=["repo:a"],
                 related=["PARENT-1"])
    write_task(bench.tasks, "F-2", status="TO DO", resources=["repo:b"],
                 related=["PARENT-1"])
    orch, store = bench(limits=Limits(max_workers=3))
    orch.tick()
    rel = orch.tick()
    assert set(rel.dispatched) == {"PARENT-1", "F-1", "F-2"}
    assert not store.dependencies("wks_test"), "relacionamento virou aresta"


def test_database_old_migrates_in_instead_of_refusing(tmp_path):
    """A schema bump without a migration turns the state promise into a trap."""
    import sqlite3
    from regente.engine.store_sqlite import SqliteStore

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    # pt-BR on purpose: this rebuilds a REAL v1 database by hand, and a v1
    # database really does have `chave`/`valor` columns and a row keyed
    # 'esquema'. Translating the fixture would test a database that never
    # existed, and the migration it is meant to prove would go unexercised.
    conn.executescript("""
        CREATE TABLE meta (chave TEXT PRIMARY KEY, valor TEXT NOT NULL);
        INSERT INTO meta VALUES('esquema','1');
        CREATE TABLE leases (
          recurso TEXT PRIMARY KEY, dono TEXT NOT NULL, workspace_id TEXT NOT NULL,
          expira_em TEXT NOT NULL, renovado_em TEXT NOT NULL);
        INSERT INTO leases VALUES('repo:x','run_1','wks_a','2099-01-01T00:00:00.000000Z',
                                  '2026-01-01T00:00:00.000000Z');
    """)
    conn.commit(); conn.close()

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

    path = tmp_path / "future.db"
    conn = sqlite3.connect(str(path))
    # pt-BR on purpose, for the same reason as the migration test above.
    conn.executescript("CREATE TABLE meta (chave TEXT PRIMARY KEY, valor TEXT NOT NULL);"
                      "INSERT INTO meta VALUES('esquema','99');")
    conn.commit(); conn.close()
    with _pytest.raises(CorruptedState, match="newer engine|no path to"):
        SqliteStore(path).migrate()


def test_the_daily_dispatch_counter_is_written_and_read_under_one_name(tmp_path):
    """The ceiling only bites if both sides agree on the counter's name.

    They did not: the rename to English changed the write and left the read, so
    `dispatch_count` always answered zero and `max_dispatches_per_day` never
    engaged. A budget that silently does not apply fails in the expensive
    direction, and nothing in the suite noticed -- hence this test.
    """
    from regente.engine.store_sqlite import SqliteStore

    s = SqliteStore(tmp_path / "counter.db")
    s.migrate()
    assert s.dispatch_count("wks_a", "2026-09-06") == 0
    s.mark_dispatch("wks_a", "2026-09-06")
    s.mark_dispatch("wks_a", "2026-09-06")
    assert s.dispatch_count("wks_a", "2026-09-06") == 2
    assert s.dispatch_count("wks_b", "2026-09-06") == 0, "counters are per workspace"
    assert s.dispatch_count("wks_a", "2026-09-07") == 0, "counters are per day"
    s.close()


def test_values_stored_in_portuguese_are_migrated(tmp_path):
    """`_v2_to_v3` translated the COLUMNS and left the rows saying `descoberta`.

    A migration nobody exercises is worth nothing, so this builds a v4 database
    by hand -- English columns, Portuguese values -- and checks every place a
    pt-BR value was persisted comes back translated by `_v6_to_v7`.

    Stamped v4 rather than v6 on purpose: the whole ladder then runs, so this
    also proves the value migration composes with the two schema migrations
    between it and the version it starts from.
    """
    import json
    import sqlite3
    from regente.engine.store_sqlite import SqliteStore

    path = tmp_path / "v4.db"
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES('schema','4');
        CREATE TABLE events (
          id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, ts TEXT NOT NULL,
          kind TEXT NOT NULL, task_id TEXT, run_id TEXT,
          actor TEXT NOT NULL DEFAULT 'engine', summary TEXT NOT NULL DEFAULT '',
          data TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, project_id TEXT NOT NULL,
          title TEXT NOT NULL, state TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          provider TEXT, external_key TEXT, url TEXT,
          priority INTEGER NOT NULL DEFAULT 100, risk TEXT, paused_at TEXT,
          resources TEXT NOT NULL DEFAULT '[]', attempts INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          data TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE approvals (
          id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, task_id TEXT NOT NULL,
          run_id TEXT, state TEXT NOT NULL, risk TEXT NOT NULL,
          what_happened TEXT NOT NULL DEFAULT '', why_it_matters TEXT NOT NULL DEFAULT '',
          attempts TEXT NOT NULL DEFAULT '[]', options TEXT NOT NULL DEFAULT '[]',
          recommendation TEXT, created_at TEXT NOT NULL, decided_at TEXT,
          decided_by TEXT, choice TEXT, note TEXT NOT NULL DEFAULT '');
    """)
    con.execute("INSERT INTO events VALUES('evt_1','wks_a','2026-01-01T00:00:00.000000Z',"
                "'descoberta',NULL,NULL,'engine','',  '{}')")
    con.execute("INSERT INTO events VALUES('evt_2','wks_a','2026-01-01T00:00:00.000000Z',"
                "'transicao',NULL,NULL,'engine','',  '{}')")
    con.execute("INSERT INTO events VALUES('evt_3','wks_a','2026-01-01T00:00:00.000000Z',"
                "'mudou_na_origem',NULL,NULL,'engine','',?)",
                (json.dumps({"de": "TO DO", "to_state": "CODING"}),))
    con.execute(
        "INSERT INTO tasks VALUES('tsk_1','wks_a','prj_1','t','BLOCKED','',NULL,NULL,"
        "NULL,100,NULL,NULL,'[]',0,'2026-01-01T00:00:00.000000Z','2026-01-01T00:00:00.000000Z',?)",
        (json.dumps({"situacao_externa": "EM_EXECUCAO", "estado_externo": "CODING",
                     "rotulos": ["a"], "bloqueada_por": "origem"}),))
    con.execute("INSERT INTO approvals VALUES('apv_1','wks_a','tsk_1',NULL,'DECIDED',"
                "'MEDIUM','','','[]','[]','seguir','2026-01-01T00:00:00.000000Z',NULL,NULL,"
                "'seguir','')")
    con.commit(); con.close()

    store = SqliteStore(path)
    store.migrate()
    store.verify()

    kinds = [e.kind for e in store.events("wks_a", limit=10)]
    assert "discovered" in kinds and "transition" in kinds
    assert "descoberta" not in kinds and "transicao" not in kinds

    task = store.task("tsk_1")
    assert task.data["normalised_status"] == "IN_PROGRESS"
    assert task.data["raw_status"] == "CODING"
    assert task.data["labels"] == ["a"]
    assert task.data["blocked_by"] == "source"
    assert "situacao_externa" not in task.data

    approval = store.approval("apv_1")
    assert approval.choice == "follow"
    assert approval.recommendation == "follow"

    # The event payload's own keys, not just its kind. `de`/`to_state` was a
    # half-finished pair; both halves read as a pair again afterwards.
    moved = [e for e in store.events("wks_a", limit=10) if e.kind == "changed_at_source"]
    assert moved and moved[0].data == {"from_state": "TO DO", "to_state": "CODING"}
    store.close()


def test_migrating_to_v6_keeps_a_day_already_spent(tmp_path):
    """Renaming the counter must not hand back budget that was already used."""
    from regente.engine.store_sqlite import SCHEMA_VERSION, SqliteStore

    path = tmp_path / "v5.db"
    s = SqliteStore(path)
    s.migrate()
    with s._tx() as c:
        c.execute("INSERT INTO counters(workspace_id, day, name, value) "
                  "VALUES('wks_a','2026-09-06','despachos',7)")
        c.execute("UPDATE meta SET value='5' WHERE key='schema'")
    s.close()

    s2 = SqliteStore(path)
    s2.migrate()
    assert s2.dispatch_count("wks_a", "2026-09-06") == 7
    s2.mark_dispatch("wks_a", "2026-09-06")
    assert s2.dispatch_count("wks_a", "2026-09-06") == 8
    assert s2._conn.execute(
        "SELECT COUNT(*) FROM counters WHERE name='despachos'").fetchone()[0] == 0
    assert s2._conn.execute(
        "SELECT value FROM meta WHERE key='schema'").fetchone()[0] == SCHEMA_VERSION
    s2.close()
