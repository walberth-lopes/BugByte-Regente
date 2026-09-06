# -*- coding: utf-8 -*-
"""O motor de ponta a ponta, contra os criterios de sucesso do produto.

Cada teste aqui corresponde a uma promessa: descobrir trabalho, montar o grafo,
achar paralelismo, isolar workers, persistir estado, detectar falha, pedir
intervencao e **retomar execucao interrompida**.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from regente.adapters.notify.console import Console
from regente.adapters.runner.scripted import ScriptedRunner
from regente.adapters.tasks.filesystem import FilesystemTasks
from regente.adapters.workspace.local import DiretorioIsolado
from regente.core.model import RunState, Workspace, agora
from regente.core.policy import AutonomyLevel, PolicyEngine
from regente.core.risk import RiskEngine
from regente.core.scheduling import Limites
from regente.core.states import TaskState
from regente.engine.gate import Gate
from regente.engine.orchestrator import Orchestrator
from regente.engine.store_sqlite import SqliteStore
from regente.ports import AdapterErro


def escreve_task(pasta: Path, key: str, **campos) -> None:
    dados = {"key": key, "titulo": campos.pop("titulo", f"trabalho {key}"),
             "estado": campos.pop("estado", "TO DO"), **campos}
    (pasta / f"{key}.yaml").write_text(
        yaml.safe_dump(dados, allow_unicode=True, sort_keys=False), encoding="utf-8")


@pytest.fixture
def bancada(tmp_path):
    """Monta um motor completo em disco temporario."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    def montar(roteiro=None, limites=None, lease_segundos=900):
        store = SqliteStore(tmp_path / "regente.db")
        store.migra()
        ws = Workspace(id="wks_teste", client_id="cli_teste", nome="teste",
                       autonomia_maxima=AutonomyLevel.L3)
        store.salva_workspace(ws)
        risco = RiskEngine()
        orq = Orchestrator(
            store=store, workspace=ws,
            tasks_provider=FilesystemTasks(tasks_dir),
            area_provider=DiretorioIsolado(tmp_path / "areas"),
            runner=ScriptedRunner(roteiro=roteiro or {}),
            gate=Gate(store=store, policy=PolicyEngine.de_config([]), risco=risco),
            risco=risco, limites=limites or Limites(max_workers=3),
            notificador=Console(jornal=tmp_path / "jornal.log"),
            lease_segundos=lease_segundos)
        return orq, store

    montar.tasks = tasks_dir
    montar.raiz = tmp_path
    return montar


# ---- descoberta e baseline ----------------------------------------------

def test_primeira_passada_registra_e_nao_despacha(bancada):
    """Ligar o motor num backlog cheio nao pode virar tempestade de workers."""
    for k in ("A-1", "A-2", "A-3"):
        escreve_task(bancada.tasks, k)
    orq, store = bancada()
    rel = orq.tick()

    assert rel.baseline
    assert rel.descobertas == 3
    assert not rel.despachadas
    assert all(t.estado is TaskState.DISCOVERED for t in store.tasks("wks_teste"))


def test_segundo_tick_analisa_e_despacha(bancada):
    escreve_task(bancada.tasks, "A-1", recursos=["repo:x"])
    orq, store = bancada()
    orq.tick()                      # baseline
    rel = orq.tick()

    assert not rel.baseline
    assert rel.despachadas == ("A-1",)
    assert store.tasks("wks_teste")[0].estado is TaskState.TESTING


def test_task_nova_nao_reabre_baseline(bancada):
    escreve_task(bancada.tasks, "A-1")
    orq, store = bancada()
    orq.tick()
    escreve_task(bancada.tasks, "A-2")
    rel = orq.tick()
    assert not rel.baseline
    assert rel.descobertas == 1


def test_task_nao_duplica_entre_ticks(bancada):
    escreve_task(bancada.tasks, "A-1")
    orq, store = bancada()
    for _ in range(3):
        orq.tick()
    assert len([t for t in store.tasks("wks_teste") if t.chave == "A-1"]) == 1


def test_falha_de_adapter_nao_vira_ausencia_de_trabalho(bancada):
    """O erro precisa aparecer. Board vazio e board quebrado sao coisas diferentes."""
    escreve_task(bancada.tasks, "A-1")
    orq, store = bancada()
    orq.tick()
    (bancada.tasks / "quebrado.yaml").write_text("isto: [nao\n fecha", encoding="utf-8")
    rel = orq.tick()
    assert rel.erros
    assert "adapter" in rel.erros[0]


