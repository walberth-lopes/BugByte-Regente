# -*- coding: utf-8 -*-
"""Two clients, one engine, one database, and deliberately identical names.

The weak form of this test gives each client different names and proves they
differ. That proves the names differ. Every test here gives both clients the
SAME local identifiers -- same task key, same repository, same resource, same
branch, same workspace name -- and proves they stay different entities.

If global identity is ever derived from a local name anywhere in the chain

    task -> target -> mission -> workspace -> lease -> run -> delivery

these two collide and the file goes red all at once.

What is being defended is not tidiness. A client that can read another's task,
spend another's budget, resolve another's secret or take another's lease is a
breach, and every one of those is a single forgotten `WHERE` away.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from soak import Clock, check_invariants
from tenants import (ORGANIZATION, SHARED_RESOURCE, SHARED_TASK,
                     SHARED_WORKSPACE_NAME, Tenant, two_tenants)

from regente.core.errors import CorruptedState, PolicyDenied
from regente.core.model import Run, RunState, now
from regente.core.policy import Action, AutonomyLevel, Effect, PolicyContext
from regente.core.states import TaskState
from regente.engine import health as health_module
from regente.engine.store_sqlite import SqliteStore

HERE = Path(__file__).resolve().parent


@pytest.fixture
def pair(tmp_path):
    a, b = two_tenants(tmp_path / "world")
    a.open()
    b.open()
    yield a, b
    a.close()
    b.close()


def work_both(a: Tenant, b: Tenant, ticks: int = 3) -> None:
    for _ in range(ticks):
        a.tick()
        b.tick()
        a.clock.advance(minutes=5)
        b.clock.advance(minutes=5)


# ---------------------------------------------------------------------------
# 0. The premise: the same local name is not the same identity
# ---------------------------------------------------------------------------

def test_the_same_workspace_name_under_two_clients_is_two_workspaces(pair):
    a, b = pair
    assert a.workspace.name == b.workspace.name == SHARED_WORKSPACE_NAME
    assert a.client_id != b.client_id
    assert a.workspace_id != b.workspace_id, (
        "identity is derived from organization + client + workspace; if it were "
        "derived from the workspace name these two would be one")


# ---------------------------------------------------------------------------
# 1. Task isolation
# ---------------------------------------------------------------------------

def test_1_the_same_task_key_in_two_clients_is_two_tasks(pair):
    a, b = pair
    a.write_task()
    b.write_task()
    work_both(a, b, ticks=2)

    a_tasks, b_tasks = a.tasks(), b.tasks()
    assert [t.key for t in a_tasks] == [SHARED_TASK]
    assert [t.key for t in b_tasks] == [SHARED_TASK]
    assert a_tasks[0].id != b_tasks[0].id
    assert a_tasks[0].workspace_id == a.workspace_id
    assert b_tasks[0].workspace_id == b.workspace_id


def test_1_a_client_never_sees_the_other_clients_tasks(pair):
    a, b = pair
    a.write_task("ONLY-IN-A")
    b.write_task("ONLY-IN-B")
    work_both(a, b, ticks=2)

    assert "ONLY-IN-B" not in {t.key for t in a.tasks()}
    assert "ONLY-IN-A" not in {t.key for t in b.tasks()}
    for task in a.tasks():
        assert task.workspace_id == a.workspace_id


def test_1_a_task_id_from_the_other_client_reads_as_absent(pair):
    """Ids are globally unique, so an id from outside would otherwise work."""
    a, b = pair
    a.write_task()
    b.write_task()
    work_both(a, b, ticks=2)

    theirs = b.tasks()[0]
    assert a.store.task(theirs.id, a.workspace_id) is None
    assert a.store.task_runs(theirs.id, a.workspace_id) == []
    # And the unscoped read still works, which is why the engine always scopes.
    assert a.store.task(theirs.id) is not None


def test_1_task_state_cannot_be_changed_across_the_boundary(pair):
    a, b = pair
    a.write_task()
    b.write_task()
    work_both(a, b, ticks=3)

    theirs = b.tasks()[0]
    before = theirs.state
    moved = a.orchestrator._moved_by_another(
        theirs.id, TaskState.CANCELLED, actor="client-a",
        reason="reaching across")
    assert not moved, "one client moved another client's task"
    assert b.store.task(theirs.id, b.workspace_id).state is before


# ---------------------------------------------------------------------------
# 2 & 3. Repository and lease isolation
# ---------------------------------------------------------------------------

def test_3_the_same_resource_name_is_two_leases(pair):
    """`A-X does not block B-X` -- the property that makes tenancy real."""
    a, b = pair
    at = now()
    run_a = Run(id="run_a", task_id="t", workspace_id=a.workspace_id,
                agent="coder", state=RunState.RUNNING, started_at=at)
    run_b = Run(id="run_b", task_id="t", workspace_id=b.workspace_id,
                agent="coder", state=RunState.RUNNING, started_at=at)

    assert a.store.claim(run_a, (SHARED_RESOURCE,), 300, when=at)
    assert b.store.claim(run_b, (SHARED_RESOURCE,), 300, when=at), (
        "client A's lease blocked client B on a resource of the same name")

    assert a.store.holds_lease(SHARED_RESOURCE, "run_a", a.workspace_id, when=at)
    assert b.store.holds_lease(SHARED_RESOURCE, "run_b", b.workspace_id, when=at)
    assert not a.store.holds_lease(SHARED_RESOURCE, "run_b", a.workspace_id,
                                   when=at)


def test_3_the_same_resource_within_one_client_still_contends(pair):
    """`A-X blocks A-X` -- isolation must not have disabled contention."""
    a, _ = pair
    at = now()
    first = Run(id="run_1", task_id="t1", workspace_id=a.workspace_id,
                agent="coder", state=RunState.RUNNING, started_at=at)
    second = Run(id="run_2", task_id="t2", workspace_id=a.workspace_id,
                 agent="coder", state=RunState.RUNNING, started_at=at)

    assert a.store.claim(first, (SHARED_RESOURCE,), 300, when=at)
    assert not a.store.claim(second, (SHARED_RESOURCE,), 300, when=at)


def test_3_a_client_cannot_release_or_renew_the_others_lease(pair):
    a, b = pair
    at = now()
    run_b = Run(id="run_b", task_id="t", workspace_id=b.workspace_id,
                agent="coder", state=RunState.RUNNING, started_at=at)
    assert b.store.claim(run_b, (SHARED_RESOURCE,), 300, when=at)

    # Same resource name, same owner id, wrong workspace.
    a.store.release_lease(SHARED_RESOURCE, "run_b", a.workspace_id)
    assert b.store.holds_lease(SHARED_RESOURCE, "run_b", b.workspace_id, when=at)

    assert a.store.renew_lease(SHARED_RESOURCE, "run_b", 900, a.workspace_id,
                               when=at) is False


def test_2_both_clients_work_the_same_repository_name_independently(pair):
    a, b = pair
    a.write_task(resources=(SHARED_RESOURCE,))
    b.write_task(resources=(SHARED_RESOURCE,))
    work_both(a, b, ticks=3)

    a_runs = {r.id for r in a.store.runs_in_state(a.workspace_id,
                                                  RunState.SUCCEEDED.value)}
    b_runs = {r.id for r in b.store.runs_in_state(b.workspace_id,
                                                  RunState.SUCCEEDED.value)}
    assert a_runs and b_runs, "both clients must have done work"
    assert not (a_runs & b_runs)


def test_2_delivery_records_are_scoped(pair):
    """The delivery chain keys on (workspace, provider, repo, pr_number)."""
    a, b = pair
    a_row = a.store.open_delivery(a.workspace_id, SHARED_TASK, "run_a",
                                  "github", "acme/worker", "feature/test", "a" * 40)
    b_row = b.store.open_delivery(b.workspace_id, SHARED_TASK, "run_b",
                                  "github", "acme/worker", "feature/test", "b" * 40)
    a.store.record_pull_request(a_row, 7, "url-a", "a" * 40)
    b.store.record_pull_request(b_row, 7, "url-b", "b" * 40)

    mine = a.store.delivery_for_pr(a.workspace_id, "github", "acme/worker", 7)
    theirs = b.store.delivery_for_pr(b.workspace_id, "github", "acme/worker", 7)
    assert mine["run_id"] == "run_a"
    assert theirs["run_id"] == "run_b"
    assert len(a.store.deliveries(a.workspace_id)) == 1


# ---------------------------------------------------------------------------
# 4. Policy isolation
# ---------------------------------------------------------------------------

def decide(tenant: Tenant, kind: str) -> str:
    return tenant.policy().decide(PolicyContext(
        action=Action(kind=kind, resource="acme/worker", environment="staging"),
        organization=ORGANIZATION, client=tenant.client,
        workspace=SHARED_WORKSPACE_NAME, project="p", agent="coder",
        risk="LOW", autonomy=AutonomyLevel.L3)).effect


def test_4_two_clients_have_genuinely_different_policies(pair):
    a, b = pair
    assert decide(a, "agent.run") == Effect.ALLOW
    assert decide(b, "agent.run") == Effect.ALLOW
    assert decide(a, "repo.push") == Effect.DENY, "client A must stay refused"
    assert decide(b, "repo.push") == Effect.ALLOW, "client B must stay allowed"


def test_4_changing_one_policy_does_not_change_the_other(pair):
    a, b = pair
    a.policy_rules = a.policy_rules + [
        {"name": "loosen", "effect": "ALLOW", "match": {"action": "repo.push"}}]

    # Most-restrictive-wins, so A stays denied even after adding an ALLOW.
    assert decide(a, "repo.push") == Effect.DENY
    assert decide(b, "repo.push") == Effect.ALLOW

    b.policy_rules = b.policy_rules + [
        {"name": "tighten", "effect": "DENY", "match": {"action": "repo.push"}}]
    assert decide(b, "repo.push") == Effect.DENY
    assert decide(a, "agent.run") == Effect.ALLOW, "A was changed by B's edit"


def test_4_an_adapter_cannot_grant_its_own_authorisation(pair):
    """Restating the M7 rule under two tenants: the engine decides."""
    from regente.engine import readiness
    from regente.ports.agent import AgentAvailability, AgentRunner, Check

    class Liar(AgentRunner):
        name = "liar"

        def run(self, mission):
            raise NotImplementedError

        def availability(self):
            return AgentAvailability(
                adapter="liar", executable=Check.yes(), protocol=Check.yes(),
                authentication=Check.yes(), agent=Check.yes(),
                policy=Check.yes("client A says yes"),
                budget=Check.yes("and has money"))

    a, _ = pair
    state = readiness.diagnose(
        Liar(),
        policy=a.policy(), autonomy=AutonomyLevel.L3,
        organization=ORGANIZATION, client=a.client,
        workspace=SHARED_WORKSPACE_NAME, resource="acme/worker",
        ceiling_usd=10.0, max_dispatches=5)
    assert "client A says yes" not in state.policy.detail


# ---------------------------------------------------------------------------
# 5. Budget isolation
# ---------------------------------------------------------------------------

def test_5_exhausting_one_clients_budget_leaves_the_other_working(pair):
    a, b = pair
    a.write_task("A-1", resources=("repo:a1",))
    a.write_task("A-2", resources=("repo:a2",))
    a.write_task("A-3", resources=("repo:a3",))
    for i in range(1, 4):
        b.write_task(f"B-{i}", resources=(f"repo:b{i}",))

    for _ in range(6):
        a.tick()
        b.tick()
        a.clock.advance(minutes=2)
        b.clock.advance(minutes=2)

    spent_a = a.dispatches_today()
    spent_b = b.dispatches_today()
    assert spent_a == a.max_dispatches, "A must stop at its own ceiling"
    assert spent_b > spent_a, "B has a larger budget and must have used it"

    # And A's spending did not consume B's.
    assert spent_b <= b.max_dispatches


def test_5_the_counter_is_per_client_not_a_singleton(pair):
    a, b = pair
    day = a.clock().strftime("%Y-%m-%d")
    a.store.mark_dispatch(a.workspace_id, day)
    a.store.mark_dispatch(a.workspace_id, day)

    assert a.store.dispatch_count(a.workspace_id, day) == 2
    assert b.store.dispatch_count(b.workspace_id, day) == 0, (
        "a global counter would show A's spending in B")


def test_5_the_day_boundary_resets_each_client_separately(pair):
    a, b = pair
    day_one = a.clock().strftime("%Y-%m-%d")
    a.store.mark_dispatch(a.workspace_id, day_one)
    b.store.mark_dispatch(b.workspace_id, day_one)
    b.store.mark_dispatch(b.workspace_id, day_one)

    a.clock.at = a.clock.at.replace(hour=0, minute=5) + timedelta(days=1)
    day_two = a.clock().strftime("%Y-%m-%d")

    assert a.store.dispatch_count(a.workspace_id, day_two) == 0
    assert a.store.dispatch_count(a.workspace_id, day_one) == 1
    assert b.store.dispatch_count(b.workspace_id, day_one) == 2, (
        "A crossing midnight reset B's yesterday")


def test_5_a_restart_preserves_each_clients_own_budget(pair):
    a, b = pair
    day = a.clock().strftime("%Y-%m-%d")
    for _ in range(2):
        a.store.mark_dispatch(a.workspace_id, day)
    b.store.mark_dispatch(b.workspace_id, day)

    a.crash()
    b.crash()
    assert a.store.dispatch_count(a.workspace_id, day) == 2
    assert b.store.dispatch_count(b.workspace_id, day) == 1


# ---------------------------------------------------------------------------
# 6. Secret isolation
# ---------------------------------------------------------------------------

def test_6_each_client_resolves_only_its_own_secret(pair, monkeypatch):
    a, b = pair
    monkeypatch.setenv("SECRET_A", a.secret_value)
    monkeypatch.setenv("SECRET_B", b.secret_value)

    assert a.secrets().resolve("env:SECRET_A") == a.secret_value
    assert b.secrets().resolve("env:SECRET_B") == b.secret_value

    with pytest.raises(Exception):
        a.secrets().resolve("env:SECRET_B")
    with pytest.raises(Exception):
        b.secrets().resolve("env:SECRET_A")


def test_6_a_refusal_does_not_reveal_the_other_clients_value(pair, monkeypatch):
    """The message a person reads must not carry what it refused to give."""
    a, b = pair
    monkeypatch.setenv("SECRET_B", b.secret_value)
    try:
        a.secrets().resolve("env:SECRET_B")
        raise AssertionError("client A resolved client B's secret")
    except Exception as e:
        assert b.secret_value not in str(e)


# ---------------------------------------------------------------------------
# 10. Observability isolation
# ---------------------------------------------------------------------------

def test_10_health_reports_only_the_asking_clients_world(pair):
    a, b = pair
    a.write_task("A-ONLY", resources=("repo:a",))
    b.write_task("B-ONLY", resources=("repo:b",))
    work_both(a, b, ticks=3)

    report = health_module.inspect(a.store, a.workspace_id, a.client,
                                   areas_root=a.areas, max_dispatches=2,
                                   when=a.clock())
    rendered = report.render()
    assert "B-ONLY" not in rendered
    assert b.workspace_id not in rendered
    for task in b.tasks():
        assert task.id not in rendered


def test_10_events_are_scoped_to_the_asking_client(pair):
    a, b = pair
    a.write_task("A-ONLY")
    b.write_task("B-ONLY")
    work_both(a, b, ticks=2)

    a_events = a.store.events(a.workspace_id, limit=500)
    assert a_events
    assert all(e.workspace_id == a.workspace_id for e in a_events)
    assert not any("B-ONLY" in (e.summary or "") for e in a_events)


def test_10_aggregate_telemetry_may_count_without_revealing(pair):
    """The line between global operational telemetry and tenant data.

    A count of workspaces or active runs is operational and safe. A task key, a
    repository, a secret reference or an escalation belongs to one tenant and
    must never appear in a global view.
    """
    a, b = pair
    a.write_task("A-ONLY")
    b.write_task("B-ONLY")
    work_both(a, b, ticks=2)

    workspaces = a.store.workspaces()
    assert len(workspaces) == 2, "the fleet view may know how many tenants exist"
    assert {w.id for w in workspaces} == {a.workspace_id, b.workspace_id}

    # Aggregates are counts. They carry no tenant content.
    total_runs = sum(len(a.store.active_runs(w.id)) for w in workspaces)
    assert isinstance(total_runs, int)


# ---------------------------------------------------------------------------
# 11. Cross-tenant identity attacks
# ---------------------------------------------------------------------------

def test_11_an_approval_id_from_another_client_is_refused(pair):
    """The most exposed untrusted identifier: one a person types."""
    a, b = pair
    a.write_task()
    b.write_task()
    work_both(a, b, ticks=3)

    theirs = b.approvals()
    assert theirs, "client B should have something waiting"

    assert a.store.approval(theirs[0].id, a.workspace_id) is None
    with pytest.raises(CorruptedState, match="nao pertence"):
        a.store.decide_approval(theirs[0].id, "investigar", per="attacker",
                                workspace_id=a.workspace_id)
    assert b.approvals(), "the other client's decision queue was drained"


def test_11_a_run_id_from_another_client_reads_as_absent(pair):
    a, b = pair
    a.write_task()
    b.write_task()
    work_both(a, b, ticks=3)

    theirs = b.store.runs_in_state(b.workspace_id, RunState.SUCCEEDED.value)
    assert theirs
    assert a.store.run(theirs[0].id, a.workspace_id) is None


def test_11_a_lease_owner_from_another_client_cannot_be_impersonated(pair):
    a, b = pair
    at = now()
    run_b = Run(id="run_b", task_id="t", workspace_id=b.workspace_id,
                agent="coder", state=RunState.RUNNING, started_at=at)
    assert b.store.claim(run_b, (SHARED_RESOURCE,), 300, when=at)

    from regente.engine.ownership import Ownership, OwnershipLost

    # Client A claims to be B's run, in A's workspace.
    impostor = Ownership(store=a.store, workspace_id=a.workspace_id,
                         run_id="run_b", resources=(SHARED_RESOURCE,),
                         lease_seconds=300, clock=lambda: at)
    with pytest.raises(OwnershipLost):
        impostor.verify()


def test_11_a_claim_cannot_be_made_for_another_clients_workspace(pair):
    """The run carries the workspace; a mismatched one writes into its own.

    Asserted so the behaviour is deliberate rather than incidental: the claim
    uses `run.workspace_id`, which the engine sets from its own context and
    never from anything a caller passed alongside.
    """
    a, b = pair
    at = now()
    run = Run(id="run_x", task_id="t", workspace_id=b.workspace_id,
              agent="coder", state=RunState.RUNNING, started_at=at)
    assert a.store.claim(run, (SHARED_RESOURCE,), 300, when=at)

    assert b.store.holds_lease(SHARED_RESOURCE, "run_x", b.workspace_id, when=at)
    assert not a.store.holds_lease(SHARED_RESOURCE, "run_x", a.workspace_id,
                                   when=at), (
        "the lease landed in the caller's workspace rather than the run's")


# ---------------------------------------------------------------------------
# 12. Persistence and restart
# ---------------------------------------------------------------------------

def test_12_both_contexts_survive_a_restart_from_the_same_store(pair, tmp_path):
    a, b = pair
    a.write_task("A-ONLY")
    b.write_task("B-ONLY")
    work_both(a, b, ticks=3)

    before_a = {t.key for t in a.tasks()}
    before_b = {t.key for t in b.tasks()}
    database = a.database
    a.close()
    b.close()

    reopened = SqliteStore(database)
    reopened.migrate()
    try:
        assert {t.key for t in reopened.tasks(a.workspace_id)} == before_a
        assert {t.key for t in reopened.tasks(b.workspace_id)} == before_b
        assert not ({t.id for t in reopened.tasks(a.workspace_id)}
                    & {t.id for t in reopened.tasks(b.workspace_id)})
        # Each tenant against its OWN work-area root. Checking B's workspace
        # against A's directory would report A's areas as orphaned, which is a
        # statement about the test rather than about the engine.
        for workspace_id, areas in ((a.workspace_id, a.areas),
                                    (b.workspace_id, b.areas)):
            assert not check_invariants(reopened, workspace_id, a.clock(),
                                        areas)
    finally:
        reopened.close()


def test_12_a_restart_does_not_rebuild_a_context_from_defaults(pair):
    """The workspace row must come back as it was written, not as a default."""
    a, b = pair
    work_both(a, b, ticks=1)
    a.crash()

    restored = a.store.workspace(a.workspace_id)
    assert restored is not None
    assert restored.client_id == a.client_id
    assert restored.name == SHARED_WORKSPACE_NAME
    assert restored.id != b.workspace_id


# ---------------------------------------------------------------------------
# 9. No accidental global state
# ---------------------------------------------------------------------------

#: Module-level mutable containers that are global BY DESIGN. Each is a constant
#: lookup table or a registry of factories, and none holds tenant data. Anything
#: not on this list is a finding: the audit exists to make a new global an
#: explicit decision rather than an accident.
GLOBAL_BY_DESIGN = {
    ("registry.py", "_REGISTRO"),          # capability -> factory, no tenant data
    ("claude_code.py", "OUTCOME_SCHEMA"),  # a JSON schema
    ("jira.py", "STATUS_MAP"), ("jira.py", "CATEGORY_MAP"),
    ("jira.py", "PRIORITY_MAP"), ("jira.py", "LINK_MAP"),
    ("policy.py", "_SEVERIDADE"), ("policy.py", "REQUIRED_LEVEL"),
    ("states.py", "_AVANCOS"),
    ("context.py", "NOISE"), ("observation.py", "NOISE"),
    ("observation.py", "_STATUS"),
    ("store_sqlite.py", "MIGRATIONS"),
    # Vocabulario, nao estado. `MEANING` e uma frase por estado da maquina;
    # `UI_TYPES` e a lista fechada de extensoes que a Mission Control pode
    # servir. Nenhum dos dois recebe escrita depois da importacao, e nenhum
    # guarda nada de tenant -- mas passam pela declaracao como todo o resto,
    # porque a guarda so vale enquanto ninguem acrescenta silenciosamente.
    ("states.py", "MEANING"), ("api.py", "UI_TYPES"),
    # Como cada recusa do Core vira HTTP. Tabela de traducao, nao estado: sem
    # escrita depois da importacao e sem nada de tenant. Declarada como todo o
    # resto, porque a guarda so vale enquanto ninguem acrescenta em silencio.
    ("api.py", "DENIAL_STATUS"), ("api.py", "REFUSAL_STATUS"),
    ("api.py", "CREDENTIAL_STATUS"),
    # Vocabulario de papeis: tres nomes, cada um com um conjunto fixo de
    # capacidades. Nao recebe escrita depois da importacao e nao guarda nada de
    # tenant -- mas passa pela declaracao como todo o resto, porque a guarda so
    # vale enquanto ninguem acrescenta em silencio.
    ("access.py", "ROLES"),
}


def test_9_no_module_level_state_holds_tenant_data():
    """A singleton is how tenancy leaks without a single wrong `WHERE`.

    Every module-level mutable container in the engine has to be a named,
    reviewed constant. A cache keyed by task or repository, a "current
    workspace", a shared counter -- any of those work perfectly with one client
    and leak with two.
    """
    import ast

    root = Path(__file__).resolve().parents[1] / "regente"
    found = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target] if isinstance(node, ast.AnnAssign)
                       else [])
            for target in targets:
                name = getattr(target, "id", None)
                value = getattr(node, "value", None)
                if not name or value is None:
                    continue
                mutable = isinstance(value, (ast.Dict, ast.List, ast.Set))
                built = (isinstance(value, ast.Call)
                         and getattr(value.func, "id", "") in
                         ("dict", "list", "set", "defaultdict"))
                if mutable or built:
                    found.add((path.name, name))

    unexpected = found - GLOBAL_BY_DESIGN
    assert not unexpected, (
        "module-level mutable state that nobody declared global by design:\n  "
        + "\n  ".join(f"{m}:{n}" for m, n in sorted(unexpected)))


def test_9_nothing_caches_across_tenants():
    """A memoised lookup keyed by a local name is a cross-tenant read."""
    import ast

    root = Path(__file__).resolve().parents[1] / "regente"
    caches = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for deco in node.decorator_list:
                    text = ast.dump(deco)
                    if any(m in text for m in ("lru_cache", "cache", "memoize")):
                        caches.append(f"{path.name}:{node.name}")
    assert not caches, ("caching across tenants:\n  " + "\n  ".join(caches))


# ---------------------------------------------------------------------------
# 7 & 8. Real processes, interleaved, with kills on either side
# ---------------------------------------------------------------------------

def multitenant(tmp_path, **kw) -> dict:
    args = [sys.executable, str(HERE / "multitenant.py"),
            "--root", str(tmp_path / kw.pop("name", "mt")),
            "--seconds", str(kw.pop("seconds", 3)),
            "--workers-each", str(kw.pop("workers_each", 2)),
            "--repeat", str(kw.pop("repeat", 2))]
    for key, value in kw.items():
        args += [f"--{key.replace('_', '-')}", str(value)]
    done = subprocess.run(args, capture_output=True, encoding="utf-8",
                          errors="replace", timeout=600)
    return {"exit": done.returncode, "out": done.stdout + done.stderr}


@pytest.mark.slow
def test_7_two_clients_operate_concurrently_in_separate_processes(tmp_path):
    """Not A then B. Both at once, in their own interpreters, one database."""
    result = multitenant(tmp_path, name="concurrent", seconds=3,
                         workers_each=2, repeat=2)
    assert "no tenancy boundary crossed" in result["out"], result["out"][-2000:]
    assert result["exit"] == 0


@pytest.mark.slow
def test_8_killing_one_clients_workers_never_freezes_the_other(tmp_path):
    """A failure on one side must not stop the other side working."""
    result = multitenant(tmp_path, name="killed", seconds=3, workers_each=2,
                         repeat=3, kill="both", kill_at=1.0)
    assert "no tenancy boundary crossed" in result["out"], result["out"][-2000:]
    assert "one client did no work at all" not in result["out"], (
        "killing one client's workers stopped the other from working")
    assert result["exit"] == 0


# ---------------------------------------------------------------------------
# Defects these scenarios found
# ---------------------------------------------------------------------------

def test_opening_a_store_while_another_holds_the_write_lock(tmp_path):
    """Four processes starting at once found this; one process never could.

    `busy_timeout` used to be set AFTER `journal_mode=WAL`, which takes a lock
    to switch modes -- so a process opening the database while another was
    writing raised `database is locked` from its own constructor, with the
    setting that would have made it wait sitting two lines below, unreached.
    """
    import sqlite3
    import threading

    path = tmp_path / "contended.db"
    first = SqliteStore(path)
    first.migrate()
    try:
        blocker = sqlite3.connect(str(path), isolation_level=None,
                                  check_same_thread=False)
        blocker.execute("PRAGMA busy_timeout=100")
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("INSERT INTO counters(workspace_id, day, name, value) "
                        "VALUES('w','2026-01-01','x',1)")

        def let_go():
            import time as _t
            _t.sleep(0.4)
            blocker.execute("COMMIT")
            blocker.close()

        threading.Thread(target=let_go, daemon=True).start()

        second = SqliteStore(path)      # must wait, not raise
        second.migrate()
        second.close()
    finally:
        first.close()


def test_a_run_that_loses_possession_returns_its_task_to_the_queue(tmp_path):
    """Losing a lease with nobody else waiting used to strand the task for good.

    Recovery finds dead workers by matching expired leases against active runs.
    Abandoning released the leases and ended the run, so there was nothing left
    for recovery to match -- the task sat in an active state that the scheduler
    skips, owned by nobody, permanently.

    Only reachable when a mission outlives its lease AND no other worker is
    waiting, which is why a thousand-tick soak and forty contention rounds never
    produced it.
    """
    from faults import FrozenRenewal

    a, _ = two_tenants(tmp_path / "strand")
    a.lease_seconds = 30
    a.open()
    a.write_task("STRAND-1", resources=("repo:only",))
    try:
        a.tick()
        a.clock.advance(minutes=1)

        class BurnsTheClock:
            name = "burner"

            def __init__(self, clock, inner):
                self.clock, self.inner = clock, inner

            def availability(self):
                return self.inner.availability()

            def describe(self):
                return {"adapter": "burner"}

            def run(self, mission):
                self.clock.advance(seconds=300)   # far past the lease
                return self.inner.run(mission)

        a.orchestrator.store = FrozenRenewal(inner=a.store)
        a.orchestrator.runner = BurnsTheClock(a.clock, a.orchestrator.runner)
        outcome = a.orchestrator.tick()

        assert any("ownership lost" in e for e in outcome.errors)
        task = a.store.task_by_key(a.workspace_id, "filesystem", "STRAND-1")
        assert task.state is TaskState.READY, (
            f"the task was left in {task.state.value}, where no tick reaches it")
        assert not check_invariants(a.store, a.workspace_id, a.clock(), a.areas)
    finally:
        a.close()


def test_a_task_taken_by_another_worker_is_left_alone_when_abandoning(tmp_path):
    """The other half: if somebody else owns it, hands off.

    Returning the task to the queue is right only when nobody took it. Doing it
    unconditionally would rip a task out from under the worker that legitimately
    owns it now.
    """
    from regente.core.model import Run, RunState

    a, _ = two_tenants(tmp_path / "handsoff")
    a.lease_seconds = 30
    a.open()
    a.write_task("HANDS-1", resources=("repo:only",))
    try:
        from regente.engine.orchestrator import TickReport

        a.tick()                       # discovery only
        a.clock.advance(minutes=1)
        # Analysis is driven directly rather than by another tick: a tick would
        # also dispatch, and the task would run the whole cycle before the
        # scenario could be set up. The state under test has to be arranged, not
        # hoped for.
        a.orchestrator._analyze(TickReport(workspace=a.client))

        task = a.store.task_by_key(a.workspace_id, "filesystem", "HANDS-1")
        assert task.state is TaskState.READY
        rival = Run(id="run_rival", task_id=task.id,
                    workspace_id=a.workspace_id, agent="rival",
                    state=RunState.RUNNING, started_at=a.clock())
        a.store.save_run(rival)

        lost = Run(id="run_lost", task_id=task.id, workspace_id=a.workspace_id,
                   agent="coder", state=RunState.RUNNING, started_at=a.clock())
        a.store.save_run(lost)
        a.orchestrator._transition(task.id, TaskState.ASSIGNED,
                                   actor="orchestrator", reason="setup")
        a.orchestrator._transition(task.id, TaskState.IMPLEMENTING,
                                   actor="coder", reason="setup")

        report = TickReport(workspace=a.client)
        a.orchestrator._abandon(task.id, lost, [], report)

        after = a.store.task(task.id, a.workspace_id)
        assert after.state is TaskState.IMPLEMENTING, (
            "a task owned by another live run was pulled back to the queue")
        assert report.recovered == ()
    finally:
        a.close()


# ---------------------------------------------------------------------------
# Gaps the tenancy sweep found
# ---------------------------------------------------------------------------

def write_config(root: Path, organization: str, client: str,
                 workspace: str) -> Path:
    """A real configuration file, loaded by the real composition root."""
    (root / "tasks").mkdir(parents=True, exist_ok=True)
    path = root / "regente.yaml"
    path.write_text(f"""
