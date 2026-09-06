# -*- coding: utf-8 -*-
"""Contrato do TaskProvider. Vale para TODO adapter, presente e futuro.

A suite roda contra cada implementacao registrada em `PROVEDORES`. Adicionar um
provedor novo e adicionar uma linha la -- e se ele nao passar, ele nao esta
pronto, por mais que os testes proprios dele passem.

Por que contrato e nao teste de unidade: teste de unidade prova que o adapter
faz o que o autor dele imaginou. Contrato prova que ele faz o que o **motor
espera** -- que e a unica coisa que impede o Core de precisar saber com qual
provedor esta falando.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from regente.adapters.tasks.filesystem import FilesystemTasks
from regente.adapters.tasks.jira import JiraTasks
from regente.adapters.tasks.transporte import (FalhaDeAutenticacao, LimiteDeTaxa,
                                               NaoEncontrado, ProvedorIndisponivel,
                                               RespostaMalformada, TransporteInstantaneo)
from regente.ports import AdapterErro, SomenteLeitura
from regente.ports.tasks import SituacaoExterna, TaskProvider

INSTANTANEOS = Path(__file__).parent / "instantaneos" / "jira"
CHAVES = json.loads((INSTANTANEOS / "CHAVES.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# As implementacoes sob contrato
# ---------------------------------------------------------------------------

def _monta_filesystem(tmp_path: Path) -> tuple[TaskProvider, str]:
    pasta = tmp_path / "tasks"
    pasta.mkdir()
    corpo = [
        {"key": "K-1", "titulo": "primeira", "estado": "TO DO", "prioridade": 10,
         "descricao": "faz alguma coisa", "labels": ["um"]},
        {"key": "K-2", "titulo": "segunda", "estado": "CODING",
         "depende_de": [{"key": "K-1"}], "descricao": "outra coisa"},
        {"key": "K-3", "titulo": "terceira", "estado": "ESTADO QUE NINGUEM MAPEOU",
         "relacionadas": ["K-1"], "descricao": "mais uma"},
    ]
    for d in corpo:
        (pasta / f"{d['key']}.yaml").write_text(
            yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return FilesystemTasks(pasta), "K-1"


def _monta_jira(tmp_path: Path) -> tuple[TaskProvider, str]:
    return (JiraTasks(transporte=TransporteInstantaneo(diretorio=INSTANTANEOS),
                      site="https://exemplo.atlassian.net"),
            CHAVES["issue_individual"])


PROVEDORES = {"filesystem": _monta_filesystem, "jira": _monta_jira}


@pytest.fixture(params=sorted(PROVEDORES))
def provedor(request, tmp_path):
    porta, chave = PROVEDORES[request.param](tmp_path)
    return porta, chave


# ---------------------------------------------------------------------------
# Identidade e descoberta
# ---------------------------------------------------------------------------

def test_declara_o_que_e(provedor):
    porta, _ = provedor
    d = porta.descreve()
    assert d["capability"] == "tasks"
    assert d["adapter"] and d["adapter"] != "desconhecido"


def test_lista_devolve_tasks(provedor):
    porta, _ = provedor
    tarefas = porta.list_tasks()
    assert tarefas, "list_tasks nao pode devolver vazio quando ha trabalho"
    assert all(t.key for t in tarefas), "toda task precisa de identidade"


def test_identidade_e_unica(provedor):
    porta, _ = provedor
    chaves = [t.key for t in porta.list_tasks()]
    assert len(chaves) == len(set(chaves)), f"chaves duplicadas: {chaves}"


def test_get_devolve_a_mesma_task_que_a_lista(provedor):
    porta, chave = provedor
    um = porta.get_task(chave)
    assert um.key == chave
    da_lista = {t.key: t for t in porta.list_tasks()}
    if chave in da_lista:
        assert da_lista[chave].titulo == um.titulo
        assert da_lista[chave].situacao is um.situacao


def test_task_inexistente_levanta_e_nao_devolve_none(provedor):
    """Ausencia precisa ser erro. `None` silencioso vira 'nao havia trabalho'."""
    porta, _ = provedor
    with pytest.raises(AdapterErro):
        porta.get_task("NAO-EXISTE-999")


# ---------------------------------------------------------------------------
# Normalizacao
# ---------------------------------------------------------------------------

def test_situacao_e_do_vocabulario_do_motor(provedor):
    porta, _ = provedor
    for t in porta.list_tasks():
        assert isinstance(t.situacao, SituacaoExterna)


def test_status_cru_e_preservado(provedor):
    """Sem ele, uma situacao DESCONHECIDA nao diz o que apareceu no board."""
    porta, _ = provedor
    assert any(t.estado_externo for t in porta.list_tasks())


def test_status_desconhecido_nao_e_coagido(provedor):
    """O pecado que este teste impede: mapear o desconhecido para o vizinho."""
    porta, _ = provedor
    desconhecidas = [t for t in porta.list_tasks()
                     if t.situacao is SituacaoExterna.DESCONHECIDA]
    assert desconhecidas, "a fixture precisa conter um status nao mapeado"
    for t in desconhecidas:
        assert t.estado_externo, "DESCONHECIDA sem o status cru e indiagnosticavel"
        assert any("nao mapeado" in a for a in t.anomalias)


def test_prioridade_e_inteiro_comparavel(provedor):
    porta, _ = provedor
    for t in porta.list_tasks():
        assert isinstance(t.prioridade, int)
        assert 0 < t.prioridade <= 1000


def test_vinculo_declara_se_bloqueia(provedor):
    porta, _ = provedor
    for t in porta.list_tasks():
        for v in t.vinculos:
            assert v.key
            assert isinstance(v.bloqueante, bool)


def test_hierarquia_nunca_e_bloqueio(provedor):
    """Subtarefa nao espera a mae. Confundir isso trava um board inteiro."""
    porta, _ = provedor
    for t in porta.list_tasks():
        for v in t.vinculos:
            if v.tipo in ("pai", "filho", "relacionado"):
                assert not v.bloqueante, f"{t.key} -> {v.key} ({v.tipo}) nao pode bloquear"


def test_anomalia_e_reportada_e_nao_corrigida(provedor):
    porta, _ = provedor
    tarefas = porta.list_tasks()
    assert any(t.anomalias for t in tarefas), "a fixture precisa ter dado torto"
    # Dado torto nao derruba a listagem: ele vira relato.
    assert len(tarefas) >= 3


def test_dado_faltante_nao_quebra_a_normalizacao(provedor):
    porta, _ = provedor
    for t in porta.list_tasks():
        assert isinstance(t.titulo, str)
        assert isinstance(t.descricao, str)
        assert isinstance(t.rotulos, tuple)
        assert isinstance(t.recursos, tuple)


# ---------------------------------------------------------------------------
# Shadow mode: o contrato deste marco
# ---------------------------------------------------------------------------

ESCRITAS = ("update_task", "transition_task", "add_comment", "add_label")


@pytest.mark.parametrize("operacao", ESCRITAS)
def test_nenhuma_escrita_e_executada(provedor, operacao):
    """Todo provedor sob shadow precisa RECUSAR escrita, nao ignora-la.

    `filesystem` implementa escrita de verdade e nao esta em shadow -- por isso
    o contrato exige apenas que a operacao seja explicita: ou recusa, ou faz. O
    que nao pode existir e o meio-termo silencioso: aceitar a chamada, nao fazer
    nada e devolver sucesso.
    """
    porta, chave = provedor
    if porta.nome == "filesystem":
        pytest.skip("filesystem nao esta em modo somente-leitura neste marco")
    with pytest.raises(SomenteLeitura):
        getattr(porta, operacao)(chave, "qualquer-coisa")


def test_transporte_de_leitura_nao_tem_verbo_de_escrita():
    """A garantia real do shadow: nao ha funcao que mute o sistema externo."""
    from regente.adapters.tasks import transporte as t
    for classe in (t.TransporteHTTP, t.TransporteInstantaneo):
        metodos = {m for m in dir(classe) if not m.startswith("_")}
        proibidos = metodos & {"post", "put", "patch", "delete", "write", "mutate"}
        assert not proibidos, f"{classe.__name__} expoe escrita: {proibidos}"


# ---------------------------------------------------------------------------
# Paginacao, resiliencia e falhas -- especificos de quem fala com rede
# ---------------------------------------------------------------------------

def _jira(**kw) -> JiraTasks:
    return JiraTasks(transporte=TransporteInstantaneo(diretorio=INSTANTANEOS, **kw),
                     site="https://exemplo.atlassian.net")


def test_paginacao_percorre_todas_as_paginas():
    """Paginar errado devolve a primeira pagina para sempre."""
    porta = _jira()
    tarefas = porta.list_tasks()
    assert len(tarefas) == CHAVES["total"], (
        f"esperava {CHAVES['total']} de duas paginas, vieram {len(tarefas)}")
    assert len({t.key for t in tarefas}) == len(tarefas)


def test_paginacao_respeita_teto_de_paginas():
    porta = _jira()
    porta.max_paginas = 1
    assert len(porta.list_tasks()) == 5, "o teto precisa cortar de verdade"


@pytest.mark.parametrize("erro", [
    ProvedorIndisponivel("timeout apos 30s"),
    ProvedorIndisponivel("falha de conexao: recusada"),
    LimiteDeTaxa("HTTP 429", esperar_segundos=1),
    FalhaDeAutenticacao("HTTP 401"),
    RespostaMalformada("corpo nao e JSON"),
])
def test_falha_do_provedor_sobe_como_erro_de_adapter(erro):
    """O motor nao pode quebrar porque o provedor caiu -- nem confundir queda
    com ausencia de trabalho."""
    porta = _jira(falhas={"search": erro})
    with pytest.raises(AdapterErro):
        porta.list_tasks()


def test_falha_de_autenticacao_nao_e_retentada():
    """Repetir credencial invalida so bloqueia a conta."""
    from regente.adapters.tasks.transporte import TransporteHTTP
    tentativas = []

    def credencial():
        tentativas.append(1)
        raise FalhaDeAutenticacao("credencial ausente")

    t = TransporteHTTP(base_url="https://exemplo.invalido", credencial=credencial,
                       max_tentativas=5)
    with pytest.raises(FalhaDeAutenticacao):
        t.get("/rest/api/3/myself")
    assert len(tentativas) == 1, f"tentou {len(tentativas)}x uma credencial invalida"


def test_instantaneo_ausente_e_nao_encontrado_e_nao_lista_vazia():
    porta = _jira()
    with pytest.raises(NaoEncontrado):
        porta.get_task("SG-NAO-CAPTURADA")


def test_resposta_de_tipo_errado_e_recusada(tmp_path):
    """200 com corpo valido mas de forma errada nao pode virar 'zero tasks'."""
    (tmp_path / "rest_api_3_search_jql.json").write_text("[]", encoding="utf-8")
    porta = JiraTasks(transporte=TransporteInstantaneo(diretorio=tmp_path))
    with pytest.raises(AdapterErro):
        porta.list_tasks()


def test_issue_sem_chave_e_recusada(tmp_path):
    (tmp_path / "rest_api_3_search_jql.json").write_text(
        json.dumps({"issues": [{"id": "1", "fields": {"summary": "sem key"}}]}),
        encoding="utf-8")
    porta = JiraTasks(transporte=TransporteInstantaneo(diretorio=tmp_path))
    with pytest.raises(AdapterErro):
        porta.list_tasks()


# ---------------------------------------------------------------------------
# Observabilidade
# ---------------------------------------------------------------------------

def test_toda_chamada_produz_registro_diagnosticavel():
    vistas = []
    porta = _jira(observador=vistas.append)
    porta.list_tasks()
    assert len(vistas) == 2, "duas paginas, duas chamadas"
    for c in vistas:
        assert c.operacao and c.caminho
        assert c.duracao_ms >= 0
        assert isinstance(c.sucesso, bool)


def test_registro_nao_carrega_corpo_nem_credencial():
    vistas = []
    _jira(observador=vistas.append).list_tasks()
    campos = {f for c in vistas for f in c.__slots__}
    assert not (campos & {"corpo", "body", "credencial", "token", "autorizacao"})