# ---- grafo e paralelismo -------------------------------------------------

def test_dependencia_declarada_vira_grafo(bancada):
    escreve_task(bancada.tasks, "A-1", recursos=["repo:a"])
    escreve_task(bancada.tasks, "A-2", recursos=["repo:b"])
    escreve_task(bancada.tasks, "A-3", recursos=["repo:c"],
                 depende_de=[{"key": "A-1"}, {"key": "A-2"}])
    orq, store = bancada()
    orq.tick()

    plano = orq.plano()  # ainda em DISCOVERED: nada pronto
    assert not plano.despachar

    orq._analisa(type("R", (), {"analisadas": 0})())
    plano = orq.plano()
    chaves = {store.task(i).chave for i in plano.despachar}
    assert chaves == {"A-1", "A-2"}, "A-3 depende das outras duas"


def test_recurso_compartilhado_serializa(bancada):
    """Duas tasks na mesma migration nao podem sair no mesmo tick."""
    escreve_task(bancada.tasks, "A-1", recursos=["migration:api"], prioridade=1)
    escreve_task(bancada.tasks, "A-2", recursos=["migration:api"], prioridade=2)
    orq, store = bancada(limites=Limites(max_workers=4))
    orq.tick()
    orq._analisa(type("R", (), {"analisadas": 0})())

    plano = orq.plano()
    assert len(plano.despachar) == 1
    assert store.task(plano.despachar[0]).chave == "A-1"
    assert any("recurso ocupado" in a.motivo for a in plano.adiadas)


def test_ciclo_escala_em_vez_de_travar(bancada):
    escreve_task(bancada.tasks, "A-1", depende_de=[{"key": "A-2"}])
    escreve_task(bancada.tasks, "A-2", depende_de=[{"key": "A-1"}])
    orq, store = bancada()
    orq.tick()
    rel = orq.tick()

    assert set(rel.ciclos) == {"A-1", "A-2"}
    assert not rel.despachadas
    abertos = store.approvals_abertos("wks_teste")
    assert len(abertos) == 1
    assert "circular" in abertos[0].o_que_aconteceu


def test_ciclo_nao_repergunta_a_cada_tick(bancada):
    escreve_task(bancada.tasks, "A-1", depende_de=[{"key": "A-2"}])
    escreve_task(bancada.tasks, "A-2", depende_de=[{"key": "A-1"}])
    orq, store = bancada()
    for _ in range(4):
        orq.tick()
    assert len(store.approvals_abertos("wks_teste")) == 1


# ---- isolamento ----------------------------------------------------------

def test_cada_worker_tem_area_propria(bancada):
    escreve_task(bancada.tasks, "A-1", recursos=["repo:a"])
    escreve_task(bancada.tasks, "A-2", recursos=["repo:b"])
    orq, store = bancada()
    orq.tick()
    orq.tick()

    areas = {r.workspace_path for r in
             [x for t in store.tasks("wks_teste") for x in store.runs_da_task(t.id)]}
    assert len(areas) == 2
    for a in areas:
        assert (Path(a) / "run.json").is_file()


# ---- persistencia e retomada --------------------------------------------

def test_estado_sobrevive_ao_processo(bancada):
    escreve_task(bancada.tasks, "A-1", recursos=["repo:a"])
    orq, store = bancada()
    orq.tick()
    orq.tick()
    store.fecha()

    orq2, store2 = bancada()          # processo novo, mesmo banco
    t = store2.tasks("wks_teste")[0]
    assert t.chave == "A-1"
    assert t.estado is TaskState.TESTING


