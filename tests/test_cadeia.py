# -*- coding: utf-8 -*-
"""Resolucao de alvo e cadeia de execucao. Puro, sem I/O.

O que estes testes protegem, acima de tudo: **o motor nao adivinha onde uma task
roda.** Ambiguidade e ausencia sao desfechos legitimos, e trocar qualquer um dos
dois por um chute e o defeito mais caro que este elo poderia ter -- porque o
resultado nao seria um erro, seria codigo escrito no repositorio errado.
"""

from __future__ import annotations

import pytest

from regente.core.policy import AutonomyLevel, Effect, PolicyEngine
from regente.core.risk import RiskEngine, RiskLevel
from regente.engine import cadeia
from regente.engine.alvo import Confianca, ResolvedorDeAlvo
from regente.engine.cadeia import Elo
from regente.ports.repository import (LEITURA, Branch, CapacidadeRepo, RepoInfo, RepoRef)
from regente.ports.tasks import SituacaoExterna, ExternalTask

REGRAS = [
    {"nome": "codigo", "efeito": "ALLOW", "match": {"action": "repo.branch*"}},
    {"nome": "nada_em_producao", "efeito": "DENY",
     "match": {"action": "repo.*", "environment": "production"}},
]


def repo(chave: str, base: str = "main", arquivado: bool = False,
         caps: frozenset[CapacidadeRepo] = LEITURA) -> RepoInfo:
    return RepoInfo(ref=RepoRef(provider="p", key=chave),
                    nome=chave.rsplit("/", 1)[-1], branch_base=base,
                    arquivado=arquivado, capacidades=caps)


def task(chave: str, situacao=SituacaoExterna.NAO_INICIADA, rotulos=(), projeto="P") -> ExternalTask:
    return ExternalTask(key=chave, titulo=f"trabalho {chave}", situacao=situacao,
                        estado_externo="TO DO", rotulos=tuple(rotulos), projeto=projeto)


REPOS = [repo("acme/api"), repo("acme/web"), repo("acme/worker")]


# ---------------------------------------------------------------------------
# Resolucao de alvo
# ---------------------------------------------------------------------------

def test_sem_evidencia_e_ausente_e_nao_um_chute():
    a = ResolvedorDeAlvo().resolve(task("K-1"), REPOS)
    assert a.confianca is Confianca.AUSENTE
    assert a.repo is None
    assert not a.candidatos


def test_mapa_por_task_e_declarada():
    r = ResolvedorDeAlvo(por_task={"K-1": "acme/api"})
    a = r.resolve(task("K-1"), REPOS)
    assert a.confianca is Confianca.DECLARADA
    assert a.repo.ref.key == "acme/api"
    assert a.branch_base == "main"


def test_mapa_por_rotulo_e_por_projeto():
    a = ResolvedorDeAlvo(por_rotulo={"backend": "acme/api"}).resolve(
        task("K-1", rotulos=["backend"]), REPOS)
    assert a.repo.ref.key == "acme/api"
    b = ResolvedorDeAlvo(por_projeto={"P": "acme/web"}).resolve(task("K-2"), REPOS)
    assert b.repo.ref.key == "acme/web"


def test_mapa_aceita_nome_curto_quando_nao_ha_ambiguidade():
    a = ResolvedorDeAlvo(por_task={"K-1": "api"}).resolve(task("K-1"), REPOS)
    assert a.repo.ref.key == "acme/api"


def test_nome_curto_ambiguo_nao_entra_no_indice():
    """Dois repositorios chamados `api` em orgs diferentes nao podem ser
    resolvidos por nome curto -- isso seria reintroduzir o chute pela porta
    dos fundos."""
    repos = REPOS + [repo("outra/api")]
    a = ResolvedorDeAlvo(por_task={"K-1": "api"}).resolve(task("K-1"), repos)
    assert a.confianca is Confianca.AUSENTE


