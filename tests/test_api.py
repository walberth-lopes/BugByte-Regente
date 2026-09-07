# -*- coding: utf-8 -*-
"""A API: escopo antes de leitura, e nenhuma escrita.

`resolve()` e uma funcao pura, entao estes testes exercitam exatamente o codigo
que o servidor HTTP executa -- sem cliente de teste no meio, e sem a duvida de
estar provando a camada errada. Um teste sobe o servidor de verdade, porque
roteamento correto e servidor que nao sobe ainda e uma tela em branco.

O que se prova aqui:

* escopo em CADA rota, com ids validos do tenant errado;
* nenhuma rota escreve, e a recusa diz por que -- nao "ainda nao implementado";
* arquivo estatico nao vira leitura arbitraria de disco;
* o payload nunca melhora o estado que o motor gravou.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from regente.core import ids
from regente.core.model import ExternalRef, Run, RunState, Task, Workspace
from regente.core.policy import AutonomyLevel
from regente.core.states import TaskState
from regente.app.api import Api, Principal
from regente.engine.readmodel import ReadModel
from regente.engine.store_sqlite import SqliteStore

T0 = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 40

#: Toda rota escopada por workspace. A lista existe para que uma rota nova
#: entre aqui -- um endpoint sem teste de tenancy e como o vazamento chega.
SCOPED = ["", "/overview", "/health", "/tasks", "/runs", "/deliveries",
          "/events", "/escalations"]


@pytest.fixture
def bench(tmp_path):
    store = SqliteStore(tmp_path / "api.db", clock=lambda: T0)
    store.migrate()

    def workspace(wid, client, client_name):
        store.save_client(client, "org", client_name)
        store.save_workspace(Workspace(id=wid, client_id=client, name="main",
                                       max_autonomy=AutonomyLevel.L3))

    def task(wid, key, state=TaskState.READY):
        t = Task(id=ids.new_id(ids.TASK), workspace_id=wid, project_id="p",
                 title=f"trabalho {key}", state=TaskState.READY,
                 externo=ExternalRef(provider="filesystem", key=key))
        store.save_task(t)
        if state is not TaskState.READY:
            for step in (TaskState.ASSIGNED, TaskState.IMPLEMENTING,
                         TaskState.TESTING):
                store.transition(t.id, step, actor="t", reason="setup",
                                 workspace_id=wid)
                if step is state:
                    break
        return store.task(t.id, wid)

    workspace("wks_a", "cli_a", "Acme")
    workspace("wks_b", "cli_b", "Beta")
    a = task("wks_a", "SAME-1", TaskState.TESTING)
    b = task("wks_b", "SAME-1", TaskState.TESTING)
    store.save_run(Run(id="run_a", task_id=a.id, task_key="SAME-1",
                       workspace_id="wks_a", agent="coder",
                       state=RunState.SUCCEEDED))
    store.save_run(Run(id="run_b", task_id=b.id, task_key="SAME-1",
                       workspace_id="wks_b", agent="coder",
                       state=RunState.SUCCEEDED))
    store.open_delivery("wks_a", "SAME-1", "run_a", "github", "acme/api",
                        "regente/x", SHA)
    store.open_delivery("wks_b", "SAME-1", "run_b", "github", "acme/api",
                        "regente/x", "b" * 40)

    yield SimpleNamespace(
        api=Api(read=ReadModel(store=store, clock=lambda: T0)),
        tasks={"a": a, "b": b}, store=store)
    store.close()


def get(bench, path, query=None, principal=None):
    return bench.api.resolve("GET", path, query or {}, principal)


# ---------------------------------------------------------------------------
# Somente leitura
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_no_method_that_writes_is_accepted(bench, method):
    r = bench.api.resolve(method, "/api/workspaces/wks_a/tasks", {}, None)
    assert r.status == 405
    assert r.payload["error"] == "read_only"
    # Nao e "ainda nao": a autoridade de escrita nao mora numa tela.
    assert "somente leitura" in r.payload["detail"]


def test_there_is_no_route_that_mutates(bench):
    """Prova por ausencia, lida do proprio codigo.

    Uma rota de escrita acrescentada distraidamente passaria despercebida numa
    revisao; aqui ela quebra o teste que diz que a API nao escreve.
    """
    import ast
    from pathlib import Path

    source = Path("regente/app/api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    writing = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr.startswith(("save_", "record_", "open_delivery",
                                          "transition", "decide_", "acquire_",
                                          "release_", "claim", "mark_")):
                writing.append(f"linha {node.lineno}: {node.func.attr}")
    assert not writing, "a API escreveu no motor:\n  " + "\n  ".join(writing)


# ---------------------------------------------------------------------------
# Escopo
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("suffix", SCOPED)
def test_every_scoped_route_refuses_a_workspace_the_principal_cannot_read(
        bench, suffix):
    only_a = Principal(name="a", workspaces=frozenset({"wks_a"}))
    assert get(bench, f"/api/workspaces/wks_a{suffix}", principal=only_a).status == 200
    r = get(bench, f"/api/workspaces/wks_b{suffix}", principal=only_a)
    assert r.status == 404, f"{suffix} vazou para outro tenant"


@pytest.mark.parametrize("suffix", SCOPED)
def test_a_workspace_that_does_not_exist_answers_the_same_as_one_forbidden(
        bench, suffix):
    """Distinguir as duas confirmaria a existencia de um workspace alheio."""
    forbidden = get(bench, f"/api/workspaces/wks_b{suffix}",
                    principal=Principal(workspaces=frozenset({"wks_a"})))
    absent = get(bench, f"/api/workspaces/wks_ghost{suffix}")
    assert forbidden.status == absent.status == 404
    assert forbidden.payload == absent.payload


def test_a_task_id_from_another_tenant_is_not_readable(bench):
    theirs = bench.tasks["b"].id
    r = get(bench, f"/api/workspaces/wks_a/tasks/{theirs}")
    assert r.status == 404
    assert get(bench, f"/api/workspaces/wks_b/tasks/{theirs}").status == 200


def test_a_run_id_from_another_tenant_is_not_readable(bench):
    assert get(bench, "/api/workspaces/wks_a/runs/run_b").status == 404
    assert get(bench, "/api/workspaces/wks_b/runs/run_b").status == 200


def test_a_task_never_leaks_through_the_other_tenants_run(bench):
    """Mesma chave, mesmo repositorio, mesma branch nos dois clientes."""
    a = get(bench, "/api/workspaces/wks_a/runs/run_a").payload
    b = get(bench, "/api/workspaces/wks_b/runs/run_b").payload
    assert a["run"]["task_id"] == bench.tasks["a"].id
    assert b["run"]["task_id"] == bench.tasks["b"].id
    assert a["run"]["task_id"] != b["run"]["task_id"]
    assert a["run"]["client"] == "Acme" and b["run"]["client"] == "Beta"


def test_a_delivery_of_one_tenant_never_appears_in_the_other(bench):
    a = get(bench, "/api/workspaces/wks_a/deliveries").payload["deliveries"]
    b = get(bench, "/api/workspaces/wks_b/deliveries").payload["deliveries"]
    assert [d["commit_sha"] for d in a] == [SHA]
    assert [d["commit_sha"] for d in b] == ["b" * 40]


def test_the_workspace_list_shows_only_what_the_principal_may_read(bench):
    only_a = Principal(workspaces=frozenset({"wks_a"}))
    payload = get(bench, "/api/workspaces", principal=only_a).payload
    assert [w["id"] for w in payload["workspaces"]] == ["wks_a"]
    assert len(get(bench, "/api/workspaces").payload["workspaces"]) == 2


def test_the_client_list_hides_a_client_with_no_readable_workspace(bench):
    only_a = Principal(workspaces=frozenset({"wks_a"}))
    payload = get(bench, "/api/clients", principal=only_a).payload
    assert [c["name"] for c in payload["clients"]] == ["Acme"]


def test_global_health_covers_only_readable_workspaces(bench):
    only_a = Principal(workspaces=frozenset({"wks_a"}))
    payload = get(bench, "/api/health", principal=only_a).payload
    assert [w["id"] for w in payload["workspaces"]] == ["wks_a"]


def test_global_health_reports_the_worst_workspace_not_an_average(bench):
    """Um agregado que suaviza produz um verde que sobrevive a um cliente
    parado -- e e esse cliente que precisa aparecer."""
    payload = get(bench, "/api/health").payload
    assert payload["level"] == "STUCK"
    assert {w["health"] for w in payload["workspaces"]} == {"STUCK"}


# ---------------------------------------------------------------------------
# O payload nao melhora o estado
# ---------------------------------------------------------------------------

def test_a_stalled_task_arrives_stalled_with_its_reason(bench):
    payload = get(bench, "/api/workspaces/wks_a/overview").payload
    assert payload["health"]["level"] == "STUCK"
    assert payload["health"]["healthy"] is False
    stalled = payload["stalled"]
    assert stalled and stalled[0]["state"]["name"] == "TESTING"
    assert stalled[0]["state"]["owner"] == "nobody"
    assert "nao tem etapa" in stalled[0]["state"]["next_action"]


def test_a_delivery_without_a_pull_request_arrives_without_one(bench):
    d = get(bench, "/api/workspaces/wks_a/deliveries").payload["deliveries"][0]
    assert d["pull_request"] is None
    assert d["ci_state"] == "NOT_OBSERVED"
    assert d["ci_green"] is False


def test_the_payload_is_json_and_carries_no_internal_row(bench):
    """O modelo externo e separado do interno de proposito.

    Se a linha do banco vazar inteira, renomear uma coluna quebra quem consome
    -- e um campo interno novo passa a ser publico so por existir.
    """
    body = get(bench, "/api/workspaces/wks_a/tasks").rendered().decode()
    payload = json.loads(body)
    task = payload["tasks"][0]
    for internal in ("data", "externo", "project_id", "paused_at", "resources"):
        assert internal not in task, f"campo interno exposto: {internal}"
    assert task["key"] == "SAME-1"


def test_a_bad_limit_falls_back_instead_of_failing_or_returning_everything(bench):
    r = get(bench, "/api/workspaces/wks_a/events", {"limit": ["abacaxi"]})
    assert r.status == 200
    r = get(bench, "/api/workspaces/wks_a/events", {"limit": ["999999"]})
    assert r.status == 200


# ---------------------------------------------------------------------------
# Estaticos
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/../../../etc/passwd",
    "/..%2f..%2fregente.yaml",
    "/./../regente/app/api.py",
    "/app.js/../../api.py",
])
def test_a_path_that_escapes_the_ui_directory_is_refused(bench, path):
    """Servir um diretorio sem restricao e como um caminho arbitrario virar
    leitura de arquivo arbitrario no servidor."""
    r = get(bench, path)
    assert r.status == 404, f"{path} escapou da raiz da UI"


def test_only_the_declared_extensions_are_served(bench, tmp_path):
    (bench.api.ui_root / "segredo.env").write_text("TOKEN=x", encoding="utf-8")
    try:
        assert get(bench, "/segredo.env").status == 404
    finally:
        (bench.api.ui_root / "segredo.env").unlink()


def test_the_root_serves_the_mission_control(bench):
    r = get(bench, "/")
    assert r.status == 200
    assert r.content_type.startswith("text/html")
    assert b"REGENTE" in r.rendered()


def test_an_unknown_api_route_is_not_silently_a_page(bench):
    r = get(bench, "/api/nao-existe")
    assert r.status == 404
    assert r.payload["error"] == "not_found"


# ---------------------------------------------------------------------------
# HTTP de verdade
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_the_server_actually_serves(tmp_path):
    """Roteamento certo com servidor que nao sobe ainda e uma tela em branco."""
    import threading
    import urllib.request

    from regente.app.api import serve

    store = SqliteStore(tmp_path / "live.db", clock=lambda: T0)
    store.migrate()
    store.save_client("cli_a", "org", "Acme")
    store.save_workspace(Workspace(id="wks_a", client_id="cli_a", name="main",
                                   max_autonomy=AutonomyLevel.L2))
    httpd = serve(ReadModel(store=store, clock=lambda: T0),
                  host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/workspaces", timeout=10) as r:
            assert r.status == 200
            body = json.loads(r.read())
        assert body["workspaces"][0]["client"] == "Acme"

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            assert r.status == 200
            assert b"mission control" in r.read()

        # Escrita recusada tambem no caminho HTTP, nao so no roteador.
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/workspaces", method="POST",
            data=b"{}")
        try:
            urllib.request.urlopen(request, timeout=10)
            raise AssertionError("a API aceitou uma escrita por HTTP")
        except urllib.error.HTTPError as e:
            # 405 com motivo, e nao o 501 generico da stdlib: "metodo nao
            # suportado" le-se como "ainda nao implementado".
            assert e.code == 405
            assert json.loads(e.read())["error"] == "read_only"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)
        store.close()


def test_a_symlink_out_of_the_ui_directory_is_refused(bench, tmp_path):
    """A segunda guarda dos estaticos, exercitada sozinha.

    A checagem de segmentos suspeitos pega `..` no caminho pedido. O que ela nao
    pega e um link dentro da propria pasta apontando para fora -- e e por isso
    que o caminho resolvido tambem e conferido contra a raiz.
    """
    import os

    outside = tmp_path / "segredo.js"
    outside.write_text("TOKEN=x", encoding="utf-8")
    link = bench.api.ui_root / "atalho.js"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"este sistema nao permite criar symlink: {e}")
    try:
        r = get(bench, "/atalho.js")
        assert r.status == 404, "um link para fora da raiz foi servido"
    finally:
        link.unlink()
