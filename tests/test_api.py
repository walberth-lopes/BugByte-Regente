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
from regente.app.api import Api
from regente.core.principal import ANONYMOUS, Principal
from regente.engine.readmodel import ReadModel
from regente.engine.store_sqlite import SqliteStore

T0 = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 40

#: Toda rota escopada por workspace. A lista existe para que uma rota nova
#: entre aqui -- um endpoint sem teste de tenancy e como o vazamento chega.
SCOPED = ["", "/overview", "/health", "/tasks", "/runs", "/deliveries",
          "/events", "/escalations", "/resources", "/resources/providers"]


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


def operator(*reads, decides=(), method="test") -> Principal:
    """Uma identidade JA autenticada, com alcance explicito.

    `method` preenchido e o que separa autenticado de afirmado. Um principal
    montado sem ele nao passa por nenhuma barreira -- que e exatamente o
    comportamento que se quer.
    """
    from regente.core.access import abilities_of
    return Principal(subject="operador", display="operador", method=method,
                     provider=method,
                     workspaces=frozenset(reads) if reads else None,
                     abilities={w: abilities_of("operator") for w in decides})


def get(bench, path, query=None, principal=None):
    # Leitura autenticada por default: o que se testa em cada caso e o ESCOPO,
    # e um default anonimo faria todos falharem pelo mesmo motivo, escondendo o
    # que cada teste queria dizer. O anonimo tem testes proprios.
    who = operator() if principal is None else principal
    return bench.api.resolve("GET", path, query or {}, who)


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
    only_a = operator("wks_a")
    assert get(bench, f"/api/workspaces/wks_a{suffix}", principal=only_a).status == 200
    r = get(bench, f"/api/workspaces/wks_b{suffix}", principal=only_a)
    assert r.status == 404, f"{suffix} vazou para outro tenant"


@pytest.mark.parametrize("suffix", SCOPED)
def test_a_workspace_that_does_not_exist_answers_the_same_as_one_forbidden(
        bench, suffix):
    """Distinguir as duas confirmaria a existencia de um workspace alheio."""
    forbidden = get(bench, f"/api/workspaces/wks_b{suffix}",
                    principal=operator("wks_a"))
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
    only_a = operator("wks_a")
    payload = get(bench, "/api/workspaces", principal=only_a).payload
    assert [w["id"] for w in payload["workspaces"]] == ["wks_a"]
    assert len(get(bench, "/api/workspaces").payload["workspaces"]) == 2


def test_the_client_list_hides_a_client_with_no_readable_workspace(bench):
    only_a = operator("wks_a")
    payload = get(bench, "/api/clients", principal=only_a).payload
    assert [c["name"] for c in payload["clients"]] == ["Acme"]


def test_global_health_covers_only_readable_workspaces(bench):
    only_a = operator("wks_a")
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
    """A raiz serve a CASCA da Mission Control, e nao um HTML qualquer.

    A versao anterior deste teste procurava a marca escrita em caixa alta.
    Isso amarrava o contrato da rota a uma escolha de tipografia: trocar
    "REGENTE" por "Regente" na tela derrubava um teste que nao fala de
    tipografia nenhuma. O que a rota promete e o esqueleto onde a aplicacao
    monta -- o alvo do JS, o ponto de entrada e a folha de estilo.
    """
    r = get(bench, "/")
    assert r.status == 200
    assert r.content_type.startswith("text/html")
    corpo = r.rendered()
    # O alvo onde a aplicacao monta, e um script de modulo. Os nomes dos
    # arquivos levam hash do build e mudam a cada alteracao -- prende-los aqui
    # faria a guarda quebrar em toda troca de CSS, sem falar de nada.
    assert b'id="raiz"' in corpo, "a casca perdeu o ponto de montagem"
    assert b'type="module"' in corpo, "a casca perdeu o script da aplicacao"
    assert b".js" in corpo and b".css" in corpo, "a casca perdeu os assets"


def test_an_unknown_api_route_is_not_silently_a_page(bench):
    r = get(bench, "/api/nao-existe")
    assert r.status == 404
    assert r.payload["error"] == "not_found"


# ---------------------------------------------------------------------------
# HTTP de verdade
# ---------------------------------------------------------------------------
#
# A UI e cliente. A API e fronteira. O Core e autoridade.
#
# Estes testes nao passam pela tela: falam HTTP direto. "O botao nao aparece"
# nao e defesa nenhuma -- qualquer requisicao valida pode TENTAR, e o que
# importa e o que a fronteira faz com ela.