def test_worker_morto_devolve_a_task_para_a_fila(bancada):
    """O criterio de morte e o lease vencido, nao a ausencia de processo."""
    escreve_task(bancada.tasks, "A-1", recursos=["repo:a"])
    orq, store = bancada(roteiro={"A-1": {"ok": True, "resumo": "feito"}})
    orq.tick()

    # Simula um worker que travou: run vivo, lease ja vencido.
    orq._analisa(type("R", (), {"analisadas": 0})())
    task = store.tasks("wks_teste")[0]
    store.transiciona(task.id, TaskState.ASSIGNED, ator="teste")
    from regente.core.ids import RUN, novo
    from regente.core.model import Run
    run = Run(id=novo(RUN), task_id=task.id, workspace_id="wks_teste", agente="coder")
    store.salva_run(run)
    store.adquire_lease("repo:a", run.id, "wks_teste", segundos=1)
    store._con.execute("UPDATE leases SET expira_em=? WHERE recurso=?",
                       ("2000-01-01T00:00:00.000000Z", "repo:a"))

    rel = orq.tick()
    assert rel.recuperadas == ("A-1",)
    assert store.run(run.id).estado is RunState.INTERRUPTED
    # Recuperar e devolver a fila, e a fila anda no mesmo tick: o trabalho volta
    # a andar sozinho, sem esperar o proximo ciclo nem intervencao.
    assert rel.despachadas == ("A-1",)
    assert store.task(task.id).estado is TaskState.TESTING
    assert store.adquire_lease("repo:a", "outro", "wks_teste", 60) is not None, \
        "o lease do worker morto precisa ter sido solto"


def test_lease_impede_dois_donos(bancada):
    orq, store = bancada()
    assert store.adquire_lease("repo:a", "run_1", "wks_teste", 60) is not None
    assert store.adquire_lease("repo:a", "run_2", "wks_teste", 60) is None
    store.solta_lease("repo:a", "run_1")
    assert store.adquire_lease("repo:a", "run_2", "wks_teste", 60) is not None


def test_lease_vencido_pode_ser_tomado(bancada):
    orq, store = bancada()
    store.adquire_lease("repo:a", "run_1", "wks_teste", 60)
    store._con.execute("UPDATE leases SET expira_em=? WHERE recurso=?",
                       ("2000-01-01T00:00:00.000000Z", "repo:a"))
    assert store.adquire_lease("repo:a", "run_2", "wks_teste", 60) is not None


# ---- falha, escada e escalonamento ---------------------------------------

def test_primeira_falha_retenta_sem_incomodar_o_dono(bancada):
    escreve_task(bancada.tasks, "A-1", recursos=["repo:a"])
    orq, store = bancada(roteiro={"A-1": {"ok": False, "resumo": "teste vermelho"}})
    orq.tick()
    rel = orq.tick()

    task = store.tasks("wks_teste")[0]
    assert task.estado is TaskState.READY
    assert task.tentativas == 1
    assert not rel.escalonadas, "falha unica e trabalho, nao pergunta"


def test_escada_acaba_e_escala(bancada):
    escreve_task(bancada.tasks, "A-1", recursos=["repo:a"])
    orq, store = bancada(roteiro={"A-1": {"ok": False, "resumo": "mesmo erro"}})
    for _ in range(5):
        orq.tick()

    task = store.tasks("wks_teste")[0]
    assert task.estado is TaskState.WAITING_HUMAN
    abertos = store.approvals_abertos("wks_teste")
    assert len(abertos) == 1
    assert abertos[0].por_que_importa
    assert abertos[0].o_que_o_agente_tentou


def test_worker_pode_pedir_decisao_humana(bancada):
    escreve_task(bancada.tasks, "A-1", recursos=["repo:a"])
    orq, store = bancada(roteiro={"A-1": {
        "ok": False, "desfecho": "precisa_humano",
        "resumo": "contrato da API publica e ambiguo",
        "pergunta": {"o_que_aconteceu": "contrato da API publica e ambiguo",
                     "por_que_importa": "escolher errado quebra cliente em producao",
                     "tentativas": ["li os dois consumidores", "procurei ADR"],
                     "recomendacao": "seguir"}}})
    orq.tick()
    rel = orq.tick()

    assert rel.escalonadas == ("A-1",)
    task = store.tasks("wks_teste")[0]
    assert task.estado is TaskState.WAITING_HUMAN
    assert task.pausada_em is TaskState.IMPLEMENTING

    a = store.approvals_abertos("wks_teste")[0]
    assert a.recomendacao == "seguir"
    assert len(a.opcoes) >= 3


