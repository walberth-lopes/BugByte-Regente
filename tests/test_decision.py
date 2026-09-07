# -*- coding: utf-8 -*-
"""A primeira escrita humana, e cada barreira que ela precisa atravessar.

Sete perguntas diferentes, e nenhuma responde pela outra:

    autenticacao   quem e voce?
    autorizacao    voce manda NESTE workspace?
    escopo         a aprovacao e deste workspace?
    policy         esta acao e permitida aqui?
    estado         esta aprovacao ainda esta aberta?
    transicao      a escolha esta entre as oferecidas?
    auditoria      quem, onde, o que, e a partir de que estado

Um teste que so prova o caminho feliz prova que as sete estao presentes na
ordem certa quando tudo da certo -- que e exatamente quando nenhuma delas
importa. Aqui cada barreira e atacada sozinha, com as outras satisfeitas.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from regente.core import ids
from regente.core.model import ApprovalState, ExternalRef, Task, Workspace
from regente.core.policy import PolicyEngine
from regente.core.principal import ANONYMOUS, Principal
from regente.core.risk import RiskLevel
from regente.core.states import TaskState
from regente.engine import escalation
from regente.engine.decision import DECIDE_ACTION, DecisionService, Denial
from regente.engine.store_sqlite import SqliteStore

T0 = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)

ALLOW = [{"name": "decidir", "effect": "ALLOW",
          "match": {"action": DECIDE_ACTION}}]
DENY = [{"name": "nao", "effect": "DENY", "match": {"action": DECIDE_ACTION}}]


@pytest.fixture
def bench(tmp_path):
    store = SqliteStore(tmp_path / "d.db", clock=lambda: T0)
    store.migrate()
    for wid, client, name in (("wks_a", "cli_a", "Acme"),
                              ("wks_b", "cli_b", "Beta")):
        store.save_client(client, "org", name)
        store.save_workspace(Workspace(id=wid, client_id=client, name="main"))
    yield store
    store.close()


def an_approval(store, workspace_id="wks_a", key="SAME-1"):
    """Uma escalada aberta, com a task esperando por uma pessoa."""
    task = Task(id=ids.new_id(ids.TASK), workspace_id=workspace_id,
                project_id="p", title="parou", state=TaskState.READY,
                externo=ExternalRef(provider="filesystem", key=key))
    store.save_task(task)
    for step in (TaskState.ASSIGNED, TaskState.WAITING_HUMAN):
        store.transition(task.id, step, actor="t", reason="setup",
                         workspace_id=workspace_id)
    approval = escalation.build(
        task=store.task(task.id, workspace_id),
        what_happened="o agente parou", why_it_matters="alguem precisa escolher",
        risk=RiskLevel.MEDIUM)
    store.open_approval(approval)
    return approval


def service(store, rules=None) -> DecisionService:
    return DecisionService(
        store=store, policy=PolicyEngine.from_config(
            ALLOW if rules is None else rules),
        clock=lambda: T0, organization="org", client="Acme",
        workspace_name="main")


def operator(*decides, reads=None, method="dev-token") -> Principal:
    return Principal(subject="walberth", display="walberth", method=method,
                     workspaces=reads, decides=frozenset(decides))


# ---------------------------------------------------------------------------
# 1. Autenticacao
# ---------------------------------------------------------------------------

def test_an_unauthenticated_principal_decides_nothing(bench):
    approval = an_approval(bench)
    out = service(bench).decide(ANONYMOUS, "wks_a", approval.id, "seguir")

    assert out.accepted is False
    assert out.denial is Denial.UNAUTHENTICATED
    assert bench.approval(approval.id, "wks_a").state is ApprovalState.OPEN


def test_a_principal_asserted_without_a_method_is_not_authenticated(bench):
    """O campo que separa autenticado de afirmado.

    Um `Principal` montado a mao, com todas as concessoes e sem `method`, e
    exatamente o que um endpoint distraido produziria a partir de um JSON. Ele
    nao passa.
    """
    approval = an_approval(bench)
    forjado = Principal(subject="walberth", method="",
                        decides=frozenset({"wks_a"}))

    out = service(bench).decide(forjado, "wks_a", approval.id, "seguir")
    assert out.denial is Denial.UNAUTHENTICATED
    assert bench.approval(approval.id, "wks_a").state is ApprovalState.OPEN


# ---------------------------------------------------------------------------
# 2. Autorizacao: ler nao concede decidir
# ---------------------------------------------------------------------------

def test_reading_a_workspace_does_not_grant_deciding_in_it(bench):
    """Derivar escrita de leitura faria de todo observador um decisor."""
    approval = an_approval(bench)
    leitor = Principal(subject="x", method="dev-token",
                       workspaces=frozenset({"wks_a"}), decides=frozenset())

    assert leitor.may_read("wks_a") is True
    out = service(bench).decide(leitor, "wks_a", approval.id, "seguir")
    assert out.denial is Denial.NOT_FOUND
    assert bench.approval(approval.id, "wks_a").state is ApprovalState.OPEN


def test_authority_in_one_workspace_is_not_authority_in_another(bench):
    mine = an_approval(bench, "wks_a")
    theirs = an_approval(bench, "wks_b")
    who = operator("wks_a")
    svc = service(bench)

    assert svc.decide(who, "wks_a", mine.id, "seguir").accepted is True
    assert svc.decide(who, "wks_b", theirs.id, "seguir").denial is Denial.NOT_FOUND
    assert bench.approval(theirs.id, "wks_b").state is ApprovalState.OPEN


def test_an_approval_id_from_another_tenant_is_not_reachable(bench):
    """Conhecer o id nao e autoridade, nem dentro do proprio workspace."""
    theirs = an_approval(bench, "wks_b")
    out = service(bench).decide(operator("wks_a"), "wks_a", theirs.id, "seguir")

    assert out.denial is Denial.NOT_FOUND
    assert bench.approval(theirs.id, "wks_b").state is ApprovalState.OPEN


def test_a_refusal_for_another_tenant_reveals_nothing_about_it(bench):
    theirs = an_approval(bench, "wks_b", key="SEGREDO-1")
    out = service(bench).decide(operator("wks_a"), "wks_b", theirs.id, "seguir")

    said = f"{out.reason} {out.denial.value}"
    for leak in ("wks_b", "Beta", "SEGREDO-1", theirs.task_id):
        assert leak not in said, f"a recusa vazou '{leak}'"


def test_an_unknown_workspace_answers_exactly_like_a_forbidden_one(bench):
    """Distinguir os dois confirma a existencia de um workspace alheio."""
    approval = an_approval(bench, "wks_b")
    svc = service(bench)
    forbidden = svc.decide(operator("wks_a"), "wks_b", approval.id, "seguir")
    ghost = svc.decide(operator("wks_a", "wks_fantasma"), "wks_fantasma",
                       approval.id, "seguir")

    assert forbidden.denial is ghost.denial is Denial.NOT_FOUND
    assert forbidden.reason == ghost.reason


# ---------------------------------------------------------------------------
# 3. Policy: autoridade independente
# ---------------------------------------------------------------------------

def test_policy_deny_stops_an_operator_who_is_otherwise_authorised(bench):
    """Autorizado nao e permitido. As duas perguntas sao diferentes."""
    approval = an_approval(bench)
    out = service(bench, DENY).decide(operator("wks_a"), "wks_a", approval.id,
                                      "seguir")

    assert out.denial is Denial.POLICY_DENIED
    assert "DENY" in out.reason
    assert bench.approval(approval.id, "wks_a").state is ApprovalState.OPEN


def test_the_default_policy_denies_because_the_default_is_deny(bench):
    """Sem regra que permita, nao acontece. Vale tambem para humano."""
    approval = an_approval(bench)
    out = service(bench, []).decide(operator("wks_a"), "wks_a", approval.id,
                                    "seguir")
    assert out.denial is Denial.POLICY_DENIED


def test_a_rule_about_another_action_does_not_govern_this_one(bench):
    """`approval.decide` tem nome proprio para nao ser atingido por acidente."""
    approval = an_approval(bench)
    out = service(bench, [
        {"name": "deploy", "effect": "DENY", "match": {"action": "deploy.*"}},
        {"name": "decidir", "effect": "ALLOW", "match": {"action": DECIDE_ACTION}},
    ]).decide(operator("wks_a"), "wks_a", approval.id, "seguir")
    assert out.accepted is True


def test_human_approval_does_not_ask_a_human_to_approve_a_human(bench):
    """`HUMAN_APPROVAL` ja esta satisfeito: o ator E a pessoa.

    Tratar isso como recusa travaria justamente a fila que este caminho existe
    para destravar -- e nao ha para quem escalar a escalada.
    """
    approval = an_approval(bench)
    out = service(bench, [
        {"name": "assinatura", "effect": "HUMAN_APPROVAL",
         "match": {"action": DECIDE_ACTION}},
    ]).decide(operator("wks_a"), "wks_a", approval.id, "seguir")

    assert out.accepted is True
    assert bench.approval(approval.id, "wks_a").state is ApprovalState.DECIDED


# ---------------------------------------------------------------------------
# 4. Estado
# ---------------------------------------------------------------------------

def test_a_valid_decision_is_recorded_with_who_and_from_what_state(bench):
    approval = an_approval(bench)
    out = service(bench).decide(operator("wks_a"), "wks_a", approval.id,
                                "seguir", note="conferido")

    assert out.accepted is True
    assert out.previous_state == "OPEN" and out.new_state == "DECIDED"
    assert out.decided_by == "dev-token:walberth"
    assert out.task_key == "SAME-1"
    # A task NAO se move aqui: quem a retoma e o proximo tick.
    assert out.task_state == "WAITING_HUMAN"
    assert bench.task(approval.task_id, "wks_a").state is TaskState.WAITING_HUMAN


def test_a_second_decision_conflicts_and_keeps_the_first(bench):
    approval = an_approval(bench)
    svc = service(bench)
    svc.decide(operator("wks_a"), "wks_a", approval.id, "seguir")

    out = svc.decide(operator("wks_a"), "wks_a", approval.id, "cancelar")
    assert out.denial is Denial.CONFLICT
    assert "ja foi decidida" in out.reason
    assert bench.approval(approval.id, "wks_a").choice == "seguir"


def test_an_approval_that_does_not_exist_is_not_found(bench):
    out = service(bench).decide(operator("wks_a"), "wks_a", "apr_fantasma",
                                "seguir")
    assert out.denial is Denial.NOT_FOUND


def test_a_choice_outside_the_offered_options_is_refused(bench):
    approval = an_approval(bench)
    out = service(bench).decide(operator("wks_a"), "wks_a", approval.id,
                                "mergear-agora")

    assert out.denial is Denial.INVALID_STATE
    assert "nao esta entre as opcoes" in out.reason
    assert bench.approval(approval.id, "wks_a").state is ApprovalState.OPEN


# ---------------------------------------------------------------------------
# 5. Concorrencia
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_two_processes_deciding_at_once_produce_exactly_one_decision(tmp_path):
    """Duas abas, dois processos, um clique cada. Uma decisao.

    Processos de verdade, e nao threads: a guarda que vale esta dentro da
    transacao do SQLite, e uma prova feita em thread poderia passar por um
    detalhe do GIL em vez de pela transacao.
    """
    import json
    import subprocess
    import sys
    from pathlib import Path

    root = tmp_path
    store = SqliteStore(root / "race.db", clock=lambda: T0)
    store.migrate()
    store.save_client("cli_a", "org", "Acme")
    store.save_workspace(Workspace(id="wks_a", client_id="cli_a", name="main"))
    approval = an_approval(store)
    store.close()

    harness = str(Path(__file__).with_name("decision_race.py"))
    procs = [subprocess.Popen(
        [sys.executable, harness, str(root), approval.id, escolha],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for escolha in ("seguir", "cancelar")]
    saidas = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode(errors="replace")
        saidas.append(json.loads(out.decode().strip().splitlines()[-1]))

    aceitas = [s for s in saidas if s["accepted"]]
    recusadas = [s for s in saidas if not s["accepted"]]
    assert len(aceitas) == 1, f"nem uma nem duas: {saidas}"
    assert len(recusadas) == 1
    assert recusadas[0]["denial"] == "CONFLICT"

    reaberto = SqliteStore(root / "race.db")
    try:
        final = reaberto.approval(approval.id, "wks_a")
        assert final.state is ApprovalState.DECIDED
        assert final.choice == aceitas[0]["choice"]
    finally:
        reaberto.close()


# ---------------------------------------------------------------------------
# 6. Auditoria
# ---------------------------------------------------------------------------

def test_the_audit_says_who_where_what_and_from_what_state(bench):
    approval = an_approval(bench)
    service(bench).decide(operator("wks_a"), "wks_a", approval.id, "seguir")

    trail = [e for e in bench.events("wks_a", limit=50)
             if e.kind == "decisao_humana_autenticada"]
    assert len(trail) == 1
    e = trail[0]

    assert e.actor == "dev-token:walberth"
    assert e.data["subject"] == "walberth"
    assert e.data["method"] == "dev-token"
    assert e.data["workspace_id"] == "wks_a"
    assert e.data["client"] == "Acme"
    assert e.data["organization"] == "org"
    assert e.data["approval_id"] == approval.id
    assert e.data["task_key"] == "SAME-1"
    assert e.data["choice"] == "seguir"
    assert e.data["previous_state"] == "OPEN"
    assert e.data["new_state"] == "DECIDED"
    assert e.data["task_state"] == "WAITING_HUMAN"
    assert e.ts is not None
    assert e.task_id == approval.task_id


def test_the_audit_is_attributable_and_not_just_a_verb(bench):
    """"approval decided" sem sujeito e um registro que nao responde nada."""
    approval = an_approval(bench)
    service(bench).decide(operator("wks_a"), "wks_a", approval.id, "seguir")

    e = [x for x in bench.events("wks_a", limit=50)
         if x.kind == "decisao_humana_autenticada"][0]
    assert "walberth" in e.summary
    assert e.actor and e.actor != "engine"


def test_the_note_is_never_persisted_verbatim(bench):
    """Texto livre e onde uma credencial colada por engano viraria registro."""
    approval = an_approval(bench)
    segredo = "ghp_naoehumtokendeverdade0000"
    service(bench).decide(operator("wks_a"), "wks_a", approval.id, "seguir",
                          note=f"aprovado, token {segredo}")

    for e in bench.events("wks_a", limit=50):
        if e.kind == "decisao_humana_autenticada":
            assert segredo not in str(e.data)
            assert segredo not in e.summary
            assert e.data["note_length"] > 0


def test_a_refused_decision_writes_no_audit_of_a_decision(bench):
    """Recusa nao e decisao. Registrar uma sugeriria que algo foi decidido."""
    approval = an_approval(bench)
    service(bench, DENY).decide(operator("wks_a"), "wks_a", approval.id,
                                "seguir")

    assert not [e for e in bench.events("wks_a", limit=50)
                if e.kind == "decisao_humana_autenticada"]


# ---------------------------------------------------------------------------
# 7. Leitura depois da escrita
# ---------------------------------------------------------------------------

def test_the_read_model_shows_the_persisted_result_not_the_response(bench):
    from regente.engine.readmodel import ReadModel

    approval = an_approval(bench)
    read = ReadModel(store=bench, clock=lambda: T0)
    assert len(read.escalations("wks_a")) == 1

    service(bench).decide(operator("wks_a"), "wks_a", approval.id, "seguir")

    assert read.escalations("wks_a") == ()
    detail = read.task("wks_a", approval.task_id)
    assert detail.escalation is None
    assert not [b for b in detail.blockers if b.kind == "HUMAN"]
    assert any(e.kind == "decisao_humana_autenticada" for e in detail.timeline)


# ---------------------------------------------------------------------------
# 8. A fronteira, verificada no codigo-fonte
# ---------------------------------------------------------------------------

def test_the_api_never_writes_to_the_store_itself():
    """Escrita da API passa pelo Core, sempre.

    Um `store.decide_approval` chamado do endpoint funcionaria perfeitamente e
    pularia policy, auditoria e a traducao de conflito -- e a revisao de codigo
    veria uma linha que parece certa.
    """
    import ast
    from pathlib import Path

    source = Path("regente/app/api.py").read_text(encoding="utf-8")
    escritas = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr.startswith(("save_", "record_", "decide_",
                                          "transition", "open_", "acquire_",
                                          "release_", "claim", "mark_")):
                escritas.append(f"linha {node.lineno}: {node.func.attr}")
    assert not escritas, "a API escreveu direto:\n  " + "\n  ".join(escritas)


def test_only_the_core_decides_an_approval():
    """Uma segunda funcao de decisao e a que um dia esquece uma barreira."""
    import ast
    from pathlib import Path

    permitido = {"regente/engine/decision.py", "regente/engine/store_sqlite.py"}
    culpados = []
    for path in sorted(Path("regente").rglob("*.py")):
        rel = path.as_posix()
        if rel in permitido:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "decide_approval"):
                culpados.append(f"{rel}:{node.lineno}")
    assert not culpados, ("decide_approval chamado fora do Core:\n  "
                          + "\n  ".join(culpados))


def test_the_ui_holds_no_authorisation_logic():
    """A tela pode esconder um botao. Nao pode conceder um.

    O que se procura aqui e a tela chegando sozinha a uma conclusao de
    autoridade: comparar estado para liberar acao, ou montar uma lista de
    opcoes que o motor nunca ofereceu.
    """
    import re
    from pathlib import Path

    source = Path("regente/app/ui/app.js").read_text(encoding="utf-8")
    proibidos = [
        (r"state\s*===?\s*[\"'](WAITING_HUMAN|OPEN|APPROVED)",
         "a tela comparou estado para liberar acao"),
        # `Authorization` e o NOME DO CABECALHO, e apresentar credencial nao e
        # decidir autoridade. A primeira versao deste padrao pegava a propria
        # palavra e acusava a tela de algo que ela nao faz -- um guard que
        # acusa o inocente ensina a ignorar o guard.
        (r"may_decide|canDecide|isAllowed|hasPermission|podeDecidir",
         "a tela decidiu sobre autoridade"),
        (r"[\"'](seguir|investigar|bloquear|cancelar)[\"']",
         "a tela embutiu uma opcao que deveria vir da API"),
    ]
    achados = [motivo for padrao, motivo in proibidos
               if re.search(padrao, source, re.IGNORECASE)]
    assert not achados, "autorizacao na tela:\n  " + "\n  ".join(achados)


def test_the_ui_never_talks_to_the_store():
    from pathlib import Path

    source = Path("regente/app/ui/app.js").read_text(encoding="utf-8")
    for proibido in ("sqlite", "SELECT ", "INSERT ", "store."):
        assert proibido not in source, f"a tela alcancou '{proibido}'"


def test_the_api_module_never_imports_sqlite_nor_an_adapter():
    import ast
    from pathlib import Path

    tree = ast.parse(Path("regente/app/api.py").read_text(encoding="utf-8"))
    nomes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            nomes += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            nomes.append(node.module or "")
    for nome in nomes:
        assert "sqlite" not in nome, f"a API importou {nome}"
        assert "adapters" not in nome, f"a API importou um adapter: {nome}"


# ---------------------------------------------------------------------------
# 9. A fiacao do terminal
# ---------------------------------------------------------------------------
#
# `regente decide` estava QUEBRADO. O parser recebia `opcao` e `--por`; o
# handler lia `args.option` e `args.per`. A chamada morria com `AttributeError`
# antes de tocar no store -- o unico caminho humano do sistema, inutil, desde a
# renomeacao para ingles.
#
# Ninguem viu porque todo teste chamava `store.decide_approval` diretamente. A
# fiacao de argumentos do CLI nao tinha teste nenhum, e e exatamente ali que uma
# renomeacao deixa restos: dois lados do mesmo nome, mudados em momentos
# diferentes, sem nada no meio que os compare.

def _bench_workspace(tmp_path, monkeypatch):
    """Uma configuracao real em disco, do jeito que o CLI a le."""
    from regente.app import config as config_mod

    (tmp_path / "board").mkdir(exist_ok=True)
    (tmp_path / "policies.yaml").write_text(
        "rules:\n"
        "  - name: decidir\n    effect: ALLOW\n"
        "    match: {action: 'approval.decide'}\n",
        encoding="utf-8")
    (tmp_path / "regente.yaml").write_text(
        "organization: org\nclient: acme\nworkspace: main\nautonomy: L2\n"
        "shadow: true\nroot: .regente\npolicies: policies.yaml\n"
        "providers:\n"
        "  tasks:\n    name: filesystem\n    directory: board\n"
        "  workspace_provider:\n    name: directory\n"
        "  runner:\n    name: script\n"
        "  notification:\n    name: console\n"
        "projects:\n- name: P\n  default_environment: staging\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return config_mod.load(tmp_path / "regente.yaml")


def test_the_decide_command_actually_reaches_the_engine(tmp_path, monkeypatch,
                                                        capsys):
    """A prova que faltava: rodar o comando como uma pessoa o roda.

    Nao chama a funcao interna -- chama `main(argv)`, que e onde o parser e o
    handler precisam concordar sobre o nome de cada argumento.
    """
    from regente import cli

    _bench_workspace(tmp_path, monkeypatch)
    code = cli.main(["decide", "apr_naoexiste", "seguir"])

    assert code == 1
    saida = capsys.readouterr()
    # Chegou ao Core e foi recusada POR MOTIVO, e nao por AttributeError.
    assert "NOT_FOUND" in saida.err
    assert "AttributeError" not in saida.err + saida.out


def test_the_decide_command_carries_the_note_and_never_an_identity(tmp_path,
                                                                   monkeypatch):
    """`--por` foi embora. Identidade digitada nao e identidade.

    Ela gravava na auditoria o texto que a pessoa quisesse -- o mesmo padrao que
    a API recusa no corpo do POST. Quem assina agora e a conta que roda o
    processo.
    """
    import argparse

    from regente import cli

    _bench_workspace(tmp_path, monkeypatch)
    parser = None
    try:
        cli.main(["decide"])
    except SystemExit:
        pass

    # O parser aceita `--nota` e recusa `--por`.
    with pytest.raises(SystemExit):
        cli.main(["decide", "apr_x", "seguir", "--por", "chefe"])


@pytest.mark.slow
def test_a_decision_from_the_terminal_is_attributable_to_the_account(
        tmp_path, monkeypatch, capsys):
    """O terminal atravessa as mesmas barreiras, e assina do mesmo jeito."""
    from regente import cli
    from regente.app import container
    from regente.core.risk import RiskLevel
    from regente.engine import escalation

    cfg = _bench_workspace(tmp_path, monkeypatch)
    motor = container.build(cfg)
    try:
        task = Task(id=ids.new_id(ids.TASK), workspace_id=motor.workspace.id,
                    project_id="p", title="parou", state=TaskState.READY,
                    externo=ExternalRef(provider="filesystem", key="T-1"))
        motor.store.save_task(task)
        for step in (TaskState.ASSIGNED, TaskState.WAITING_HUMAN):
            motor.store.transition(task.id, step, actor="t", reason="setup",
                                   workspace_id=motor.workspace.id)
        approval = escalation.build(
            task=motor.store.task(task.id, motor.workspace.id),
            what_happened="parou", why_it_matters="precisa de gente",
            risk=RiskLevel.MEDIUM)
        motor.store.open_approval(approval)
        workspace_id = motor.workspace.id
    finally:
        motor.close()

    code = cli.main(["decide", approval.id, "seguir", "--nota", "ok"])
    assert code == 0, capsys.readouterr()

    from regente.engine.store_sqlite import SqliteStore

    store = SqliteStore(cfg.banco)
    try:
        stored = store.approval(approval.id, workspace_id)
        assert stored.state.value == "DECIDED"
        assert stored.decided_by.startswith("terminal:"), stored.decided_by

        trail = [e for e in store.events(workspace_id, limit=50)
                 if e.kind == "decisao_humana_autenticada"]
        assert len(trail) == 1
        assert trail[0].data["method"] == "terminal"
        assert trail[0].data["previous_state"] == "OPEN"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 10. As lacunas que o sweep de mutacao encontrou
# ---------------------------------------------------------------------------

def test_resolve_without_a_principal_is_anonymous_not_the_local_operator():
    """O default de `resolve` importa, e nenhum teste o exercitava.

    Todo teste passava um principal explicito e o caminho HTTP monta o seu a
    partir do cabecalho -- entao o default nunca era usado, e trocar `ANONYMOUS`
    por um operador permissivo passava despercebido. Um default generoso numa
    fronteira e o tipo de coisa que so aparece quando ja vazou.
    """
    import tempfile
    from pathlib import Path

    from regente.app.api import Api
    from regente.engine.readmodel import ReadModel

    root = Path(tempfile.mkdtemp())
    store = SqliteStore(root / "d.db", clock=lambda: T0)
    store.migrate()
    store.save_client("cli_a", "org", "Acme")
    store.save_workspace(Workspace(id="wks_a", client_id="cli_a", name="main"))
    try:
        api = Api(read=ReadModel(store=store, clock=lambda: T0))

        vazio = api.resolve("GET", "/api/workspaces")
        assert vazio.payload["workspaces"] == [], (
            "sem principal, a API mostrou workspaces")
        negado = api.resolve("GET", "/api/workspaces/wks_a/tasks")
        assert negado.status == 404
    finally:
        store.close()


def test_the_dev_token_refuses_to_authenticate_off_the_loopback():
    """A recusa esta no codigo, e nao no README.

    Sem este teste, remover a guarda deixava o mecanismo de desenvolvimento
    autenticando requisicoes vindas da rede -- que e exatamente a situacao em
    que ele nao serve.
    """
    from regente.adapters.identity.dev_token import DevTokenIdentity

    exposto = DevTokenIdentity(operator="x", bind_is_local=False)
    assert exposto.authenticate(exposto.token) is None
    assert "FORA DO LOOPBACK" in exposto.describe()

    local = DevTokenIdentity(operator="x", bind_is_local=True)
    assert local.authenticate(local.token) is not None


def test_a_race_inside_the_core_becomes_a_conflict_and_not_a_success(bench):
    """A corrida REAL: outro decidiu entre a leitura e a escrita.

    A pre-checagem de estado cobre o caso comum, entao o `except` so e alcancado
    quando os dois processos se cruzam exatamente ali. O teste de dois processos
    chega la por acaso; este chega de proposito.
    """
    from regente.core.errors import CorruptedState

    approval = an_approval(bench)
    svc = service(bench)

    class PerdeACorrida:
        """Encaminha tudo para o store real, menos a escrita -- que perde."""

        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def decide_approval(self, *a, **kw):
            raise CorruptedState("approval ja foi decidido")

    svc.store = PerdeACorrida(bench)
    out = svc.decide(operator("wks_a"), "wks_a", approval.id, "seguir")

    assert out.accepted is False
    assert out.denial is Denial.CONFLICT
    assert bench.approval(approval.id, "wks_a").state is ApprovalState.OPEN


def test_every_denial_maps_to_a_status_that_is_not_success():
    """Nenhuma recusa pode chegar ao navegador como `2xx`.

    Um mapa incompleto cairia no default, e um default otimista transformaria
    uma recusa nova em sucesso -- silenciosamente, no dia em que alguem
    acrescentasse um motivo de recusa.
    """
    from regente.app.api import DENIAL_STATUS

    for denial in Denial:
        assert denial in DENIAL_STATUS, f"{denial.value} nao tem status"
        assert DENIAL_STATUS[denial] >= 400, (
            f"{denial.value} chega ao navegador como {DENIAL_STATUS[denial]}")


def test_the_core_write_is_always_scoped_by_workspace():
    """Defesa em profundidade, fixada estruturalmente.

    Duas barreiras anteriores ja garantem o escopo, entao remover o argumento
    daqui nao muda nenhum comportamento observavel -- e por isso nenhum teste de
    comportamento consegue peg -lo. O que se perde e a ultima linha de defesa,
    justamente a que vale quando uma das anteriores for removida.
    """
    import ast
    from pathlib import Path

    origem = Path("regente/engine/decision.py").read_text(encoding="utf-8")
    chamadas = [n for n in ast.walk(ast.parse(origem))
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "decide_approval"]
    assert chamadas, "o Core nao chama mais decide_approval"
    for chamada in chamadas:
        nomes = {k.arg for k in chamada.keywords}
        assert "workspace_id" in nomes, (
            f"linha {chamada.lineno}: escrita no Core sem escopo de workspace")
