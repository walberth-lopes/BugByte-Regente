# -*- coding: utf-8 -*-
"""Maquina de estados, policy, risco, grafo e scheduler -- tudo sem I/O."""

from __future__ import annotations

import pytest

from regente.core.errors import InvalidTransition
from regente.core.graph import DependencyGraph
from regente.core.policy import (Action, AutonomyLevel, Effect, PolicyContext,
                                 PolicyEngine, required_level)
from regente.core.risk import RiskEngine, RiskLevel
from regente.core.scheduling import Candidate, Limits, plan
from regente.core.states import S, require, can, resumable_from


# ---- maquina de estados -------------------------------------------------

def test_path_happy_full():
    path = [S.DISCOVERED, S.ANALYZING, S.READY, S.ASSIGNED, S.IMPLEMENTING,
               S.TESTING, S.PR_CREATED, S.CI_RUNNING, S.AI_REVIEW, S.APPROVED,
               S.MERGING, S.DEPLOYING, S.QA_STAGING, S.DONE]
    for source, destination in zip(path, path[1:]):
        require(source, destination)


def test_transition_absurd_is_refused():
    with pytest.raises(InvalidTransition):
        require(S.DISCOVERED, S.DONE)
    with pytest.raises(InvalidTransition):
        require(S.READY, S.MERGING)


def test_terminal_not_exits():
    assert not can(S.DONE, S.IMPLEMENTING)
    assert not can(S.CANCELLED, S.READY)


def test_qa_rejected_returns_to_code():
    require(S.QA_STAGING, S.IMPLEMENTING)


def test_escalating_is_always_possible():
    """O motor nunca fica sem a opcao de parar e perguntar."""
    for state in S:
        if state in (S.DONE, S.CANCELLED, S.WAITING_HUMAN):
            continue
        assert can(state, S.WAITING_HUMAN), state


def test_waiting_human_returns_to_where_paused():
    assert can(S.WAITING_HUMAN, S.MERGING, pausado_em=S.APPROVED)
    assert can(S.WAITING_HUMAN, S.APPROVED, pausado_em=S.APPROVED)


def test_human_not_teleport_task():
    """Aprovar um deploy nao e o mesmo que declarar a task pronta."""
    assert not can(S.WAITING_HUMAN, S.DONE, pausado_em=S.IMPLEMENTING)
    assert S.DONE not in resumable_from(S.IMPLEMENTING)


# ---- policy --------------------------------------------------------------

RULES = [
    {"name": "ler", "effect": "ALLOW", "match": {"action": "*.read"}},
    {"name": "pr", "effect": "ALLOW", "match": {"action": "repo.pr*"}},
    {"name": "staging", "effect": "ALLOW",
     "match": {"action": "repo.merge", "environment": "staging"}},
    {"name": "prod", "effect": "HUMAN_APPROVAL",
     "match": {"action": "repo.merge", "environment": "production"}},
    {"name": "sem_banco", "effect": "DENY", "match": {"action": "db.write*"}},
]


def motor_policy() -> PolicyEngine:
    return PolicyEngine.from_config(RULES)


def ctx(kind: str, environment: str = "staging",
        autonomy: AutonomyLevel = AutonomyLevel.L4, resource: str = "*") -> PolicyContext:
    return PolicyContext(action=Action(kind=kind, resource=resource, environment=environment),
                         autonomy=autonomy)


def test_action_without_rule_is_denied():
    d = motor_policy().decide(ctx("cloud.provision"))
    assert d.effect == Effect.DENY
    assert "nenhuma regra" in d.reason


def test_read_passes():
    assert motor_policy().decide(ctx("repo.read")).allowed


def test_production_asks_human():
    d = motor_policy().decide(ctx("repo.merge", "production"))
    assert d.needs_human


def test_deny_beats_allow():
    """Uma regra permissiva nao anula uma proibicao."""
    regras = RULES + [{"name": "liberou_tudo", "effect": "ALLOW", "match": {"action": "*"}}]
    d = PolicyEngine.from_config(regras).decide(ctx("db.write"))
    assert d.effect == Effect.DENY
    assert d.rule == "sem_banco"


def test_ceiling_of_autonomy_tightens_allow():
    d = motor_policy().decide(ctx("repo.merge", "staging", autonomy=AutonomyLevel.L2))
    assert d.needs_human
    assert d.rule == "teto_de_autonomia"


def test_ceiling_not_loosens_deny():
    d = motor_policy().decide(ctx("db.write", autonomy=AutonomyLevel.L4))
    assert d.effect == Effect.DENY


def test_action_unknown_requires_the_level_max():
    assert required_level("capacidade.inventada") is AutonomyLevel.L4


# ---- risco ---------------------------------------------------------------

def test_production_is_high():
    a = RiskEngine().assess({"action": "deploy.staging", "environment": "production"})
    assert a.level is RiskLevel.HIGH
    assert a.requires_second_pass