def test_decisao_humana_e_registrada_e_retoma(bancada):
    escreve_task(bancada.tasks, "A-1", recursos=["repo:a"])
    orq, store = bancada(roteiro={"A-1": {
        "ok": False, "desfecho": "precisa_humano", "resumo": "ambiguo",
        "pergunta": {"por_que_importa": "afeta contrato publico"}}})
    orq.tick()
    orq.tick()

    a = store.approvals_abertos("wks_teste")[0]
    decidido = store.decide_approval(a.id, "seguir", por="walberth", nota="manter compat")
    assert decidido.escolha == "seguir"
    assert not store.approvals_abertos("wks_teste")

    task = store.task(a.task_id)
    store.transiciona(task.id, TaskState.IMPLEMENTING, ator="walberth", motivo="decidido")
    assert store.task(task.id).estado is TaskState.IMPLEMENTING


def test_escolha_fora_das_opcoes_e_recusada(bancada):
    escreve_task(bancada.tasks, "A-1", recursos=["repo:a"])
    orq, store = bancada(roteiro={"A-1": {
        "ok": False, "desfecho": "precisa_humano", "resumo": "x",
        "pergunta": {"por_que_importa": "y"}}})
    orq.tick()
    orq.tick()
    a = store.approvals_abertos("wks_teste")[0]
    with pytest.raises(Exception):
        store.decide_approval(a.id, "opcao_inventada", por="walberth")


def test_worker_que_explode_nao_derruba_o_tick(bancada):
    class Explode(ScriptedRunner):
        def run(self, pedido):
            raise RuntimeError("estourou")

    escreve_task(bancada.tasks, "A-1", recursos=["repo:a"])
    orq, store = bancada()
    orq.runner = Explode()
    orq.tick()
    rel = orq.tick()

    assert store.tasks("wks_teste")[0].estado is TaskState.READY
    assert not rel.erros, "excecao do worker vira falha da task, nao erro do tick"


# ---- trilha --------------------------------------------------------------

def test_timeline_conta_a_historia(bancada):
    escreve_task(bancada.tasks, "A-1", recursos=["repo:a"])
    orq, store = bancada()
    orq.tick()
    orq.tick()

    task = store.tasks("wks_teste")[0]
    tipos = [e.tipo for e in store.eventos("wks_teste", task_id=task.id, limite=50)]
    assert "descoberta" in tipos
    assert "despachada" in tipos
    assert tipos.count("transicao") >= 4


def test_toda_transicao_deixa_rastro(bancada):
    escreve_task(bancada.tasks, "A-1")
    orq, store = bancada()
    orq.tick()
    task = store.tasks("wks_teste")[0]
    store.transiciona(task.id, TaskState.ANALYZING, ator="teste", motivo="porque sim")
    evento = store.eventos("wks_teste", task_id=task.id, limite=1)[0]
    assert evento.tipo == "transicao"
    assert evento.dados["motivo"] == "porque sim"
    assert evento.ator == "teste"


def test_task_recuperada_volta_a_ser_agendavel(bancada):
    """O buraco que quase passou: recuperar mudando o rotulo, sem devolver a fila.

    Uma task que volta do crash para um estado ATIVO nao e despachada por
    ninguem -- fica viva no papel e parada de verdade, sem erro que acuse.
    """
    from regente.core.states import ATIVOS
    from regente.engine.supervisor import estado_de_retomada

    for estado in ATIVOS:
        assert estado_de_retomada(estado) is TaskState.READY, estado


def test_area_de_trabalho_e_da_task_e_sobrevive_a_retomada(bancada):
    """O WIP da tentativa anterior precisa estar la quando o worker volta."""
    escreve_task(bancada.tasks, "A-1", recursos=["repo:a"])
    orq, store = bancada(roteiro={"A-1": {"ok": False, "resumo": "caiu"}})
    orq.tick()
    orq.tick()

    task = store.tasks("wks_teste")[0]
    primeira = Path(store.runs_da_task(task.id)[0].workspace_path)
    (primeira / "wip.txt").write_text("commit pela metade", encoding="utf-8")

    orq.tick()   # segunda tentativa
    runs = store.runs_da_task(task.id)
    assert len(runs) == 2
    assert runs[1].workspace_path == str(primeira), "a retomada abriu area nova"
    assert (Path(runs[1].workspace_path) / "wip.txt").is_file(), "o WIP foi jogado fora"


# ---- relevancia: o que a ORIGEM diz sobre o trabalho ---------------------
# Os dois testes abaixo travam defeitos encontrados rodando contra um board
# real. Nenhum dado inventado os teria revelado: eles so aparecem quando o
# provedor descreve trabalho que ja tem gente nele.