@pytest.fixture
def live(tmp_path):
    """Um servidor de verdade, com identidade de verdade e uma escalada aberta."""
    import threading

    from regente.adapters.identity.dev_token import DevTokenIdentity
    from regente.app.api import serve
    from regente.core.policy import PolicyEngine
    from regente.core.risk import RiskLevel
    from regente.engine import escalation
    from regente.engine.decision import DecisionService

    store = SqliteStore(tmp_path / "live.db", clock=lambda: T0)
    store.migrate()
    store.save_client("cli_a", "org", "Acme")
    store.save_client("cli_b", "org", "Beta")
    for wid, client in (("wks_a", "cli_a"), ("wks_b", "cli_b")):
        store.save_workspace(Workspace(id=wid, client_id=client, name="main",
                                       max_autonomy=AutonomyLevel.L3))

    made = {}
    for wid in ("wks_a", "wks_b"):
        t = Task(id=ids.new_id(ids.TASK), workspace_id=wid, project_id="p",
                 title="precisa de gente", state=TaskState.READY,
                 externo=ExternalRef(provider="filesystem", key="SAME-1"))
        store.save_task(t)
        for step in (TaskState.ASSIGNED, TaskState.WAITING_HUMAN):
            store.transition(t.id, step, actor="t", reason="setup",
                             workspace_id=wid)
        a = escalation.build(task=store.task(t.id, wid),
                             what_happened="o agente parou",
                             why_it_matters="alguem precisa escolher",
                             risk=RiskLevel.MEDIUM)
        store.open_approval(a)
        made[wid] = {"task": t, "approval": a}

    identity = DevTokenIdentity(operator="walberth",
                                reads=frozenset({"wks_a"}))
    # A autoridade nao vem mais do provedor: vem de uma concessao GRAVADA, com
    # autor e data. E por isso que ela e montada aqui, e nao num campo do
    # adapter -- e por isso que revogar fecha a porta.
    from regente.core.access import AccessGrant, PrincipalRef, abilities_of
    store.open_grant(AccessGrant(
        id=ids.new_id(ids.GRANT), client_id="cli_a", workspace_id="wks_a",
        principal=PrincipalRef("dev-token", "walberth"),
        abilities=abilities_of("operator"), granted_by="os-account:fundador",
        granted_at=T0))
    regras = PolicyEngine.from_config([
        {"name": "decidir", "effect": "ALLOW",
         "match": {"action": ["approval.decide", "workspace.access.grant",
                              "workspace.access.revoke",
                              "workspace.access.list"]}}])
    decisions = DecisionService(
        store=store, policy=regras, clock=lambda: T0, organization="org",
        client="Acme", workspace_name="main")
    from regente.engine.access import AccessService
    access = AccessService(store=store, policy=regras, clock=lambda: T0,
                           organization="org", client="Acme",
                           workspace_name="main")

    httpd = serve(ReadModel(store=store, clock=lambda: T0),
                  host="127.0.0.1", port=0, identity=identity,
                  decisions=decisions, access=access,
                  session_token=identity.token)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            port=httpd.server_address[1], token=identity.token, store=store,
            made=made, identity=identity)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)
        identity.close()
        store.close()