def test_branch_existente_e_evidencia_observada():
    a = ResolvedorDeAlvo().resolve(
        task("K-1"), REPOS,
        branches={"acme/api": [Branch(nome="feat/K-1-coisa")]})
    assert a.confianca is Confianca.OBSERVADA
    assert a.repo.ref.key == "acme/api"
    assert "K-1" in a.motivo


def test_branch_casa_por_palavra_inteira():
    """`K-1` nao pode casar com `K-11`: seria trabalho no repositorio errado."""
    a = ResolvedorDeAlvo().resolve(
        task("K-1"), REPOS, branches={"acme/api": [Branch(nome="feat/K-11-outra")]})
    assert a.confianca is Confianca.AUSENTE


def test_declarado_vence_observado():
    """Uma branch pode ser resto de tentativa abandonada; um mapa e afirmacao."""
    a = ResolvedorDeAlvo(por_task={"K-1": "acme/web"}).resolve(
        task("K-1"), REPOS, branches={"acme/api": [Branch(nome="feat/K-1-x")]})
    assert a.confianca is Confianca.DECLARADA
    assert a.repo.ref.key == "acme/web"


def test_empate_e_ambiguo_e_o_motor_nao_desempata():
    a = ResolvedorDeAlvo().resolve(
        task("K-1"), REPOS,
        branches={"acme/api": [Branch(nome="feat/K-1-x")],
                  "acme/web": [Branch(nome="fix/K-1-y")]})
    assert a.confianca is Confianca.AMBIGUA
    assert a.repo is None, "o motor escolheu um dos dois"
    assert len(a.candidatos) == 2


def test_declaracao_desempata_o_ambiguo():
    branches = {"acme/api": [Branch(nome="feat/K-1-x")],
                "acme/web": [Branch(nome="fix/K-1-y")]}
    a = ResolvedorDeAlvo(por_task={"K-1": "acme/api"}).resolve(task("K-1"), REPOS, branches)
    assert a.confianca is Confianca.DECLARADA
    assert a.repo.ref.key == "acme/api"


def test_toda_evidencia_e_auditavel():
    a = ResolvedorDeAlvo(por_rotulo={"backend": "acme/api"}).resolve(
        task("K-1", rotulos=["backend"]), REPOS,
        branches={"acme/api": [Branch(nome="feat/K-1-x")]})
    fontes = {e.fonte for e in a.candidatos[0].evidencias}
    assert fontes == {"mapa:rotulo", "branch"}
    assert all(e.detalhe for e in a.candidatos[0].evidencias)


def test_alvo_que_nao_existe_no_provedor_e_ignorado():
    """Mapa apontando para repositorio inexistente nao pode virar alvo fantasma."""
    a = ResolvedorDeAlvo(por_task={"K-1": "acme/nao-existe"}).resolve(task("K-1"), REPOS)
    assert a.confianca is Confianca.AUSENTE


# ---------------------------------------------------------------------------
# Cadeia
# ---------------------------------------------------------------------------

def monta(tasks, repos=None, resolvedor=None, autonomia=AutonomyLevel.L2,
          branches=None, ambiente="staging"):
    return cadeia.monta(
        workspace_nome="ws", workspace_id="wks_1", tasks=tasks,
        repos=repos if repos is not None else REPOS,
        resolvedor=resolvedor or ResolvedorDeAlvo(),
        policy=PolicyEngine.de_config(REGRAS), risco=RiskEngine(),
        autonomia=autonomia, branches=branches, ambiente=ambiente)


def test_cadeia_completa_produz_candidato():
    r = monta([task("K-1")], resolvedor=ResolvedorDeAlvo(por_task={"K-1": "acme/api"}))
    p = r.passos[0]
    assert p.elo is Elo.CANDIDATO
    assert p.repo.ref.key == "acme/api"
    assert p.branch_base == "main"
    assert p.branch_de_trabalho == "regente/k-1"
    assert p.recursos == ("repo:wks_1/p/acme/api",)
    assert p.decisao.efeito == Effect.ALLOW
    assert r.mutacoes == 0