def test_nao_despacha_trabalho_que_ja_tem_alguem(bancada):
    """Um agente por cima de uma pessoa e o pior desfecho possivel.

    Medido contra o board real: duas issues em CODING foram despachadas no
    primeiro tick antes desta guarda existir.
    """
    escreve_task(bancada.tasks, "A-1", estado="TO DO", recursos=["repo:a"])
    escreve_task(bancada.tasks, "A-2", estado="CODING", recursos=["repo:b"])
    escreve_task(bancada.tasks, "A-3", estado="REVIEWING", recursos=["repo:c"])
    orq, store = bancada()
    orq.tick()
    rel = orq.tick()

    assert rel.despachadas == ("A-1",)
    porchave = {t.chave: t for t in store.tasks("wks_teste")}
    assert porchave["A-2"].estado is TaskState.BLOCKED
    assert porchave["A-3"].estado is TaskState.BLOCKED
    assert porchave["A-2"].dados["bloqueada_por"] == "origem"


def test_situacao_desconhecida_nao_e_despachada(bancada):
    """Nao saber se alguem esta na task custa um adiamento, nunca um atropelo."""
    escreve_task(bancada.tasks, "A-9", estado="AGUARDANDO JURIDICO", recursos=["repo:x"])
    orq, store = bancada()
    orq.tick()
    rel = orq.tick()
    assert not rel.despachadas
    assert store.tasks("wks_teste")[0].estado is TaskState.BLOCKED
    assert rel.anomalias and "nao mapeado" in rel.anomalias[0]


def test_mudanca_na_origem_libera_o_trabalho(bancada):
    """A pessoa devolveu a task ao board; o motor precisa notar sozinho."""
    escreve_task(bancada.tasks, "A-1", estado="REVIEWING", recursos=["repo:a"])
    orq, store = bancada()
    orq.tick(); orq.tick()
    assert store.tasks("wks_teste")[0].estado is TaskState.BLOCKED

    escreve_task(bancada.tasks, "A-1", estado="TO DO", recursos=["repo:a"])
    rel = orq.tick()
    assert rel.mudancas and rel.mudancas[0][0] == "A-1"
    assert rel.liberadas == ("A-1",)
    assert store.tasks("wks_teste")[0].estado is TaskState.TESTING


def test_bloqueio_por_falha_nao_e_desfeito_por_status_externo(bancada):
    """So quem foi bloqueado PELA ORIGEM volta por mudanca da origem."""
    escreve_task(bancada.tasks, "A-1", estado="TO DO", recursos=["repo:a"])
    orq, store = bancada()
    orq.tick(); orq.tick()
    task = store.tasks("wks_teste")[0]
    store.transiciona(task.id, TaskState.BLOCKED, ator="humano", motivo="parei na mao")

    orq.tick()
    assert store.task(task.id).estado is TaskState.BLOCKED, (
        "bloqueio humano nao pode ser desfeito por status de board")


def test_trabalho_encerrado_na_origem_nao_entra(bancada):
    escreve_task(bancada.tasks, "A-1", estado="TO DO", recursos=["repo:a"])
    escreve_task(bancada.tasks, "A-2", estado="DONE")
    orq, store = bancada()
    rel = orq.tick()
    assert rel.descobertas == 1
    assert {t.chave for t in store.tasks("wks_teste")} == {"A-1"}


def test_hierarquia_nao_vira_dependencia(bancada):
    """95 de 100 issues do board real tinham mae. Se hierarquia bloqueasse,
    o motor nao despacharia nada."""
    escreve_task(bancada.tasks, "MAE-1", estado="TO DO", recursos=["repo:m"])
    escreve_task(bancada.tasks, "F-1", estado="TO DO", recursos=["repo:a"],
                 relacionadas=["MAE-1"])
    escreve_task(bancada.tasks, "F-2", estado="TO DO", recursos=["repo:b"],
                 relacionadas=["MAE-1"])
    orq, store = bancada(limites=Limites(max_workers=3))
    orq.tick()
    rel = orq.tick()
    assert set(rel.despachadas) == {"MAE-1", "F-1", "F-2"}
    assert not store.dependencias("wks_teste"), "relacionamento virou aresta"
