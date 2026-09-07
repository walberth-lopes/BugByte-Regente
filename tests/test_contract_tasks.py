# -*- coding: utf-8 -*-
"""The TaskProvider contract. It holds for EVERY adapter, present and future.

The suite runs against each implementation registered in `PROVEDORES`. Adding a
new provider means adding a line there -- and if it does not pass, it is not
ready, however well its own tests do.

Why a contract and not a unit test: a unit test proves the adapter does what its
author imagined. A contract proves it does what the **engine expects** -- which
is the only thing that keeps the Core from needing to know which provider it is
talking to.
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
# The implementations under contract
# ---------------------------------------------------------------------------

def _build_filesystem(tmp_path: Path) -> tuple[TaskProvider, str]:
    folder = tmp_path / "tasks"
    folder.mkdir()
    body = [
        {"key": "K-1", "titulo": "primeira", "estado": "TO DO", "prioridade": 10,
         "descricao": "faz alguma coisa", "labels": ["um"]},
        {"key": "K-2", "titulo": "segunda", "estado": "CODING",
         "depende_de": [{"key": "K-1"}], "descricao": "outra coisa"},
        {"key": "K-3", "titulo": "terceira", "estado": "ESTADO QUE NINGUEM MAPEOU",
         "relacionadas": ["K-1"], "descricao": "mais uma"},
    ]
    for d in body:
        (folder / f"{d['key']}.yaml").write_text(
            yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return FilesystemTasks(folder), "K-1"


def _build_jira(tmp_path: Path) -> tuple[TaskProvider, str]:
    return (JiraTasks(transport=SnapshotTransport(directory=SNAPSHOTS),
                      site="https://exemplo.atlassian.net"),
            KEYS["issue_individual"])


PROVIDERS = {"filesystem": _build_filesystem, "jira": _build_jira}


@pytest.fixture(params=sorted(PROVIDERS))
def provider(request, tmp_path):
    port, key = PROVIDERS[request.param](tmp_path)
    return port, key


# ---------------------------------------------------------------------------
# Identity and discovery
# ---------------------------------------------------------------------------

def test_declares_the_that_is(provider):
    port, _ = provider
    d = port.describe()
    assert d["capability"] == "tasks"
    assert d["adapter"] and d["adapter"] != "unknown"


def test_list_returns_tasks(provider):
    port, _ = provider
    items = port.list_tasks()
    assert items, "list_tasks nao pode devolver vazio quando ha trabalho"
    assert all(t.key for t in items), "toda task precisa de identidade"


def test_identity_is_unique(provider):
    port, _ = provider
    keys = [t.key for t in port.list_tasks()]
    assert len(keys) == len(set(keys)), f"duplicate keys: {keys}"


def test_get_returns_the_same_task_that_the_list(provider):
    port, key = provider
    um = port.get_task(key)
    assert um.key == key
    from_list = {t.key: t for t in port.list_tasks()}
    if key in from_list:
        assert from_list[key].title == um.title
        assert from_list[key].status is um.status


def test_task_missing_raises_is_not_returns_none(provider):
    """Absence has to be an error. A silent `None` becomes 'there was no work'."""
    port, _ = provider
    with pytest.raises(AdapterError):
        port.get_task("NAO-EXISTE-999")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def test_status_is_of_vocabulario_of_motor(provider):
    port, _ = provider
    for t in port.list_tasks():
        assert isinstance(t.status, ExternalStatus)


def test_status_raw_is_preserved(provider):
    """Without it, a DESCONHECIDA status does not say what showed up on the board."""
    port, _ = provider
    assert any(t.external_status for t in port.list_tasks())


def test_status_desconhecido_not_is_coerced(provider):
    """The sin this test prevents: mapping the unknown onto its neighbour."""
    port, _ = provider
    unmapped = [t for t in port.list_tasks()
                     if t.status is ExternalStatus.UNKNOWN]
    assert unmapped, "a fixture precisa conter um status nao mapeado"
    for t in unmapped:
        assert t.external_status, "DESCONHECIDA sem o status cru e indiagnosticavel"
        assert any("unmapped status" in a for a in t.anomalies)


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
    """A subtask does not wait for its parent. Confusing this locks a whole board."""
    port, _ = provider
    for t in port.list_tasks():
        for v in t.links:
            if v.kind in ("parent", "child", "related"):
                assert not v.blocking, f"{t.key} -> {v.key} ({v.kind}) nao pode bloquear"


def test_anomaly_is_reportada_is_not_fixed(provider):
    port, _ = provider
    items = port.list_tasks()
    assert any(t.anomalies for t in items), "a fixture precisa ter dado torto"
    # Crooked data does not bring the listing down: it becomes a report.
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
    """Every provider under shadow must REFUSE a write, not ignore it.

    `filesystem` implements real writing and is not under shadow -- which is why
    the contract only requires the operation to be explicit: either it refuses,
    or it does it. What must not exist is the silent middle ground: accepting the
    call, doing nothing and returning success.
    """
    port, key = provider
    if port.name == "filesystem":
        pytest.skip("filesystem nao esta em modo somente-leitura neste marco")
    with pytest.raises(ReadOnlyRefused):
        getattr(port, operation)(key, "qualquer-coisa")


def test_transporte_of_read_not_tem_verb_of_write():
    """The real guarantee of the shadow: no function mutates the external system."""
    from regente.adapters.tasks import transport as t
    for classe in (t.HttpTransport, t.SnapshotTransport):
        metodos = {m for m in dir(classe) if not m.startswith("_")}
        proibidos = metodos & {"post", "put", "patch", "delete", "write", "mutate"}
        assert not proibidos, f"{classe.__name__} expoe escrita: {proibidos}"


# ---------------------------------------------------------------------------
# Pagination, resilience and failures -- specific to those that talk to a network
# ---------------------------------------------------------------------------

def _jira(**kw) -> JiraTasks:
    return JiraTasks(transport=SnapshotTransport(directory=SNAPSHOTS, **kw),
                     site="https://exemplo.atlassian.net")


def test_pagination_walks_all_the_pages():
    """Paginating wrong returns the first page for ever."""
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
    """The engine must not break because the provider went down -- nor confuse
    an outage with an absence of work."""
    port = _jira(failures={"search": error})
    with pytest.raises(AdapterError):
        port.list_tasks()


def test_failure_of_authentication_not_is_retried():
    """Repetir credencial invalida so bloqueia a conta."""
    from regente.adapters.tasks.transport import HttpTransport
    attempts = []

    def credential():
        attempts.append(1)
        raise AuthFailure("credencial ausente")

    t = HttpTransport(base_url="https://exemplo.invalido", credential=credential,
                       max_attempts=5)
    with pytest.raises(AuthFailure):
        t.get("/rest/api/3/myself")
    assert len(attempts) == 1, f"tentou {len(attempts)}x uma credencial invalida"


def test_snapshot_missing_is_not_found_is_not_list_empty():
    port = _jira()
    with pytest.raises(NotFound):
        port.get_task("SG-NAO-CAPTURADA")


def test_response_of_kind_wrong_is_refused(tmp_path):
    """A 200 with a valid body of the wrong shape must not become 'zero tasks'."""
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
# Observability
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
    fields = {f for c in vistas for f in c.__slots__}
    assert not (fields & {"corpo", "body", "credencial", "token", "autorizacao"})
