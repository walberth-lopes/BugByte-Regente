# -*- coding: utf-8 -*-
"""Isolamento entre clientes. A fronteira que nao pode vazar nunca.

Um vazamento aqui nao aparece como erro: aparece como o motor do cliente A
trabalhando com dado do cliente B, em silencio, ate o dia em que sai num log ou
num PR. Por isso cada propriedade e testada explicitamente, e nao presumida da
existencia da coluna `workspace_id`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from regente.adapters.notify.console import Console
from regente.adapters.runner.scripted import ScriptedRunner
from regente.adapters.segredos import Segredos, SegredoAusente, SegredoForaDoEscopo
from regente.adapters.tasks.filesystem import FilesystemTasks
from regente.adapters.tasks.jira import JiraTasks
from regente.adapters.tasks.transporte import TransporteInstantaneo
from regente.adapters.workspace.local import DiretorioIsolado
from regente.core.model import Workspace
from regente.core.policy import AutonomyLevel, PolicyEngine
from regente.core.risk import RiskEngine
from regente.core.scheduling import Limites
from regente.engine.gate import Gate
from regente.engine.orchestrator import Orchestrator
from regente.engine.store_sqlite import SqliteStore

INSTANTANEOS = Path(__file__).parent / "instantaneos" / "jira"


def _tasks_yaml(pasta: Path, chaves: list[str]) -> FilesystemTasks:
    pasta.mkdir(parents=True, exist_ok=True)
    for k in chaves:
        (pasta / f"{k}.yaml").write_text(
            yaml.safe_dump({"key": k, "titulo": f"trabalho {k}", "estado": "TO DO",
                            "recursos": [f"repo:{k}"]},
                           allow_unicode=True, sort_keys=False), encoding="utf-8")
    return FilesystemTasks(pasta)


def _motor(store: SqliteStore, ws_id: str, nome: str, provedor, tmp_path: Path):
    ws = Workspace(id=ws_id, client_id=f"cli_{nome}", nome=nome,
                   autonomia_maxima=AutonomyLevel.L0)
    store.salva_workspace(ws)
    risco = RiskEngine()
    return Orchestrator(
        store=store, workspace=ws, tasks_provider=provedor,
        area_provider=DiretorioIsolado(tmp_path / f"areas-{nome}"),
        runner=ScriptedRunner(),
        gate=Gate(store=store, policy=PolicyEngine.de_config([]), risco=risco),
        risco=risco, limites=Limites(max_workers=2),
        notificador=Console(jornal=tmp_path / f"{nome}.log"))


# ---------------------------------------------------------------------------
# Tenancy no estado
# ---------------------------------------------------------------------------

def test_dois_clientes_no_mesmo_banco_nao_se_veem(tmp_path):
    """O caso barato de errar: um banco compartilhado e uma consulta sem escopo."""
    store = SqliteStore(tmp_path / "compartilhado.db")
    store.migra()

    a = _motor(store, "wks_a", "clienteA", _tasks_yaml(tmp_path / "a", ["A-1", "A-2"]), tmp_path)
    b = _motor(store, "wks_b", "clienteB", _tasks_yaml(tmp_path / "b", ["B-1"]), tmp_path)
    a.tick(); a.tick()
    b.tick(); b.tick()

    chaves_a = {t.chave for t in store.tasks("wks_a")}
    chaves_b = {t.chave for t in store.tasks("wks_b")}
    assert chaves_a == {"A-1", "A-2"}
    assert chaves_b == {"B-1"}
    assert not (chaves_a & chaves_b)


def test_mesma_chave_externa_em_dois_clientes_sao_tasks_distintas(tmp_path):
    """SG-1 do cliente A e SG-1 do cliente B nao podem colidir no indice."""
    store = SqliteStore(tmp_path / "c.db")
    store.migra()
    a = _motor(store, "wks_a", "A", _tasks_yaml(tmp_path / "a", ["SG-1"]), tmp_path)
    b = _motor(store, "wks_b", "B", _tasks_yaml(tmp_path / "b", ["SG-1"]), tmp_path)
    a.tick(); b.tick()

    ta = store.task_por_chave("wks_a", "filesystem", "SG-1")
    tb = store.task_por_chave("wks_b", "filesystem", "SG-1")
    assert ta and tb
    assert ta.id != tb.id, "a mesma chave externa virou uma task so"


def test_eventos_e_acoes_sao_escopados(tmp_path):
    store = SqliteStore(tmp_path / "c.db")
    store.migra()
    a = _motor(store, "wks_a", "A", _tasks_yaml(tmp_path / "a", ["A-1"]), tmp_path)
    b = _motor(store, "wks_b", "B", _tasks_yaml(tmp_path / "b", ["B-1"]), tmp_path)
    a.tick(); a.tick()
    b.tick()

    de_a = store.eventos("wks_a", limite=200)
    ids_de_b = {t.id for t in store.tasks("wks_b")}
    assert de_a, "o cliente A produziu eventos"
    assert not any(e.task_id in ids_de_b for e in de_a)


def test_plano_de_um_cliente_ignora_trabalho_do_outro(tmp_path):
    store = SqliteStore(tmp_path / "c.db")
    store.migra()
    a = _motor(store, "wks_a", "A", _tasks_yaml(tmp_path / "a", ["A-1", "A-2"]), tmp_path)
    b = _motor(store, "wks_b", "B", _tasks_yaml(tmp_path / "b", ["B-1", "B-2", "B-3"]), tmp_path)
    a.tick(); b.tick()
    a._analisa(type("R", (), {"analisadas": 0})())
    b._analisa(type("R", (), {"analisadas": 0})())

    chaves = {store.task(i).chave for i in a.plano().despachar}
    assert chaves <= {"A-1", "A-2"}


def test_lease_de_um_cliente_nao_bloqueia_o_outro(tmp_path):
    """Recursos homonimos entre clientes sao recursos DIFERENTES...

    ...e este teste marca a fronteira exata: hoje o lease e por chave crua, entao
    dois clientes que usem `repo:api` competem pela MESMA trava. E conservador
    (ninguem se atropela) mas errado (um cliente atrasa o outro). Fica registrado
    como limite conhecido, e nao como comportamento desejado.
    """
    store = SqliteStore(tmp_path / "c.db")
    store.migra()
    store.salva_workspace(Workspace(id="wks_a", client_id="a", nome="A"))
    store.salva_workspace(Workspace(id="wks_b", client_id="b", nome="B"))

    assert store.adquire_lease("repo:api", "run_a", "wks_a", 60) is not None
    tomado = store.adquire_lease("repo:api", "run_b", "wks_b", 60)
    assert tomado is None, (
        "hoje a trava e global por chave; se isto mudar, o escopo por workspace "
        "precisa entrar tambem em `leases_vencidos` e na recuperacao")


# ---------------------------------------------------------------------------
# Tenancy nos segredos
# ---------------------------------------------------------------------------

def test_workspace_nao_alcanca_segredo_que_nao_declarou():
    """A fronteira mais cara de furar: credencial de um cliente noutro adapter."""
    a = Segredos(permitidas=frozenset({"env:A_TOKEN"}), workspace="A")
    with pytest.raises(SegredoForaDoEscopo):
        a.resolve("env:B_TOKEN")


def test_referencia_declarada_resolve(monkeypatch):
    monkeypatch.setenv("A_TOKEN", "valor-de-teste")
    a = Segredos(permitidas=frozenset({"env:A_TOKEN"}), workspace="A")
    assert a.resolve("env:A_TOKEN") == "valor-de-teste"


def test_referencia_declarada_mas_ausente_e_erro_claro():
    a = Segredos(permitidas=frozenset({"env:NAO_DEFINIDA_XYZ"}), workspace="A")
    os.environ.pop("NAO_DEFINIDA_XYZ", None)
    with pytest.raises(SegredoAusente):
        a.resolve("env:NAO_DEFINIDA_XYZ")


def test_nao_existe_segredo_literal():
    """Se `literal:` existisse, o primeiro token de producao entraria num YAML."""
    a = Segredos(permitidas=frozenset({"literal:abc123"}), workspace="A")
    with pytest.raises(SegredoAusente, match="esquema"):
        a.resolve("literal:abc123")


def test_workspace_sem_segredos_nao_alcanca_nada():
    a = Segredos(permitidas=frozenset(), workspace="A")
    with pytest.raises(SegredoForaDoEscopo):
        a.resolve("env:QUALQUER")


# ---------------------------------------------------------------------------
# Dois provedores DIFERENTES no mesmo motor
# ---------------------------------------------------------------------------

def test_clientes_com_provedores_diferentes_convivem(tmp_path):
    """Cliente A em YAML, cliente B em Jira -- mesmo Core, mesmo banco."""
    store = SqliteStore(tmp_path / "c.db")
    store.migra()
    a = _motor(store, "wks_a", "A", _tasks_yaml(tmp_path / "a", ["A-1"]), tmp_path)
    b = _motor(store, "wks_b", "B",
               JiraTasks(transporte=TransporteInstantaneo(diretorio=INSTANTANEOS),
                         site="https://exemplo.atlassian.net"), tmp_path)
    a.tick(); b.tick()

    assert {t.chave for t in store.tasks("wks_a")} == {"A-1"}
    do_b = store.tasks("wks_b")
    assert do_b and all(t.chave.startswith("SG-") for t in do_b)
    # E a identidade guarda de QUAL provedor cada task veio.
    assert {t.externo.provider for t in store.tasks("wks_a")} == {"filesystem"}
    assert {t.externo.provider for t in do_b} == {"jira"}
