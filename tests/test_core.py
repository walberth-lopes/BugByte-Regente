# -*- coding: utf-8 -*-
"""Maquina de estados, policy, risco, grafo e scheduler -- tudo sem I/O."""

from __future__ import annotations

import pytest

from regente.core.errors import TransicaoInvalida
from regente.core.graph import DependencyGraph
from regente.core.policy import (Action, AutonomyLevel, Effect, PolicyContext,
                                 PolicyEngine, nivel_exigido)
from regente.core.risk import RiskEngine, RiskLevel
from regente.core.scheduling import Candidata, Limites, planeja
from regente.core.states import S, exige, pode, retomaveis


# ---- maquina de estados -------------------------------------------------

def test_caminho_feliz_completo():
    caminho = [S.DISCOVERED, S.ANALYZING, S.READY, S.ASSIGNED, S.IMPLEMENTING,
               S.TESTING, S.PR_CREATED, S.CI_RUNNING, S.AI_REVIEW, S.APPROVED,
               S.MERGING, S.DEPLOYING, S.QA_STAGING, S.DONE]
    for origem, destino in zip(caminho, caminho[1:]):
        exige(origem, destino)


def test_transicao_absurda_e_recusada():
    with pytest.raises(TransicaoInvalida):
        exige(S.DISCOVERED, S.DONE)
    with pytest.raises(TransicaoInvalida):
        exige(S.READY, S.MERGING)


def test_terminal_nao_sai():
    assert not pode(S.DONE, S.IMPLEMENTING)
    assert not pode(S.CANCELLED, S.READY)


def test_qa_reprovada_volta_ao_codigo():
    exige(S.QA_STAGING, S.IMPLEMENTING)


def test_escalar_e_sempre_possivel():
    """O motor nunca fica sem a opcao de parar e perguntar."""
    for estado in S:
        if estado in (S.DONE, S.CANCELLED, S.WAITING_HUMAN):
            continue
        assert pode(estado, S.WAITING_HUMAN), estado


def test_waiting_human_volta_para_onde_pausou():
    assert pode(S.WAITING_HUMAN, S.MERGING, pausado_em=S.APPROVED)
    assert pode(S.WAITING_HUMAN, S.APPROVED, pausado_em=S.APPROVED)


def test_humano_nao_teletransporta_task():
    """Aprovar um deploy nao e o mesmo que declarar a task pronta."""
    assert not pode(S.WAITING_HUMAN, S.DONE, pausado_em=S.IMPLEMENTING)
    assert S.DONE not in retomaveis(S.IMPLEMENTING)


# ---- policy --------------------------------------------------------------

REGRAS = [
    {"nome": "ler", "efeito": "ALLOW", "match": {"action": "*.read"}},
    {"nome": "pr", "efeito": "ALLOW", "match": {"action": "repo.pr*"}},
    {"nome": "staging", "efeito": "ALLOW",
     "match": {"action": "repo.merge", "environment": "staging"}},
    {"nome": "prod", "efeito": "HUMAN_APPROVAL",
     "match": {"action": "repo.merge", "environment": "production"}},
    {"nome": "sem_banco", "efeito": "DENY", "match": {"action": "db.write*"}},
]


def motor_policy() -> PolicyEngine:
    return PolicyEngine.de_config(REGRAS)


def ctx(kind: str, ambiente: str = "staging",
        autonomia: AutonomyLevel = AutonomyLevel.L4, recurso: str = "*") -> PolicyContext:
    return PolicyContext(action=Action(kind=kind, resource=recurso, environment=ambiente),
                         autonomy=autonomia)


def test_acao_sem_regra_e_negada():
    d = motor_policy().decide(ctx("cloud.provision"))
    assert d.efeito == Effect.DENY
    assert "nenhuma regra" in d.motivo


def test_leitura_passa():
    assert motor_policy().decide(ctx("repo.read")).permitido


def test_producao_pede_humano():
    d = motor_policy().decide(ctx("repo.merge", "production"))
    assert d.precisa_humano


def test_deny_vence_allow():
    """Uma regra permissiva nao anula uma proibicao."""
    regras = REGRAS + [{"nome": "liberou_tudo", "efeito": "ALLOW", "match": {"action": "*"}}]
    d = PolicyEngine.de_config(regras).decide(ctx("db.write"))
    assert d.efeito == Effect.DENY
    assert d.regra == "sem_banco"


def test_teto_de_autonomia_aperta_allow():
    d = motor_policy().decide(ctx("repo.merge", "staging", autonomia=AutonomyLevel.L2))
    assert d.precisa_humano
    assert d.regra == "teto_de_autonomia"


def test_teto_nao_afrouxa_deny():
    d = motor_policy().decide(ctx("db.write", autonomia=AutonomyLevel.L4))
    assert d.efeito == Effect.DENY


def test_acao_desconhecida_exige_o_nivel_maximo():
    assert nivel_exigido("capacidade.inventada") is AutonomyLevel.L4


# ---- risco ---------------------------------------------------------------

def test_producao_e_alto():
    a = RiskEngine().avalia({"acao": "deploy.staging", "ambiente": "production"})
    assert a.nivel is RiskLevel.HIGH
    assert a.exige_segunda_passada


