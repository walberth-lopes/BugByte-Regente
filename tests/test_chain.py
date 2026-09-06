# -*- coding: utf-8 -*-
"""Resolucao de alvo e cadeia de execucao. Puro, sem I/O.

O que estes testes protegem, acima de tudo: **o motor nao adivinha onde uma task
roda.** Ambiguidade e ausencia sao desfechos legitimos, e trocar qualquer um dos
dois por um chute e o defeito mais caro que este elo poderia ter -- porque o
resultado nao seria um error, seria codigo escrito no repositorio errado.
"""

from __future__ import annotations

import pytest

from regente.core.policy import AutonomyLevel, Effect, PolicyEngine
from regente.core.risk import RiskEngine, RiskLevel
from regente.engine import chain
from regente.engine.target import Confidence, TargetResolver
from regente.engine.chain import Stage
from regente.ports.repository import (READ_CAPS, Branch, RepoCapability, RepoInfo, RepoRef)
from regente.ports.tasks import ExternalStatus, ExternalTask

RULES = [
    {"name": "codigo", "effect": "ALLOW", "match": {"action": "repo.branch*"}},
    {"name": "nada_em_producao", "effect": "DENY",
     "match": {"action": "repo.*", "environment": "production"}},
]


def repo(key: str, base: str = "main", archived: bool = False,
         caps: frozenset[RepoCapability] = READ_CAPS) -> RepoInfo:
    return RepoInfo(ref=RepoRef(provider="p", key=key),
                    name=key.rsplit("/", 1)[-1], base_branch=base,
                    archived=archived, capabilities=caps)


def task(key: str, status=ExternalStatus.NOT_STARTED, labels=(), project="P") -> ExternalTask:
    return ExternalTask(key=key, title=f"trabalho {key}", status=status,
                        external_status="TO DO", labels=tuple(labels), project=project)


REPOS = [repo("acme/api"), repo("acme/web"), repo("acme/worker")]


# ---------------------------------------------------------------------------
# Resolucao de alvo
# ---------------------------------------------------------------------------

def test_without_evidence_is_missing_is_not_a_guess():
    a = TargetResolver().resolve(task("K-1"), REPOS)
    assert a.confidence is Confidence.ABSENT
    assert a.repo is None
    assert not a.candidates


def test_map_by_task_is_declared():
    r = TargetResolver(by_task={"K-1": "acme/api"})
    a = r.resolve(task("K-1"), REPOS)
    assert a.confidence is Confidence.DECLARED
    assert a.repo.ref.key == "acme/api"
    assert a.base_branch == "main"


def test_map_by_label_is_by_project():
    a = TargetResolver(by_label={"backend": "acme/api"}).resolve(
        task("K-1", labels=["backend"]), REPOS)
    assert a.repo.ref.key == "acme/api"
    b = TargetResolver(by_project={"P": "acme/web"}).resolve(task("K-2"), REPOS)
    assert b.repo.ref.key == "acme/web"


def test_map_accepts_name_short_when_not_ha_ambiguity():
    a = TargetResolver(by_task={"K-1": "api"}).resolve(task("K-1"), REPOS)
    assert a.repo.ref.key == "acme/api"


def test_name_short_ambiguous_not_enters_in_index():
    """Dois repositorios chamados `api` em orgs diferentes nao podem ser
    resolvidos por nome curto -- isso seria reintroduzir o chute pela port
    dos fundos."""
    repos = REPOS + [repo("outra/api")]
    a = TargetResolver(by_task={"K-1": "api"}).resolve(task("K-1"), repos)
    assert a.confidence is Confidence.ABSENT


def test_branch_existente_is_evidence_observed():
    a = TargetResolver().resolve(
        task("K-1"), REPOS,
        branches={"acme/api": [Branch(name="feat/K-1-coisa")]})
    assert a.confidence is Confidence.OBSERVED
    assert a.repo.ref.key == "acme/api"
    assert "K-1" in a.reason


def test_branch_casa_by_word_whole():
    """`K-1` nao pode casar com `K-11`: seria trabalho no repositorio errado."""
    a = TargetResolver().resolve(
        task("K-1"), REPOS, branches={"acme/api": [Branch(name="feat/K-11-outra")]})
    assert a.confidence is Confidence.ABSENT


def test_declarado_beats_observed():
    """Uma branch pode ser resto de tentativa abandonada; um mapa e afirmacao."""
    a = TargetResolver(by_task={"K-1": "acme/web"}).resolve(
        task("K-1"), REPOS, branches={"acme/api": [Branch(name="feat/K-1-x")]})
    assert a.confidence is Confidence.DECLARED
    assert a.repo.ref.key == "acme/web"


def test_tie_is_ambiguous_is_the_motor_not_tiebreak():
    a = TargetResolver().resolve(
        task("K-1"), REPOS,
        branches={"acme/api": [Branch(name="feat/K-1-x")],
                  "acme/web": [Branch(name="fix/K-1-y")]})
    assert a.confidence is Confidence.AMBIGUOUS
    assert a.repo is None, "o motor escolheu um dos dois"
    assert len(a.candidates) == 2


def test_declaration_tiebreak_the_ambiguous():
    branches = {"acme/api": [Branch(name="feat/K-1-x")],
                "acme/web": [Branch(name="fix/K-1-y")]}
    a = TargetResolver(by_task={"K-1": "acme/api"}).resolve(task("K-1"), REPOS, branches)
    assert a.confidence is Confidence.DECLARED
    assert a.repo.ref.key == "acme/api"