def test_para_no_primeiro_elo_quando_nao_ha_trabalho():
    r = monta([task("K-1", situacao=SituacaoExterna.EM_EXECUCAO)])
    assert r.passos[0].elo is Elo.SEM_TRABALHO


def test_para_em_sem_alvo():
    assert monta([task("K-1")]).passos[0].elo is Elo.SEM_ALVO


def test_para_em_ambiguo():
    r = monta([task("K-1")],
              branches={"acme/api": [Branch(nome="feat/K-1-x")],
                        "acme/web": [Branch(nome="fix/K-1-y")]})
    assert r.passos[0].elo is Elo.ALVO_AMBIGUO


def test_repositorio_arquivado_nao_recebe_trabalho():
    repos = [repo("acme/api", arquivado=True)]
    r = monta([task("K-1")], repos=repos,
              resolvedor=ResolvedorDeAlvo(por_task={"K-1": "acme/api"}))
    assert r.passos[0].elo is Elo.REPO_INUTILIZAVEL


def test_repositorio_sem_branch_base_nao_recebe_trabalho():
    """Derivar da base errada produz um PR de conflito que ninguem pediu."""
    repos = [repo("acme/api", base="")]
    r = monta([task("K-1")], repos=repos,
              resolvedor=ResolvedorDeAlvo(por_task={"K-1": "acme/api"}))
    assert r.passos[0].elo is Elo.REPO_INUTILIZAVEL


def test_capacidade_ausente_para_a_cadeia_antes_de_gastar_um_ciclo():
    repos = [repo("acme/api", caps=frozenset({CapacidadeRepo.LER_METADADOS}))]
    r = monta([task("K-1")], repos=repos,
              resolvedor=ResolvedorDeAlvo(por_task={"K-1": "acme/api"}))
    p = r.passos[0]
    assert p.elo is Elo.SEM_CAPACIDADE
    assert "clonar" in p.motivo or "ler_arquivos" in p.motivo


def test_teto_de_autonomia_vira_pedido_ao_humano():
    r = monta([task("K-1")], resolvedor=ResolvedorDeAlvo(por_task={"K-1": "acme/api"}),
              autonomia=AutonomyLevel.L0)
    assert r.passos[0].elo is Elo.PRECISA_HUMANO


def test_policy_barra_producao():
    r = monta([task("K-1")], resolvedor=ResolvedorDeAlvo(por_task={"K-1": "acme/api"}),
              ambiente="production")
    p = r.passos[0]
    assert p.elo is Elo.BARRADO_POR_POLICY
    assert p.decisao.regra == "nada_em_producao"


def test_recurso_e_escopado_por_workspace_na_cadeia():
    r1 = monta([task("K-1")], resolvedor=ResolvedorDeAlvo(por_task={"K-1": "acme/api"}))
    r2 = cadeia.monta(
        workspace_nome="ws2", workspace_id="wks_2", tasks=[task("K-1")], repos=REPOS,
        resolvedor=ResolvedorDeAlvo(por_task={"K-1": "acme/api"}),
        policy=PolicyEngine.de_config(REGRAS), risco=RiskEngine(),
        autonomia=AutonomyLevel.L2)
    assert r1.passos[0].recursos != r2.passos[0].recursos


def test_relatorio_conta_onde_a_cadeia_parou():
    r = monta([task("K-1"), task("K-2", situacao=SituacaoExterna.EM_REVISAO),
               task("K-3")],
              resolvedor=ResolvedorDeAlvo(por_task={"K-1": "acme/api"}))
    assert r.por_elo["CANDIDATO"] == 1
    assert r.por_elo["SEM_TRABALHO"] == 1
    assert r.por_elo["SEM_ALVO"] == 1
    assert len(r.candidatos) == 1


def test_texto_do_relatorio_mostra_evidencia():
    r = monta([task("K-1")], resolvedor=ResolvedorDeAlvo(por_task={"K-1": "acme/api"}))
    t = cadeia.texto(r)
    assert "CANDIDATOS A EXECUCAO (1)" in t
    assert "acme/api" in t
    assert "Mutacoes                   0" in t