organization: {organization}
client: {client}
workspace: {workspace}
autonomy: L2
root: {(root / '.regente').as_posix()}
providers:
  tasks:
    name: filesystem
    directory: {(root / 'tasks').as_posix()}
  workspace_provider:
    name: directory
  runner:
    name: script
""", encoding="utf-8")
    return path


def built_ids(path: Path) -> tuple[str, str]:
    from regente.app.config import load
    from regente.app.container import build

    engine = build(load(path))
    try:
        return engine.workspace.id, engine.workspace.client_id
    finally:
        engine.close()


def test_the_composition_root_derives_identity_from_the_whole_spine(tmp_path):
    """Asserted through `build()`, not through the test harness's own copy.

    The tenancy tests computed workspace ids themselves, so removing the client
    from the derivation in `container.py` broke nothing -- every test agreed
    with the mutation. Identity has to be checked where it is actually made.
    """
    same_workspace_a = write_config(tmp_path / "a", "acme", "client-a", "main")
    same_workspace_b = write_config(tmp_path / "b", "acme", "client-b", "main")
    other_org = write_config(tmp_path / "c", "other-org", "client-a", "main")

    ws_a, client_a = built_ids(same_workspace_a)
    ws_b, client_b = built_ids(same_workspace_b)
    ws_c, client_c = built_ids(other_org)

    assert ws_a != ws_b, (
        "two clients with a workspace of the same name got the same id; "
        "identity is being derived from the workspace name alone")
    assert client_a != client_b

    assert ws_a != ws_c, (
        "the same client name under two organizations collided; the "
        "organization is part of the identity")
    assert client_a != client_c


def test_the_store_itself_refuses_a_transition_from_another_workspace(tmp_path):
    """The store's own guard, tested directly.

    The orchestrator refuses earlier -- its scoped read returns nothing, so it
    never reaches the store. That made the store's check unexercised: deleting
    it broke no test while leaving anything that calls `transition` directly one
    argument away from moving another tenant's task.
    """
    a, b = two_tenants(tmp_path / "direct")
    a.open()
    b.open()
    try:
        b.write_task("THEIRS")
        b.tick()
        theirs = b.tasks()[0]
        before = theirs.state

        with pytest.raises(CorruptedState, match="outro workspace"):
            a.store.transition(theirs.id, TaskState.CANCELLED,
                               actor="client-a", reason="reaching across",
                               workspace_id=a.workspace_id)
        assert b.store.task(theirs.id, b.workspace_id).state is before
    finally:
        a.close()
        b.close()


def test_a_claim_writes_the_lease_into_the_runs_workspace(tmp_path):
    """The claim reads `run.workspace_id`, never a workspace passed beside it.

    Which means a run object carries its own tenancy and the lease lands where
    the run belongs -- not where the caller happens to be looking.
    """
    a, b = two_tenants(tmp_path / "claimscope")
    a.open()
    b.open()
    try:
        at = now()
        run = Run(id="run_owned_by_b", task_id="t",
                  workspace_id=b.workspace_id, agent="coder",
                  state=RunState.RUNNING, started_at=at)
        # Claimed through A's store object, for a run that belongs to B.
        assert a.store.claim(run, (SHARED_RESOURCE,), 300, when=at)

        assert b.store.holds_lease(SHARED_RESOURCE, "run_owned_by_b",
                                   b.workspace_id, when=at)
        assert not a.store.holds_lease(SHARED_RESOURCE, "run_owned_by_b",
                                       a.workspace_id, when=at)
        assert a.store.leases(a.workspace_id) == []

        # And A can still claim the same resource name for itself.
        mine = Run(id="run_owned_by_a", task_id="t",
                   workspace_id=a.workspace_id, agent="coder",
                   state=RunState.RUNNING, started_at=at)
        assert a.store.claim(mine, (SHARED_RESOURCE,), 300, when=at)
    finally:
        a.close()
        b.close()


def test_the_orchestrators_helper_always_names_its_own_tenant(tmp_path):
    """The helper is what makes forgetting impossible; test that it does not.

    `_moved_by_another` refuses a foreign task before reaching the store, so the
    orchestrator never exercises the store's guard through that path. This calls
    `_transition` directly -- the way every other transition in the tick does --
    with a task belonging to the other client.
    """
    a, b = two_tenants(tmp_path / "helper")
    a.open()
    b.open()
    try:
        b.write_task("THEIRS")
        b.tick()
        theirs = b.tasks()[0]
        before = theirs.state

        with pytest.raises(CorruptedState, match="outro workspace"):
            a.orchestrator._transition(theirs.id, TaskState.CANCELLED,
                                       actor="client-a", reason="across")
        assert b.store.task(theirs.id, b.workspace_id).state is before
    finally:
        a.close()
        b.close()


def test_listing_tasks_is_scoped_and_never_returns_the_other_client(tmp_path):
    """The plainest query in the system, and the one that would leak everything."""
    a, b = two_tenants(tmp_path / "listing")
    a.open()
    b.open()
    try:
        a.write_task("A-ONE")
        a.write_task("A-TWO")
        b.write_task("B-ONE")
        for _ in range(2):
            a.tick()
            b.tick()
            a.clock.advance(minutes=5)
            b.clock.advance(minutes=5)

        a_keys = {t.key for t in a.store.tasks(a.workspace_id)}
        b_keys = {t.key for t in b.store.tasks(b.workspace_id)}
        assert a_keys == {"A-ONE", "A-TWO"}
        assert b_keys == {"B-ONE"}
        assert all(t.workspace_id == a.workspace_id
                   for t in a.store.tasks(a.workspace_id))

        # And filtered by state, which takes a different code path.
        for state in (TaskState.READY, TaskState.WAITING_HUMAN):
            for task in a.store.tasks(a.workspace_id, [state]):
                assert task.workspace_id == a.workspace_id
    finally:
        a.close()
        b.close()