def test_every_evidence_is_auditable():
    a = TargetResolver(by_label={"backend": "acme/api"}).resolve(
        task("K-1", labels=["backend"]), REPOS,
        branches={"acme/api": [Branch(name="feat/K-1-x")]})
    fontes = {e.source for e in a.candidates[0].evidence}
    assert fontes == {"map:label", "branch"}
    assert all(e.detail for e in a.candidates[0].evidence)


def test_target_that_not_exists_in_provider_is_ignored():
    """Mapa apontando para repositorio inexistente nao pode virar alvo fantasma."""
    a = TargetResolver(by_task={"K-1": "acme/nao-existe"}).resolve(task("K-1"), REPOS)
    assert a.confidence is Confidence.ABSENT


# ---------------------------------------------------------------------------
# Cadeia
# ---------------------------------------------------------------------------

def build(tasks, repos=None, resolvedor=None, autonomy=AutonomyLevel.L2,
          branches=None, environment="staging"):
    return chain.build(
        workspace_nome="ws", workspace_id="wks_1", tasks=tasks,
        repos=repos if repos is not None else REPOS,
        resolvedor=resolvedor or TargetResolver(),
        policy=PolicyEngine.from_config(RULES), risk=RiskEngine(),
        autonomy=autonomy, branches=branches, environment=environment)


def test_chain_completa_produces_candidate():
    r = build([task("K-1")], resolvedor=TargetResolver(by_task={"K-1": "acme/api"}))
    p = r.steps[0]
    assert p.elo is Stage.CANDIDATO
    assert p.repo.ref.key == "acme/api"
    assert p.base_branch == "main"
    assert p.work_branch == "regente/k-1"
    assert p.resources == ("repo:wks_1/p/acme/api",)
    assert p.decision.effect == Effect.ALLOW
    assert r.mutations == 0


def test_to_in_first_stage_when_not_ha_work():
    r = build([task("K-1", status=ExternalStatus.IN_PROGRESS)])
    assert r.steps[0].elo is Stage.SEM_TRABALHO


def test_to_in_without_target():
    assert build([task("K-1")]).steps[0].elo is Stage.SEM_ALVO


def test_to_in_ambiguous():
    r = build([task("K-1")],
              branches={"acme/api": [Branch(name="feat/K-1-x")],
                        "acme/web": [Branch(name="fix/K-1-y")]})
    assert r.steps[0].elo is Stage.ALVO_AMBIGUO


def test_repositorio_archived_not_receives_work():
    repos = [repo("acme/api", archived=True)]
    r = build([task("K-1")], repos=repos,
              resolvedor=TargetResolver(by_task={"K-1": "acme/api"}))
    assert r.steps[0].elo is Stage.REPO_INUTILIZAVEL


def test_repositorio_without_branch_base_not_receives_work():
    """Derivar da base errada produz um PR de conflito que ninguem pediu."""
    repos = [repo("acme/api", base="")]
    r = build([task("K-1")], repos=repos,
              resolvedor=TargetResolver(by_task={"K-1": "acme/api"}))
    assert r.steps[0].elo is Stage.REPO_INUTILIZAVEL


def test_capacidade_missing_to_the_chain_before_of_spending_a_cycle():
    repos = [repo("acme/api", caps=frozenset({RepoCapability.READ_METADATA}))]
    r = build([task("K-1")], repos=repos,
              resolvedor=TargetResolver(by_task={"K-1": "acme/api"}))
    p = r.steps[0]
    assert p.elo is Stage.SEM_CAPACIDADE
    assert "clone" in p.reason or "read_files" in p.reason


def test_ceiling_of_autonomy_becomes_request_to_human():
    r = build([task("K-1")], resolvedor=TargetResolver(by_task={"K-1": "acme/api"}),
              autonomy=AutonomyLevel.L0)
    assert r.steps[0].elo is Stage.PRECISA_HUMANO


def test_policy_blocks_production():
    r = build([task("K-1")], resolvedor=TargetResolver(by_task={"K-1": "acme/api"}),
              environment="production")
    p = r.steps[0]
    assert p.elo is Stage.BARRADO_POR_POLICY
    assert p.decision.rule == "nada_em_producao"


def test_resource_is_scoped_by_workspace_in_chain():
    r1 = build([task("K-1")], resolvedor=TargetResolver(by_task={"K-1": "acme/api"}))
    r2 = chain.build(
        workspace_nome="ws2", workspace_id="wks_2", tasks=[task("K-1")], repos=REPOS,
        resolvedor=TargetResolver(by_task={"K-1": "acme/api"}),
        policy=PolicyEngine.from_config(RULES), risk=RiskEngine(),
        autonomy=AutonomyLevel.L2)
    assert r1.steps[0].resources != r2.steps[0].resources


def test_report_tells_where_the_chain_stopped():
    r = build([task("K-1"), task("K-2", status=ExternalStatus.IN_REVIEW),
               task("K-3")],
              resolvedor=TargetResolver(by_task={"K-1": "acme/api"}))
    assert r.by_stage["CANDIDATO"] == 1
    assert r.by_stage["SEM_TRABALHO"] == 1
    assert r.by_stage["SEM_ALVO"] == 1
    assert len(r.candidates) == 1


def test_text_of_report_mostra_evidence():
    r = build([task("K-1")], resolvedor=TargetResolver(by_task={"K-1": "acme/api"}))
    t = chain.render(r)
    assert "EXECUTION CANDIDATES (1)" in t
    assert "acme/api" in t
    assert "Mutations                  0" in t