def call(live, path, method="GET", body=None, token="__default__"):
    """Uma requisicao HTTP crua. Devolve (status, payload)."""
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{live.port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    used = live.token if token == "__default__" else token
    if used:
        request.add_header("Authorization", f"Bearer {used}")
    try:
        with urllib.request.urlopen(request, timeout=15) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")}


@pytest.mark.slow
def test_an_authenticated_read_works_and_an_anonymous_one_sees_nothing(live):
    status, payload = call(live, "/api/workspaces")
    assert status == 200
    assert [w["id"] for w in payload["workspaces"]] == ["wks_a"]

    status, payload = call(live, "/api/workspaces", token="")
    assert status == 200
    assert payload["workspaces"] == [], (
        "uma requisicao sem credencial enxergou workspaces")


@pytest.mark.slow
def test_a_wrong_token_is_not_a_partial_identity(live):
    status, payload = call(live, "/api/workspaces", token="quase-o-token")
    assert status == 200 and payload["workspaces"] == []


@pytest.mark.slow
def test_a_decision_over_http_crosses_every_barrier_and_persists(live):
    """O caminho inteiro, sem tela: autenticar, escopo, policy, Core, auditoria."""
    approval = live.made["wks_a"]["approval"]
    status, payload = call(
        live, f"/api/workspaces/wks_a/approvals/{approval.id}/decision",
        method="POST", body={"choice": "seguir", "note": "confirmado"})

    assert status == 200, payload
    assert payload["accepted"] is True
    assert payload["previous_state"] == "OPEN"
    assert payload["new_state"] == "DECIDED"
    assert payload["decided_by"] == "dev-token:walberth"

    # Persistido, e nao apenas respondido.
    stored = live.store.approval(approval.id, "wks_a")
    assert stored.state.value == "DECIDED"
    assert stored.choice == "seguir"
    assert stored.decided_by == "dev-token:walberth"

    # E legivel de volta pela leitura, que e o que a tela consulta.
    status, payload = call(live, "/api/workspaces/wks_a/escalations")
    assert status == 200
    assert payload["escalations"] == [], "a escalada decidida continua na fila"


@pytest.mark.slow
def test_an_unauthenticated_post_is_refused_before_anything_is_written(live):
    approval = live.made["wks_a"]["approval"]
    status, payload = call(
        live, f"/api/workspaces/wks_a/approvals/{approval.id}/decision",
        method="POST", body={"choice": "seguir"}, token="")

    assert status == 401
    assert payload["error"] == "unauthenticated"
    assert live.store.approval(approval.id, "wks_a").state.value == "OPEN"


@pytest.mark.slow
def test_a_decision_in_another_tenant_is_refused_and_reveals_nothing(live):
    """O mesmo operador, a mesma chave de task, o outro cliente."""
    theirs = live.made["wks_b"]["approval"]
    status, payload = call(
        live, f"/api/workspaces/wks_b/approvals/{theirs.id}/decision",
        method="POST", body={"choice": "seguir"})

    assert status == 404
    assert "wks_b" not in json.dumps(payload)
    assert "Beta" not in json.dumps(payload)
    assert live.store.approval(theirs.id, "wks_b").state.value == "OPEN"


@pytest.mark.slow
def test_an_approval_id_from_another_tenant_does_not_work_in_my_workspace(live):
    """Conhecer o id nao e autoridade, nem quando o workspace e o meu."""
    theirs = live.made["wks_b"]["approval"]
    status, _ = call(
        live, f"/api/workspaces/wks_a/approvals/{theirs.id}/decision",
        method="POST", body={"choice": "seguir"})
    assert status == 404
    assert live.store.approval(theirs.id, "wks_b").state.value == "OPEN"


@pytest.mark.slow
def test_the_body_cannot_declare_who_is_deciding(live):
    approval = live.made["wks_a"]["approval"]
    for forged in ({"choice": "seguir", "principal": "outra-pessoa"},
                   {"choice": "seguir", "decided_by": "chefe"},
                   {"choice": "seguir", "subject": "root"},
                   {"choice": "seguir", "workspace_id": "wks_b"}):
        status, payload = call(
            live, f"/api/workspaces/wks_a/approvals/{approval.id}/decision",
            method="POST", body=forged)
        assert status == 400, forged
        assert "nao vem do corpo" in payload["detail"]
    assert live.store.approval(approval.id, "wks_a").state.value == "OPEN"


@pytest.mark.slow
def test_a_second_decision_conflicts_instead_of_overwriting(live):
    approval = live.made["wks_a"]["approval"]
    path = f"/api/workspaces/wks_a/approvals/{approval.id}/decision"

    first = call(live, path, method="POST", body={"choice": "seguir"})
    second = call(live, path, method="POST", body={"choice": "cancelar"})

    assert first[0] == 200
    assert second[0] == 409
    assert second[1]["error"] == "conflict"
    stored = live.store.approval(approval.id, "wks_a")
    assert stored.choice == "seguir", "a segunda decisao sobrescreveu a primeira"


@pytest.mark.slow
def test_a_choice_outside_the_offered_options_is_refused(live):
    approval = live.made["wks_a"]["approval"]
    status, payload = call(
        live, f"/api/workspaces/wks_a/approvals/{approval.id}/decision",
        method="POST", body={"choice": "mergear-tudo"})
    assert status == 422
    assert payload["error"] == "invalid_state"
    assert live.store.approval(approval.id, "wks_a").state.value == "OPEN"


@pytest.mark.slow
def test_no_other_post_route_exists(live):
    approval = live.made["wks_a"]["approval"]
    for path in ("/api/workspaces/wks_a/tasks",
                 "/api/workspaces/wks_a/runs/run_a",
                 f"/api/workspaces/wks_a/approvals/{approval.id}",
                 "/api/workspaces/wks_a/approvals/x/decision/extra",
                 "/api/health"):
        status, payload = call(live, path, method="POST", body={"choice": "x"})
        assert status == 405, path
        assert payload["error"] == "read_only"


@pytest.mark.slow
def test_the_page_carries_the_session_token_and_the_disk_file_does_not(live):
    """A pagina recebe o segredo; o arquivo no repositorio nunca o contem.

    E de la que a tela o le. Um site aberto noutra aba pode disparar um POST
    para o loopback, mas nao consegue LER esta pagina -- entao nao alcanca o
    token, e o POST forjado chega sem credencial.
    """
    import urllib.request

    from regente.app.api import UI_ROOT

    with urllib.request.urlopen(f"http://127.0.0.1:{live.port}/",
                                timeout=15) as r:
        page = r.read().decode()
    assert live.token in page
    assert "{{SESSION_TOKEN}}" not in page
    assert live.token not in (UI_ROOT / "index.html").read_text(encoding="utf-8")


def test_the_session_token_never_touches_the_disk(tmp_path):
    """Um segredo escrito e nunca lido e liability pura.

    A primeira versao gravava o token num arquivo que nada consultava. Ele
    sobreviveu a um `kill` -- o `finally` que o apagaria nao roda -- e ficou
    para tras sem ter servido para nada.
    """
    from regente.adapters.identity.dev_token import DevTokenIdentity

    antes = set(tmp_path.rglob("*"))
    identity = DevTokenIdentity(operator="x")
    assert identity.token
    assert set(tmp_path.rglob("*")) == antes, "o token foi parar em disco"

    achados = [f for f in tmp_path.rglob("*")
               if f.is_file() and identity.token in f.read_text(
                   encoding="utf-8", errors="ignore")]
    assert not achados, f"o segredo apareceu em {achados}"


@pytest.mark.slow
def test_parallel_reads_never_answer_404_for_something_that_exists(live):
    """O defeito que a Mission Control real encontrou.

    `check_same_thread=False` estava ligado e nao havia trava nenhuma. Enquanto
    a segunda thread era so o batimento de lease, passou despercebido. O
    navegador dispara TRES leituras em paralelo a cada cinco segundos, cada uma
    numa thread do servidor -- e a mesma URL passou a responder 200, depois
    404, depois resposta vazia.

    Um 404 intermitente e a pior forma disto: parece dado que sumiu, e quem
    olha a tela conclui que perdeu trabalho.
    """
    import concurrent.futures

    caminhos = ["/api/workspaces/wks_a/tasks",
                "/api/workspaces/wks_a/escalations",
                "/api/workspaces/wks_a/events?limit=60",
                "/api/workspaces/wks_a/overview",
                "/api/workspaces/wks_a/health"] * 12

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        resultados = list(pool.map(lambda p: (p, call(live, p)[0]), caminhos))

    ruins = [(p, s) for p, s in resultados if s != 200]
    assert not ruins, (
        f"{len(ruins)} de {len(resultados)} leituras paralelas falharam "
        f"para recursos que existem: {ruins[:5]}")


@pytest.mark.slow
def test_a_read_and_a_decision_at_the_same_time_do_not_corrupt_each_other(live):
    """Leitura e escrita concorrentes: uma decisao, e nenhuma leitura torta."""
    import concurrent.futures

    approval = live.made["wks_a"]["approval"]
    path = f"/api/workspaces/wks_a/approvals/{approval.id}/decision"

    def trabalho(i):
        if i % 4 == 0:
            return ("post", call(live, path, method="POST",
                                 body={"choice": "seguir"})[0])
        return ("get", call(live, "/api/workspaces/wks_a/overview")[0])

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        saidas = list(pool.map(trabalho, range(24)))

    leituras = [s for k, s in saidas if k == "get"]
    escritas = [s for k, s in saidas if k == "post"]
    assert set(leituras) == {200}, f"leitura quebrou sob escrita: {leituras}"
    assert escritas.count(200) == 1, f"nem uma nem duas decisoes: {escritas}"
    assert set(escritas) <= {200, 409}
    assert live.store.approval(approval.id, "wks_a").choice == "seguir"
