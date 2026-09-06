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
from regente.adapters.tasks.transport import (AuthFailure, RateLimited,
                                               NotFound, ProviderUnavailable,
                                               MalformedResponse, SnapshotTransport)
from regente.ports import AdapterError, ReadOnlyRefused
from regente.ports.tasks import ExternalStatus, TaskProvider

SNAPSHOTS = Path(__file__).parent / "snapshots" / "jira"
KEYS = json.loads((SNAPSHOTS / "KEYS.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# As implementacoes sob contrato
# ---------------------------------------------------------------------------

def _monta_filesystem(tmp_path: Path) -> tuple[TaskProvider, str]:
    pasta = tmp_path / "tasks"
    pasta.mkdir()
    body = [
        {"key": "K-1", "titulo": "primeira", "estado": "TO DO", "prioridade": 10,
         "descricao": "faz alguma coisa", "labels": ["um"]},
        {"key": "K-2", "titulo": "segunda", "estado": "CODING",
         "depende_de": [{"key": "K-1"}], "descricao": "outra coisa"},
        {"key": "K-3", "titulo": "terceira", "estado": "ESTADO QUE NINGUEM MAPEOU",
         "relacionadas": ["K-1"], "descricao": "mais uma"},
    ]
    for d in body:
        (pasta / f"{d['key']}.yaml").write_text(
            yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return FilesystemTasks(pasta), "K-1"


def _monta_jira(tmp_path: Path) -> tuple[TaskProvider, str]:
    return (JiraTasks(transport=SnapshotTransport(directory=SNAPSHOTS),
                      site="https://exemplo.atlassian.net"),
            KEYS["issue_individual"])


PROVIDERS = {"filesystem": _monta_filesystem, "jira": _monta_jira}


@pytest.fixture(params=sorted(PROVIDERS))
def provider(request, tmp_path):
    port, key = PROVIDERS[request.param](tmp_path)
    return port, key


# ---------------------------------------------------------------------------
# Identidade e descoberta
# ---------------------------------------------------------------------------

def test_declares_the_that_is(provider):
    port, _ = provider
    d = port.describe()
    assert d["capability"] == "tasks"
    assert d["adapter"] and d["adapter"] != "desconhecido"


def test_list_returns_tasks(provider):
    port, _ = provider
    items = port.list_tasks()
    assert items, "list_tasks nao pode devolver vazio quando ha trabalho"
    assert all(t.key for t in items), "toda task precisa de identidade"


def test_identity_is_unique(provider):
    port, _ = provider
    chaves = [t.key for t in port.list_tasks()]
    assert len(chaves) == len(set(chaves)), f"chaves duplicadas: {chaves}"


def test_get_returns_the_same_task_that_the_list(provider):
    port, key = provider
    um = port.get_task(key)
    assert um.key == key
    da_lista = {t.key: t for t in port.list_tasks()}
    if key in da_lista:
        assert da_lista[key].title == um.title
        assert da_lista[key].status is um.status


def test_task_missing_raises_is_not_returns_none(provider):
    """Ausencia precisa ser error. `None` silencioso vira 'nao havia trabalho'."""
    port, _ = provider
    with pytest.raises(AdapterError):
        port.get_task("NAO-EXISTE-999")


# ---------------------------------------------------------------------------
# Normalizacao
# ---------------------------------------------------------------------------

def test_status_is_of_vocabulario_of_motor(provider):
    port, _ = provider
    for t in port.list_tasks():
        assert isinstance(t.status, ExternalStatus)


def test_status_raw_is_preserved(provider):
    """Sem ele, uma situacao DESCONHECIDA nao diz o que apareceu no board."""
    port, _ = provider
    assert any(t.external_status for t in port.list_tasks())


def test_status_desconhecido_not_is_coerced(provider):
    """O pecado que este teste impede: mapear o desconhecido para o vizinho."""
    port, _ = provider
    desconhecidas = [t for t in port.list_tasks()
                     if t.status is ExternalStatus.UNKNOWN]
    assert desconhecidas, "a fixture precisa conter um status nao mapeado"
    for t in desconhecidas:
        assert t.external_status, "DESCONHECIDA sem o status cru e indiagnosticavel"
        assert any("nao mapeado" in a for a in t.anomalies)


def test_priority_is_integer_comparable(provider):
    port, _ = provider
    for t in port.list_tasks():
        assert isinstance(t.priority, int)
        assert 0 < t.priority <= 1000


def test_link_declares_if_blocks(provider):
    port, _ = provider
    for t in port.list_tasks():
        for v in t.links:
            assert v.key
            assert isinstance(v.blocking, bool)


def test_hierarchy_never_is_block(provider):
    """Subtarefa nao espera a mae. Confundir isso trava um board inteiro."""
    port, _ = provider
    for t in port.list_tasks():
        for v in t.links:
            if v.kind in ("parent", "child", "related"):
                assert not v.blocking, f"{t.key} -> {v.key} ({v.kind}) nao pode bloquear"


def test_anomaly_is_reportada_is_not_fixed(provider):
    port, _ = provider
    items = port.list_tasks()
    assert any(t.anomalies for t in items), "a fixture precisa ter dado torto"
    # Dado torto nao derruba a listagem: ele vira relato.
    assert len(items) >= 3


def test_data_missing_not_break_the_normalization(provider):
    port, _ = provider
    for t in port.list_tasks():
        assert isinstance(t.title, str)
        assert isinstance(t.description, str)
        assert isinstance(t.labels, tuple)
        assert isinstance(t.resources, tuple)


# ---------------------------------------------------------------------------
# Shadow mode: o contrato deste marco
# ---------------------------------------------------------------------------

WRITE_OPS = ("update_task", "transition_task", "add_comment", "add_label")


@pytest.mark.parametrize("operation", WRITE_OPS)
def test_nenhuma_write_is_executed(provider, operation):
    """Todo provedor sob shadow precisa RECUSAR escrita, nao ignora-la.

    `filesystem` implementa escrita de verdade e nao esta em shadow -- por isso
    o contrato exige apenas que a operacao seja explicita: ou recusa, ou faz. O
    que nao pode existir e o meio-termo silencioso: aceitar a chamada, nao fazer
    nada e devolver success.
    """
    port, key = provider
    if port.name == "filesystem":
        pytest.skip("filesystem nao esta em modo somente-leitura neste marco")
    with pytest.raises(ReadOnlyRefused):
        getattr(port, operation)(key, "qualquer-coisa")


def test_transporte_of_read_not_tem_verb_of_write():
    """A garantia real do shadow: nao ha funcao que mute o sistema externo."""
    from regente.adapters.tasks import transport as t
    for classe in (t.HttpTransport, t.SnapshotTransport):
        metodos = {m for m in dir(classe) if not m.startswith("_")}
        proibidos = metodos & {"post", "put", "patch", "delete", "write", "mutate"}
        assert not proibidos, f"{classe.__name__} expoe escrita: {proibidos}"


# ---------------------------------------------------------------------------
# Paginacao, resiliencia e falhas -- especificos de quem fala com rede
# ---------------------------------------------------------------------------

def _jira(**kw) -> JiraTasks:
    return JiraTasks(transport=SnapshotTransport(directory=SNAPSHOTS, **kw),
                     site="https://exemplo.atlassian.net")


def test_pagination_walks_all_the_pages():
    """Paginar errado devolve a primeira pagina para sempre."""
    port = _jira()
    items = port.list_tasks()
    assert len(items) == KEYS["total"], (
        f"esperava {KEYS['total']} de duas paginas, vieram {len(items)}")
    assert len({t.key for t in items}) == len(items)


def test_pagination_respects_ceiling_of_pages():
    port = _jira()
    port.max_pages = 1
    assert len(port.list_tasks()) == 5, "o teto precisa cortar de verdade"


@pytest.mark.parametrize("error", [
    ProviderUnavailable("timeout apos 30s"),
    ProviderUnavailable("falha de conexao: recusada"),
    RateLimited("HTTP 429", retry_after_seconds=1),
    AuthFailure("HTTP 401"),
    MalformedResponse("corpo nao e JSON"),
])
def test_failure_of_provider_sobe_como_error_of_adapter(error):
    """O motor nao pode quebrar porque o provedor caiu -- nem confundir queda
    com ausencia de trabalho."""
    port = _jira(failures={"search": error})
    with pytest.raises(AdapterError):
        port.list_tasks()


def test_failure_of_authentication_not_is_retried():
    """Repetir credencial invalida so bloqueia a conta."""
    from regente.adapters.tasks.transport import HttpTransport
    attempts = []

    def credencial():
        attempts.append(1)
        raise AuthFailure("credencial ausente")

    t = HttpTransport(base_url="https://exemplo.invalido", credencial=credencial,
                       max_attempts=5)
    with pytest.raises(AuthFailure):
        t.get("/rest/api/3/myself")
    assert len(attempts) == 1, f"tentou {len(attempts)}x uma credencial invalida"


def test_snapshot_missing_is_not_found_is_not_list_empty():
    port = _jira()
    with pytest.raises(NotFound):
        port.get_task("SG-NAO-CAPTURADA")


def test_response_of_kind_wrong_is_refused(tmp_path):
    """200 com corpo valido mas de forma errada nao pode virar 'zero tasks'."""
    (tmp_path / "rest_api_3_search_jql.json").write_text("[]", encoding="utf-8")
    port = JiraTasks(transport=SnapshotTransport(directory=tmp_path))
    with pytest.raises(AdapterError):
        port.list_tasks()


def test_issue_without_key_is_refused(tmp_path):
    (tmp_path / "rest_api_3_search_jql.json").write_text(
        json.dumps({"issues": [{"id": "1", "fields": {"summary": "sem key"}}]}),
        encoding="utf-8")
    port = JiraTasks(transport=SnapshotTransport(directory=tmp_path))
    with pytest.raises(AdapterError):
        port.list_tasks()


# ---------------------------------------------------------------------------
# Observabilidade
# ---------------------------------------------------------------------------

def test_every_call_produces_registry_diagnosable():
    vistas = []
    port = _jira(observer=vistas.append)
    port.list_tasks()
    assert len(vistas) == 2, "duas paginas, duas chamadas"
    for c in vistas:
        assert c.operation and c.path
        assert c.duration_ms >= 0
        assert isinstance(c.success, bool)


def test_registry_not_carries_body_nor_credential():
    vistas = []
    _jira(observer=vistas.append).list_tasks()
    campos = {f for c in vistas for f in c.__slots__}
    assert not (campos & {"corpo", "body", "credencial", "token", "autorizacao"})
