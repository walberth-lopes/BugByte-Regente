# -*- coding: utf-8 -*-
"""O motor de ponta a ponta, contra os criterios de success do produto.

Cada teste aqui corresponde a uma promessa: descobrir trabalho, make o grafo,
achar paralelismo, isolar workers, persistir estado, detectar falha, pedir
intervencao e **retomar execucao interrompida**.
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
    """Monta um motor completo em disco temporario."""
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
            # The adapter's own default is NO_PROGRESS -- an agent never
            # declares its own success. These tests exercise orchestration, so
            # the bench declares a finished process explicitly rather than
            # relying on an optimistic default that no longer exists.
            runner=ScriptedAgent(
                script=script or {},
                default_value={"status": "FINISHED", "claim": "COMPLETE",
                               "summary": "declarado pelo bench"}),
            gate=Gate(store=store, policy=PolicyEngine.from_config([]), risk=risk),
            risk=risk, limits=limits or Limits(max_workers=3),
            notificador=Console(journal=tmp_path / "jornal.log"),
            lease_seconds=lease_seconds)
        return orq, store

    make.tasks = tasks_dir
    make.root = tmp_path
    return make


# ---- descoberta e baseline ----------------------------------------------

def test_first_pass_records_is_not_dispatches(bench):
    """Ligar o motor num backlog cheio nao pode virar tempestade de workers."""
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
    # This used to assert TESTING. TESTING is where this engine's pipeline
    # currently ends -- nothing advances a task out of it -- so parking
    # there looked like progress and was a dead end: the scheduler skips an
    # active task, the work was never touched again, and every later tick
    # reported clean. A soak run found it on its fourth tick. The engine now
    # hands the task to a person instead of leaving it to look busy.
    assert store.tasks("wks_teste")[0].state is TaskState.WAITING_HUMAN


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
    """O error precisa aparecer. Board vazio e board quebrado sao coisas diferentes."""
    write_task(bench.tasks, "A-1")
    orq, store = bench()
    orq.tick()
    (bench.tasks / "quebrado.yaml").write_text("isto: [nao\n fecha", encoding="utf-8")
    rel = orq.tick()
    assert rel.errors
    assert "adapter" in rel.errors[0]


# ---- grafo e paralelismo -------------------------------------------------

def test_dependency_declared_becomes_graph(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    write_task(bench.tasks, "A-2", resources=["repo:b"])
    write_task(bench.tasks, "A-3", resources=["repo:c"],
                 depends_on=[{"key": "A-1"}, {"key": "A-2"}])
    orq, store = bench()
    orq.tick()

    plan = orq.plan()  # ainda em DISCOVERED: nada pronto
    assert not plan.dispatch

    orq._analyze(type("R", (), {"analyzed": 0})())
    plan = orq.plan()
    chaves = {store.task(i).key for i in plan.dispatch}
    assert chaves == {"A-1", "A-2"}, "A-3 depende das outras duas"


def test_resource_shared_serializes(bench):
    """Duas tasks na mesma migration nao podem sair no mesmo tick."""
    write_task(bench.tasks, "A-1", resources=["migration:api"], priority=1)
    write_task(bench.tasks, "A-2", resources=["migration:api"], priority=2)
    orq, store = bench(limits=Limits(max_workers=4))
    orq.tick()
    orq._analyze(type("R", (), {"analyzed": 0})())

    plan = orq.plan()
    assert len(plan.dispatch) == 1
    assert store.task(plan.dispatch[0]).key == "A-1"
    assert any("recurso ocupado" in a.reason for a in plan.deferred)


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


# ---- persistencia e retomada --------------------------------------------

def test_state_survives_to_process(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench()
    orq.tick()
    orq.tick()
    store.close()

    orq2, store2 = bench()          # processo novo, mesmo banco
    t = store2.tasks("wks_teste")[0]
    assert t.key == "A-1"
    # This used to assert TESTING. TESTING is where this engine's pipeline
    # currently ends -- nothing advances a task out of it -- so parking
    # there looked like progress and was a dead end: the scheduler skips an
    # active task, the work was never touched again, and every later tick
    # reported clean. A soak run found it on its fourth tick. The engine now
    # hands the task to a person instead of leaving it to look busy.
    assert t.state is TaskState.WAITING_HUMAN


def test_worker_dead_returns_the_task_to_the_queue(bench):
    """O criterio de morte e o lease vencido, nao a ausencia de processo."""
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench(script={"A-1": {"status": "FINISHED", "claim": "COMPLETE", "summary": "feito"}})
    orq.tick()

    # Simula um worker que travou: run vivo, lease ja vencido.
    orq._analyze(type("R", (), {"analyzed": 0})())
    task = store.tasks("wks_teste")[0]
    store.transition(task.id, TaskState.ASSIGNED, actor="teste")
    from regente.core.ids import RUN, new_id
    from regente.core.model import Run
    run = Run(id=new_id(RUN), task_id=task.id, workspace_id="wks_teste", agent="coder")
    store.save_run(run)
    store.acquire_lease("repo:a", run.id, "wks_teste", segundos=1)
    store._con.execute("UPDATE leases SET expires_at=? WHERE resource=?",
                       ("2000-01-01T00:00:00.000000Z", "repo:a"))

    rel = orq.tick()
    assert rel.recovered == ("A-1",)
    assert store.run(run.id).state is RunState.INTERRUPTED
    # Recuperar e devolver a fila, e a fila anda no mesmo tick: o trabalho volta
    # a andar sozinho, sem esperar o proximo ciclo nem intervencao.
    assert rel.dispatched == ("A-1",)
    # This used to assert TESTING. TESTING is where this engine's pipeline
    # currently ends -- nothing advances a task out of it -- so parking
    # there looked like progress and was a dead end: the scheduler skips an
    # active task, the work was never touched again, and every later tick
    # reported clean. A soak run found it on its fourth tick. The engine now
    # hands the task to a person instead of leaving it to look busy.
    assert store.task(task.id).state is TaskState.WAITING_HUMAN
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


# ---- falha, escada e escalonamento ---------------------------------------

def test_first_failure_retries_without_bothering_the_owner(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench(script={"A-1": {"status": "ERROR", "summary": "teste vermelho"}})
    orq.tick()
    rel = orq.tick()

    task = store.tasks("wks_teste")[0]
    assert task.state is TaskState.READY
    assert task.attempts == 1
    assert not rel.escalated, "falha unica e trabalho, nao pergunta"


def test_ladder_ends_is_escalates(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench(script={"A-1": {"status": "ERROR", "summary": "mesmo error"}})
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
        "status": "NEEDS_HUMAN", "claim": "NEEDS_HUMAN",
        "summary": "contrato da API publica e ambiguo",
        "escalation_reason": "escolher errado quebra cliente em producao",
        "questions": ["li os dois consumidores", "procurei ADR"]}})
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
        "status": "NEEDS_HUMAN", "summary": "ambiguo",
        "pergunta": {"por_que_importa": "afeta contrato publico"}}})
    orq.tick()
    orq.tick()

    a = store.open_approvals("wks_teste")[0]
    decidido = store.decide_approval(a.id, "seguir", per="walberth", note="manter compat")
    assert decidido.choice == "seguir"
    assert not store.open_approvals("wks_teste")

    task = store.task(a.task_id)
    store.transition(task.id, TaskState.IMPLEMENTING, actor="walberth", reason="decidido")
    assert store.task(task.id).state is TaskState.IMPLEMENTING


def test_choice_outside_of_options_is_refused(bench):
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench(script={"A-1": {
        "status": "NEEDS_HUMAN", "summary": "x",
        "pergunta": {"por_que_importa": "y"}}})
    orq.tick()
    orq.tick()
    a = store.open_approvals("wks_teste")[0]
    with pytest.raises(Exception):
        store.decide_approval(a.id, "opcao_inventada", per="walberth")


def test_worker_that_blowing_up_not_bringing_down_the_tick(bench):
    class Explode(ScriptedAgent):
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
    tipos = [e.kind for e in store.events("wks_teste", task_id=task.id, limit=50)]
    assert "descoberta" in tipos
    assert "despachada" in tipos
    assert tipos.count("transicao") >= 4


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
    """O buraco que quase passou: recuperar mudando o rotulo, sem devolver a fila.

    Uma task que volta do crash para um estado ATIVO nao e despachada por
    ninguem -- fica viva no papel e parada de verdade, sem error que acuse.
    """
    from regente.core.states import ACTIVE
    from regente.engine.supervisor import resume_state

    for state in ACTIVE:
        assert resume_state(state) is TaskState.READY, state


def test_area_of_work_is_of_task_is_survives_the_resume(bench):
    """O WIP da tentativa anterior precisa estar la quando o worker volta."""
    write_task(bench.tasks, "A-1", resources=["repo:a"])
    orq, store = bench(script={"A-1": {"status": "ERROR", "summary": "caiu"}})
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


# ---- relevancia: o que a ORIGEM diz sobre o trabalho ---------------------
# Os dois testes abaixo travam defeitos encontrados rodando contra um board
# real. Nenhum dado inventado os teria revelado: eles so aparecem quando o
# provedor descreve trabalho que ja tem gente nele.

def test_not_dispatches_work_that_already_tem_someone(bench):
    """Um agente por cima de uma pessoa e o pior desfecho possivel.

    Medido contra o board real: duas issues em CODING foram despachadas no
    primeiro tick antes desta guarda existir.
    """
    write_task(bench.tasks, "A-1", status="TO DO", resources=["repo:a"])
    write_task(bench.tasks, "A-2", status="CODING", resources=["repo:b"])
    write_task(bench.tasks, "A-3", status="REVIEWING", resources=["repo:c"])
    orq, store = bench()
    orq.tick()
    rel = orq.tick()

    assert rel.dispatched == ("A-1",)
    porchave = {t.key: t for t in store.tasks("wks_teste")}
    assert porchave["A-2"].state is TaskState.BLOCKED
    assert porchave["A-3"].state is TaskState.BLOCKED
    assert porchave["A-2"].data["bloqueada_por"] == "origem"


def test_status_unknown_not_is_dispatched(bench):
    """Nao saber se alguem esta na task custa um adiamento, nunca um atropelo."""
    write_task(bench.tasks, "A-9", status="AGUARDANDO JURIDICO", resources=["repo:x"])
    orq, store = bench()
    orq.tick()
    rel = orq.tick()
    assert not rel.dispatched
    assert store.tasks("wks_teste")[0].state is TaskState.BLOCKED
    assert rel.anomalies and "nao mapeado" in rel.anomalies[0]


def test_change_in_source_releases_the_work(bench):
    """A pessoa devolveu a task ao board; o motor precisa notar sozinho."""
    write_task(bench.tasks, "A-1", status="REVIEWING", resources=["repo:a"])
    orq, store = bench()
    orq.tick(); orq.tick()
    assert store.tasks("wks_teste")[0].state is TaskState.BLOCKED

    write_task(bench.tasks, "A-1", status="TO DO", resources=["repo:a"])
    rel = orq.tick()
    assert rel.changes and rel.changes[0][0] == "A-1"
    assert rel.unblocked_tasks == ("A-1",)
    # This used to assert TESTING. TESTING is where this engine's pipeline
    # currently ends -- nothing advances a task out of it -- so parking
    # there looked like progress and was a dead end: the scheduler skips an
    # active task, the work was never touched again, and every later tick
    # reported clean. A soak run found it on its fourth tick. The engine now
    # hands the task to a person instead of leaving it to look busy.
    assert store.tasks("wks_teste")[0].state is TaskState.WAITING_HUMAN


def test_block_by_failure_not_is_undone_by_status_external(bench):
    """So quem foi bloqueado PELA ORIGEM volta por mudanca da origem."""
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
    """95 de 100 issues do board real tinham mae. Se hierarquia bloqueasse,
    o motor nao despacharia nada."""
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
    """Bump de esquema sem migracao transforma a promessa de estado em pegadinha."""
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
    # E o comportamento NOVO vale depois da subida.
    assert s.acquire_lease("repo:x", "run_a", "wks_a", 60) is not None
    assert s.acquire_lease("repo:x", "run_b", "wks_b", 60) is not None
    s.close()


def test_database_of_version_future_is_refused(tmp_path):
    """Descer de versao em silencio corromperia o estado."""
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
    assert s2._con.execute(
        "SELECT COUNT(*) FROM counters WHERE name='despachos'").fetchone()[0] == 0
    assert s2._con.execute(
        "SELECT value FROM meta WHERE key='schema'").fetchone()[0] == SCHEMA_VERSION
    s2.close()