def test_credencial_e_critico():
    a = RiskEngine().avalia({"acao": "repo.commit", "ambiente": "local",
                             "caminhos": ["src/config/secrets.py"]})
    assert a.nivel is RiskLevel.CRITICAL


def test_mudanca_banal_e_baixo():
    a = RiskEngine().avalia({"acao": "repo.commit", "ambiente": "local",
                             "caminhos": ["README.md"], "linhas": 12})
    assert a.nivel is RiskLevel.LOW
    assert not a.exige_segunda_passada


def test_sinal_carrega_evidencia():
    a = RiskEngine().avalia({"acao": "repo.commit", "ambiente": "local",
                             "caminhos": ["db/migrations/004_add.sql"]})
    assert any("migration" in m for m in a.motivos)
    assert any("004_add.sql" in m for m in a.motivos)


def test_fator_do_cliente_soma_e_nao_substitui():
    r = RiskEngine.de_config([{"nome": "faturamento", "nivel": "HIGH",
                              "campo": "caminhos", "casa": ["*cobranca*"]}])
    assert r.avalia({"caminhos": ["src/cobranca/x.py"]}).nivel is RiskLevel.HIGH
    # o fator da base continua vivo
    assert r.avalia({"ambiente": "production"}).nivel is RiskLevel.HIGH


# ---- grafo ---------------------------------------------------------------

def grafo_diamante() -> DependencyGraph:
    """A e B em paralelo; C depende dos dois. D e E em serie, a parte."""
    g = DependencyGraph()
    g.liga("C", "A")
    g.liga("C", "B")
    g.liga("E", "D")
    return g


def test_paralelo_e_serie_convivem():
    g = grafo_diamante()
    prontas = g.desbloqueadas(set())
    assert prontas == frozenset({"A", "B", "D"})
    assert "C" not in prontas and "E" not in prontas


def test_dependencia_libera_quando_pais_terminam():
    g = grafo_diamante()
    assert "C" not in g.desbloqueadas({"A"})
    assert "C" in g.desbloqueadas({"A", "B"})


def test_camadas_mostram_o_paralelismo():
    assert grafo_diamante().camadas() == [["A", "B", "D"], ["C", "E"]]


def test_ciclo_e_reportado_e_nao_explode():
    g = DependencyGraph()
    g.liga("X", "Y")
    g.liga("Y", "X")
    assert g.ciclos()
    assert g.em_ciclo() == frozenset({"X", "Y"})
    assert not g.desbloqueadas(set()) & {"X", "Y"}


# ---- scheduler -----------------------------------------------------------

def test_despacha_em_paralelo_quando_nao_ha_conflito():
    g = grafo_diamante()
    cands = [Candidata("A", recursos=frozenset({"repo:x"})),
             Candidata("B", recursos=frozenset({"repo:y"})),
             Candidata("D", recursos=frozenset({"repo:z"}))]
    p = planeja(cands, g, set(), {}, Limites(max_workers=3))
    assert set(p.despachar) == {"A", "B", "D"}


def test_nao_paraleliza_quem_toca_o_mesmo_recurso():
    """O caso que produz dois workers na mesma migration."""
    g = DependencyGraph()
    for n in "AB":
        g.adiciona(n)
    cands = [Candidata("A", prioridade=1, recursos=frozenset({"migration:api"})),
             Candidata("B", prioridade=2, recursos=frozenset({"migration:api"}))]
    p = planeja(cands, g, set(), {}, Limites(max_workers=4))
    assert p.despachar == ("A",)
    assert any("recurso ocupado" in a.motivo for a in p.adiadas)


def test_respeita_worker_ja_rodando():
    g = DependencyGraph()
    g.adiciona("B")
    p = planeja([Candidata("B", recursos=frozenset({"repo:x"}))], g, set(),
                em_execucao={"A": frozenset({"repo:x"})}, limites=Limites(max_workers=4))
    assert not p.despachar


def test_teto_de_slots():
    g = DependencyGraph()
    cands = []
    for n in "ABCD":
        g.adiciona(n)
        cands.append(Candidata(n, recursos=frozenset({f"repo:{n}"})))
    assert len(planeja(cands, g, set(), {}, Limites(max_workers=2)).despachar) == 2


def test_teto_diario():
    g = DependencyGraph()
    g.adiciona("A")
    p = planeja([Candidata("A")], g, set(), {}, Limites(max_despachos_dia=8),
                despachos_hoje=8)
    assert not p.despachar
    assert any("teto diario" in a.motivo for a in p.adiadas)


def test_ordem_e_estavel():
    g = DependencyGraph()
    for n in "ABC":
        g.adiciona(n)
    cands = [Candidata("C", prioridade=5, chave="C", recursos=frozenset({"r:c"})),
             Candidata("A", prioridade=1, chave="A", recursos=frozenset({"r:a"})),
             Candidata("B", prioridade=1, chave="B", recursos=frozenset({"r:b"}))]
    p = planeja(cands, g, set(), {}, Limites(max_workers=3))
    assert p.despachar == ("A", "B", "C")


def test_task_em_ciclo_nunca_e_despachada():
    g = DependencyGraph()
    g.liga("X", "Y")
    g.liga("Y", "X")
    p = planeja([Candidata("X"), Candidata("Y")], g, set(), {})
    assert not p.despachar
    assert set(p.em_ciclo) == {"X", "Y"}