def test_credential_is_critical():
    a = RiskEngine().assess({"action": "repo.commit", "environment": "local",
                             "paths": ["src/config/secrets.py"]})
    assert a.level is RiskLevel.CRITICAL


def test_change_trivial_is_low():
    a = RiskEngine().assess({"action": "repo.commit", "environment": "local",
                             "paths": ["README.md"], "lines": 12})
    assert a.level is RiskLevel.LOW
    assert not a.requires_second_pass


def test_signal_carries_evidence():
    a = RiskEngine().assess({"action": "repo.commit", "environment": "local",
                             "paths": ["db/migrations/004_add.sql"]})
    assert any("migration" in m for m in a.reasons)
    assert any("004_add.sql" in m for m in a.reasons)


def test_factor_of_client_adds_is_not_replaces():
    r = RiskEngine.from_config([{"name": "faturamento", "level": "HIGH",
                              "field": "paths", "matches": ["*cobranca*"]}])
    assert r.assess({"paths": ["src/cobranca/x.py"]}).level is RiskLevel.HIGH
    # o fator da base continua vivo
    assert r.assess({"environment": "production"}).level is RiskLevel.HIGH


# ---- grafo ---------------------------------------------------------------

def grafo_diamante() -> DependencyGraph:
    """A e B em paralelo; C depende dos dois. D e E em serie, a parte."""
    g = DependencyGraph()
    g.link("C", "A")
    g.link("C", "B")
    g.link("E", "D")
    return g


def test_parallel_is_series_coexist():
    g = grafo_diamante()
    ready = g.unblocked(set())
    assert ready == frozenset({"A", "B", "D"})
    assert "C" not in ready and "E" not in ready


def test_dependency_releases_when_parents_finish():
    g = grafo_diamante()
    assert "C" not in g.unblocked({"A"})
    assert "C" in g.unblocked({"A", "B"})


def test_layers_show_the_parallelism():
    assert grafo_diamante().layers() == [["A", "B", "D"], ["C", "E"]]


def test_cycle_is_reported_is_not_blowing_up():
    g = DependencyGraph()
    g.link("X", "Y")
    g.link("Y", "X")
    assert g.cycles()
    assert g.in_cycle() == frozenset({"X", "Y"})
    assert not g.unblocked(set()) & {"X", "Y"}


# ---- scheduler -----------------------------------------------------------

def test_dispatches_in_parallel_when_not_ha_conflict():
    g = grafo_diamante()
    cands = [Candidate("A", resources=frozenset({"repo:x"})),
             Candidate("B", resources=frozenset({"repo:y"})),
             Candidate("D", resources=frozenset({"repo:z"}))]
    p = plan(cands, g, set(), {}, Limits(max_workers=3))
    assert set(p.dispatch) == {"A", "B", "D"}


def test_not_parallelize_who_touches_the_same_resource():
    """O caso que produz dois workers na mesma migration."""
    g = DependencyGraph()
    for n in "AB":
        g.add(n)
    cands = [Candidate("A", priority=1, resources=frozenset({"migration:api"})),
             Candidate("B", priority=2, resources=frozenset({"migration:api"}))]
    p = plan(cands, g, set(), {}, Limits(max_workers=4))
    assert p.dispatch == ("A",)
    assert any("recurso ocupado" in a.reason for a in p.deferred)


def test_respects_worker_already_running():
    g = DependencyGraph()
    g.add("B")
    p = plan([Candidate("B", resources=frozenset({"repo:x"}))], g, set(),
                running_now={"A": frozenset({"repo:x"})}, limits=Limits(max_workers=4))
    assert not p.dispatch


def test_ceiling_of_slots():
    g = DependencyGraph()
    cands = []
    for n in "ABCD":
        g.add(n)
        cands.append(Candidate(n, resources=frozenset({f"repo:{n}"})))
    assert len(plan(cands, g, set(), {}, Limits(max_workers=2)).dispatch) == 2


def test_ceiling_daily():
    g = DependencyGraph()
    g.add("A")
    p = plan([Candidate("A")], g, set(), {}, Limits(max_dispatches_per_day=8),
                dispatched_today=8)
    assert not p.dispatch
    assert any("teto diario" in a.reason for a in p.deferred)


def test_order_is_stable():
    g = DependencyGraph()
    for n in "ABC":
        g.add(n)
    cands = [Candidate("C", priority=5, key="C", resources=frozenset({"r:c"})),
             Candidate("A", priority=1, key="A", resources=frozenset({"r:a"})),
             Candidate("B", priority=1, key="B", resources=frozenset({"r:b"}))]
    p = plan(cands, g, set(), {}, Limits(max_workers=3))
    assert p.dispatch == ("A", "B", "C")


def test_task_in_cycle_never_is_dispatched():
    g = DependencyGraph()
    g.link("X", "Y")
    g.link("Y", "X")
    p = plan([Candidate("X"), Candidate("Y")], g, set(), {})
    assert not p.dispatch
    assert set(p.in_cycle) == {"X", "Y"}
