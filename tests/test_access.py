# -*- coding: utf-8 -*-
"""Identidade, concessao, policy: tres perguntas, e nenhuma responde pela outra.

    Identity     quem e voce?              provado por um provedor
    AccessGrant  voce recebeu acesso?      concedido por alguem, e revogavel
    Policy       esta acao e permitida?    independente das duas acima
    Transicao    este estado permite?      a maquina de estados

Os testes felizes provam que as quatro estao presentes na ordem certa quando
tudo da certo -- que e exatamente quando nenhuma delas importa. O que esta aqui
ataca cada uma sozinha, com as outras satisfeitas.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from regente.core import ids
from regente.core.access import (Ability, AccessGrant, PrincipalRef,
                                 abilities_of)
from regente.core.model import ApprovalState, ExternalRef, Task, Workspace
from regente.core.policy import PolicyEngine
from regente.core.principal import ANONYMOUS, Principal
from regente.core.risk import RiskLevel
from regente.core.states import TaskState
from regente.engine import escalation
from regente.engine.access import (BOOTSTRAPPED, GRANTED, REVOKED,
                                   AccessService, Refusal)
from regente.engine.decision import DECIDE_ACTION, DecisionService, Denial
from regente.engine.store_sqlite import SqliteStore

T0 = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)

TUDO = [{"name": "acesso", "effect": "ALLOW",
         "match": {"action": ["workspace.access.grant",
                              "workspace.access.revoke",
                              "workspace.access.list", DECIDE_ACTION]}}]

#: Duas pessoas com o MESMO sujeito em provedores diferentes. Se o provedor nao
#: fizesse parte da chave, seriam a mesma pessoa dentro do motor.
ALICE = PrincipalRef("os-account", "S-1-5-21-1")
BOB = PrincipalRef("os-account", "S-1-5-21-2")
OUTRO_ALICE = PrincipalRef("dev-token", "S-1-5-21-1")


@pytest.fixture
def bench(tmp_path):
    store = SqliteStore(tmp_path / "a.db", clock=lambda: T0)
    store.migrate()
    for wid, client, nome in (("wks_a", "cli_a", "Acme"),
                              ("wks_b", "cli_b", "Beta")):
        store.save_client(client, "org", nome)
        store.save_workspace(Workspace(id=wid, client_id=client, name="main"))
    yield store
    store.close()


def service(store, rules=None) -> AccessService:
    return AccessService(
        store=store, policy=PolicyEngine.from_config(
            TUDO if rules is None else rules),
        clock=lambda: T0, organization="org", client="Acme",
        workspace_name="main")


def quem(ref: PrincipalRef, method: str = "os-account") -> Principal:
    """Uma identidade AUTENTICADA e sem autoridade nenhuma."""
    return Principal(subject=ref.subject, display=ref.subject, method=method,
                     provider=ref.provider, issuer="maquina",
                     authenticated_at=T0)


def com_acesso(store, ref: PrincipalRef, workspace: str, role: str,
               por: str = "os-account:fundador") -> Principal:
    """Concede direto no store -- o atalho e SO para montar cenario.

    Os testes de concessao usam o `AccessService`; aqui interessa o estado
    resultante, nao o caminho.
    """
    store.open_grant(AccessGrant(
        id=ids.new_id(ids.GRANT), client_id="cli_a", workspace_id=workspace,
        principal=ref, abilities=abilities_of(role), granted_by=por,
        granted_at=T0))
    return service(store).authorize(quem(ref, method=ref.provider))


def uma_escalada(store, workspace_id="wks_a", key="SAME-1"):
    task = Task(id=ids.new_id(ids.TASK), workspace_id=workspace_id,
                project_id="p", title="parou", state=TaskState.READY,
                externo=ExternalRef(provider="filesystem", key=key))
    store.save_task(task)
    for step in (TaskState.ASSIGNED, TaskState.WAITING_HUMAN):
        store.transition(task.id, step, actor="t", reason="setup",
                         workspace_id=workspace_id)
    a = escalation.build(task=store.task(task.id, workspace_id),
                         what_happened="parou", why_it_matters="precisa de gente",
                         risk=RiskLevel.MEDIUM)
    store.open_approval(a)
    return a


def decisions(store, rules=None) -> DecisionService:
    return DecisionService(
        store=store, policy=PolicyEngine.from_config(
            TUDO if rules is None else rules),
        clock=lambda: T0, organization="org", client="Acme",
        workspace_name="main")


# ---------------------------------------------------------------------------
# 1. Identidade nao e autorizacao
# ---------------------------------------------------------------------------

def test_an_authenticated_identity_with_no_grant_can_do_nothing(bench):
    """A regra central do marco, na forma mais curta possivel."""
    alice = service(bench).authorize(quem(ALICE))

    assert alice.authenticated is True
    assert dict(alice.abilities) == {}
    assert alice.may_decide("wks_a") is False
    assert alice.can("wks_a", Ability.GRANT) is False


def test_an_identity_provider_hands_out_no_authority(bench):
    """Autenticar e autorizar sao perguntas diferentes, e agora sao codigo
    diferente. Enquanto o provedor devolvia `decides`, quem editava a
    configuracao concedia a si mesmo autoridade e nada guardava esse fato."""
    from regente.adapters.identity.dev_token import DevTokenIdentity
    from regente.adapters.identity.os_account import OsAccountIdentity

    for provider in (DevTokenIdentity(operator="x"), OsAccountIdentity()):
        found = provider.authenticate(getattr(provider, "token", None))
        if found is None:
            continue
        who = provider.principal(found)
        assert who.authenticated is True
        assert dict(who.abilities) == {}, (
            f"{provider.name} concedeu autoridade ao autenticar")


def test_two_providers_with_the_same_subject_are_two_people(bench):
    """Sem o provedor na chave, uma concessao de um valeria para o outro."""
    assert ALICE.key != OUTRO_ALICE.key

    alice = com_acesso(bench, ALICE, "wks_a", "operator")
    impostor = service(bench).authorize(quem(OUTRO_ALICE, method="dev-token"))

    assert alice.may_decide("wks_a") is True
    assert impostor.may_decide("wks_a") is False


def test_an_identity_without_a_provider_cannot_be_a_key(bench):
    with pytest.raises(ValueError):
        PrincipalRef("", "alguem")
    with pytest.raises(ValueError):
        PrincipalRef("os-account", "  ")


def test_a_display_name_is_never_the_identity(bench):
    """Duas pessoas com o mesmo nome de exibicao continuam duas pessoas."""
    a = Principal(subject="S-1-5-21-1", display="Walberth", method="os-account",
                  provider="os-account")
    b = Principal(subject="S-1-5-21-2", display="Walberth", method="os-account",
                  provider="os-account")
    assert a.ref.key != b.ref.key

    com_acesso(bench, a.ref, "wks_a", "operator")
    assert service(bench).authorize(a).may_decide("wks_a") is True
    assert service(bench).authorize(b).may_decide("wks_a") is False


# ---------------------------------------------------------------------------
# 2. Quem pode conceder
# ---------------------------------------------------------------------------

def test_an_authenticated_user_cannot_grant_access_to_themselves(bench):
    """Uma concessao so vale como prova se houver DUAS pessoas na linha."""
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    out = service(bench).grant(alice, "wks_a", ALICE, "owner")

    assert out.accepted is False
    assert out.refusal is Refusal.FORBIDDEN
    assert bench.grants("wks_a", ALICE.key)[0].abilities == abilities_of("admin")


def test_a_user_without_the_grant_ability_cannot_grant(bench):
    """Ter acesso nao e ter autoridade para distribuir acesso."""
    alice = com_acesso(bench, ALICE, "wks_a", "operator")
    out = service(bench).grant(alice, "wks_a", BOB, "operator")

    assert out.refusal is Refusal.NOT_FOUND
    assert bench.grants("wks_a", BOB.key) == []


def test_an_authorised_user_grants_and_the_target_can_then_act(bench):
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    out = service(bench).grant(alice, "wks_a", BOB, "operator", note="entrou hoje")

    assert out.accepted is True
    assert out.actor != out.target
    bob = service(bench).authorize(quem(BOB))
    assert bob.may_decide("wks_a") is True
    assert bob.can("wks_a", Ability.GRANT) is False


def test_an_unknown_role_grants_nothing(bench):
    """Um papel que ninguem definiu concede nada -- nunca tudo."""
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    out = service(bench).grant(alice, "wks_a", BOB, "superusuario")

    assert out.refusal is Refusal.INVALID
    assert abilities_of("superusuario") == frozenset()
    assert bench.grants("wks_a", BOB.key) == []


def test_a_second_live_grant_for_the_same_person_conflicts(bench):
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    svc = service(bench)
    assert svc.grant(alice, "wks_a", BOB, "operator").accepted is True

    out = svc.grant(alice, "wks_a", BOB, "owner")
    assert out.refusal is Refusal.CONFLICT
    vivos = bench.grants("wks_a", BOB.key)
    assert len(vivos) == 1
    assert vivos[0].abilities == abilities_of("operator"), (
        "a segunda concessao sobrescreveu a primeira")


# ---------------------------------------------------------------------------
# 3. Bootstrap: a porta estreita
# ---------------------------------------------------------------------------

def test_the_first_grant_needs_an_operating_system_identity(bench):
    """A porta inicial nao aceita identidade de desenvolvimento."""
    de_desenvolvimento = PrincipalRef("dev-token", "operador")
    out = service(bench).bootstrap(quem(de_desenvolvimento, method="dev-token"),
                                   "wks_a")
    assert out.refusal is Refusal.FORBIDDEN
    assert bench.grants("wks_a") == []


def test_the_first_grant_works_once_and_then_closes(bench):
    svc = service(bench)
    primeiro = svc.bootstrap(quem(ALICE), "wks_a")
    assert primeiro.accepted is True

    segundo = svc.bootstrap(quem(BOB), "wks_a")
    assert segundo.refusal is Refusal.CONFLICT
    assert bench.grants("wks_a", BOB.key) == []


def test_the_first_grant_is_audited_as_what_it_is(bench):
    """`acesso_inicial`, e nao concessao comum: a procedencia e outra."""
    service(bench).bootstrap(quem(ALICE), "wks_a")
    kinds = [e.kind for e in bench.events("wks_a", limit=20)]
    assert BOOTSTRAPPED in kinds
    assert GRANTED not in kinds


def test_an_anonymous_session_cannot_bootstrap(bench):
    assert service(bench).bootstrap(ANONYMOUS, "wks_a").refusal is (
        Refusal.UNAUTHENTICATED)


# ---------------------------------------------------------------------------
# 4. Revogacao
# ---------------------------------------------------------------------------

def test_revoking_closes_the_door_for_the_same_identity(bench):
    """A prova que o marco pede: mesma identidade, mesmo workspace, negado."""
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    service(bench).grant(alice, "wks_a", BOB, "operator")
    assert service(bench).authorize(quem(BOB)).may_decide("wks_a") is True

    out = service(bench).revoke(alice, "wks_a", BOB)
    assert out.accepted is True
    assert service(bench).authorize(quem(BOB)).may_decide("wks_a") is False


def test_a_revoked_identity_cannot_decide_even_with_a_live_approval(bench):
    """A porta fecha no Core, e nao por a tela esconder um botao."""
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    service(bench).grant(alice, "wks_a", BOB, "operator")
    approval = uma_escalada(bench)

    service(bench).revoke(alice, "wks_a", BOB)
    bob = service(bench).authorize(quem(BOB))
    out = decisions(bench).decide(bob, "wks_a", approval.id, "seguir")

    assert out.denial is Denial.NOT_FOUND
    assert bench.approval(approval.id, "wks_a").state is ApprovalState.OPEN


def test_revoking_keeps_the_history_instead_of_deleting_it(bench):
    """"nunca teve acesso" e "teve e perdeu" sao fatos diferentes."""
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    service(bench).grant(alice, "wks_a", BOB, "operator")
    service(bench).revoke(alice, "wks_a", BOB)

    assert bench.grants("wks_a", BOB.key) == []
    historia = bench.grants("wks_a", BOB.key, include_revoked=True)
    assert len(historia) == 1
    g = historia[0]
    assert g.active is False
    assert g.granted_by == ALICE.key and g.revoked_by == ALICE.key
    assert g.granted_at is not None and g.revoked_at is not None


def test_revoking_someone_who_has_nothing_is_not_found(bench):
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    assert service(bench).revoke(alice, "wks_a", BOB).refusal is Refusal.NOT_FOUND


def test_revoking_one_person_does_not_touch_another(bench):
    """Contraprova: fechar a porta de B nao fecha a de A."""
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    svc = service(bench)
    svc.grant(alice, "wks_a", BOB, "operator")
    carol = PrincipalRef("os-account", "S-1-5-21-3")
    svc.grant(alice, "wks_a", carol, "operator")

    svc.revoke(alice, "wks_a", BOB)

    assert svc.authorize(quem(BOB)).may_decide("wks_a") is False
    assert svc.authorize(quem(carol)).may_decide("wks_a") is True
    # `admin` administra acesso e NAO decide escalada: capacidades sao
    # separadas de proposito, e afirmar o contrario seria afirmar um papel que
    # nao existe.
    assert alice.can("wks_a", Ability.GRANT) is True
    assert alice.may_decide("wks_a") is False


# ---------------------------------------------------------------------------
# 5. Isolamento entre workspaces e clientes
# ---------------------------------------------------------------------------

def test_a_grant_in_one_workspace_is_not_a_grant_in_another(bench):
    alice = com_acesso(bench, ALICE, "wks_a", "owner")
    assert alice.may_decide("wks_a") is True
    assert alice.may_decide("wks_b") is False
    assert alice.can("wks_b", Ability.GRANT) is False


def test_granting_in_one_workspace_creates_no_access_in_another(bench):
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    service(bench).grant(alice, "wks_a", BOB, "owner")

    bob = service(bench).authorize(quem(BOB))
    assert bob.may_decide("wks_a") is True
    assert bob.may_decide("wks_b") is False
    assert bench.grants("wks_b") == []


def test_administering_another_clients_workspace_reads_as_absent(bench):
    """Um workspace alheio nao pode ser distinguido de um inexistente."""
    alice = com_acesso(bench, ALICE, "wks_a", "owner")
    svc = service(bench)

    alheio = svc.grant(alice, "wks_b", BOB, "operator")
    fantasma = svc.grant(alice, "wks_fantasma", BOB, "operator")

    assert alheio.refusal is fantasma.refusal is Refusal.NOT_FOUND
    assert alheio.reason == fantasma.reason


def test_a_refusal_reveals_nothing_about_the_other_tenant(bench):
    alice = com_acesso(bench, ALICE, "wks_a", "owner")
    com_acesso(bench, BOB, "wks_b", "owner", por="os-account:outro")

    out = service(bench).listing(alice, "wks_b")
    dito = f"{out.reason} {out.refusal.value}"
    for vazamento in ("wks_b", "Beta", BOB.key, "S-1-5-21-2"):
        assert vazamento not in dito, f"a recusa vazou '{vazamento}'"


def test_the_same_subject_in_two_clients_stays_two_grants(bench):
    """Mesmo nome, mesmo sujeito, dois clientes: duas concessoes distintas."""
    com_acesso(bench, ALICE, "wks_a", "operator")
    bench.open_grant(AccessGrant(
        id=ids.new_id(ids.GRANT), client_id="cli_b", workspace_id="wks_b",
        principal=ALICE, abilities=abilities_of("admin"),
        granted_by="os-account:outro", granted_at=T0))

    alice = service(bench).authorize(quem(ALICE))
    assert alice.abilities["wks_a"] == abilities_of("operator")
    assert alice.abilities["wks_b"] == abilities_of("admin")
    assert [g.client_id for g in bench.grants("wks_a")] == ["cli_a"]
    assert [g.client_id for g in bench.grants("wks_b")] == ["cli_b"]


# ---------------------------------------------------------------------------
# 6. Policy: independente da concessao
# ---------------------------------------------------------------------------

def test_policy_deny_stops_an_administrator_who_has_the_ability(bench):
    """Ter a capacidade e uma condicao; a policy continua sendo outra."""
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    out = service(bench, [{"name": "nao", "effect": "DENY",
                           "match": {"action": "workspace.access.grant"}}]
                  ).grant(alice, "wks_a", BOB, "operator")

    assert out.refusal is Refusal.POLICY_DENIED
    assert bench.grants("wks_a", BOB.key) == []


def test_an_absent_policy_blocks_because_the_default_is_deny(bench):
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    out = service(bench, []).grant(alice, "wks_a", BOB, "operator")
    assert out.refusal is Refusal.POLICY_DENIED


def test_a_rule_about_a_similar_action_does_not_govern_this_one(bench):
    """`workspace.write` nao e `workspace.access.grant`."""
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    out = service(bench, [
        {"name": "outra", "effect": "ALLOW", "match": {"action": "workspace.write"}},
    ]).grant(alice, "wks_a", BOB, "operator")
    assert out.refusal is Refusal.POLICY_DENIED


def test_policy_allow_does_not_replace_the_grant(bench):
    """Permitido nao e autorizado. Policy aberta e concessao ausente = nao."""
    ninguem = quem(ALICE)
    out = service(bench).grant(ninguem, "wks_a", BOB, "operator")
    assert out.refusal is Refusal.NOT_FOUND
    assert bench.grants("wks_a", BOB.key) == []


def test_a_grant_with_the_ability_and_a_policy_deny_still_fails(bench):
    """As duas precisam passar, e a ordem nao muda o resultado."""
    alice = com_acesso(bench, ALICE, "wks_a", "owner")
    out = service(bench, [{"name": "nao", "effect": "DENY",
                           "match": {"action": "workspace.access.revoke"}}]
                  ).revoke(alice, "wks_a", ALICE)
    assert out.refusal is Refusal.POLICY_DENIED


# ---------------------------------------------------------------------------
# 7. Auditoria: ator e alvo sao coisas diferentes
# ---------------------------------------------------------------------------

def test_the_audit_separates_who_acted_from_who_was_affected(bench):
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    service(bench).grant(alice, "wks_a", BOB, "operator", note="entrou hoje")

    trilha = [e for e in bench.events("wks_a", limit=20) if e.kind == GRANTED]
    assert len(trilha) == 1
    e = trilha[0]

    assert e.data["actor"] == ALICE.key
    assert e.data["target"] == BOB.key
    assert e.data["actor"] != e.data["target"]
    assert e.data["workspace_id"] == "wks_a"
    assert e.data["client_id"] == "cli_a"
    assert e.data["abilities"] == ["approval.decide"]
    assert e.data["result"] == "ACCEPTED"
    assert e.data["grant_id"]
    assert e.ts is not None


def test_revocation_is_audited_with_both_sides_too(bench):
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    service(bench).grant(alice, "wks_a", BOB, "operator")
    service(bench).revoke(alice, "wks_a", BOB)

    e = [x for x in bench.events("wks_a", limit=20) if x.kind == REVOKED][0]
    assert e.data["actor"] == ALICE.key and e.data["target"] == BOB.key


def test_a_refused_administration_writes_no_audit_of_success(bench):
    """Recusa nao e concessao. Registrar uma sugeriria que algo foi concedido."""
    alice = com_acesso(bench, ALICE, "wks_a", "operator")
    service(bench).grant(alice, "wks_a", BOB, "operator")

    assert not [e for e in bench.events("wks_a", limit=20) if e.kind == GRANTED]


def test_the_note_is_never_persisted_verbatim(bench):
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    segredo = "ghp_naoehumtokendeverdade000"
    service(bench).grant(alice, "wks_a", BOB, "operator",
                         note=f"entrou hoje, token {segredo}")

    for e in bench.events("wks_a", limit=20):
        assert segredo not in str(e.data)
        assert segredo not in e.summary


def test_the_decision_trail_can_be_traced_back_to_a_grant(bench):
    """A cadeia que o marco pede: quem deu a essa pessoa o direito de decidir.

    Nao e preciso duplicar a concessao no evento de decisao -- basta haver
    referencias suficientes para reconstruir a relacao.
    """
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    service(bench).grant(alice, "wks_a", BOB, "operator")
    approval = uma_escalada(bench)

    bob = service(bench).authorize(quem(BOB))
    assert decisions(bench).decide(bob, "wks_a", approval.id, "seguir").accepted

    decisao = [e for e in bench.events("wks_a", limit=50)
               if e.kind == "decisao_humana_autenticada"][0]
    quem_decidiu = decisao.data["method"] + ":" + decisao.data["subject"]
    assert quem_decidiu == BOB.key

    # A partir da chave, a concessao responde quem autorizou.
    concessao = bench.grants("wks_a", quem_decidiu, include_revoked=True)[0]
    assert concessao.granted_by == ALICE.key
    assert Ability.DECIDE in concessao.abilities


# ---------------------------------------------------------------------------
# 8. Concorrencia
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_two_processes_granting_at_once_produce_exactly_one_grant(tmp_path):
    """Dois administradores, o mesmo alvo, ao mesmo tempo.

    Processos de verdade: a trava que vale e o indice unico dentro da
    transacao, e uma prova feita com threads poderia passar por um detalhe do
    interpretador em vez de pelo banco.
    """
    import json
    import subprocess
    import sys
    from pathlib import Path

    store = SqliteStore(tmp_path / "race.db", clock=lambda: T0)
    store.migrate()
    store.save_client("cli_a", "org", "Acme")
    store.save_workspace(Workspace(id="wks_a", client_id="cli_a", name="main"))
    com_acesso(store, ALICE, "wks_a", "admin")
    com_acesso(store, PrincipalRef("os-account", "S-1-5-21-9"), "wks_a", "admin")
    store.close()

    harness = str(Path(__file__).with_name("access_race.py"))
    procs = [subprocess.Popen(
        [sys.executable, harness, str(tmp_path), ator, papel],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for ator, papel in (("S-1-5-21-1", "operator"),
                            ("S-1-5-21-9", "owner"))]
    saidas = []
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err.decode(errors="replace")
        saidas.append(json.loads(out.decode().strip().splitlines()[-1]))

    aceitas = [s for s in saidas if s["accepted"]]
    assert len(aceitas) == 1, f"nem uma nem duas: {saidas}"
    assert [s for s in saidas if not s["accepted"]][0]["refusal"] == "CONFLICT"

    reaberto = SqliteStore(tmp_path / "race.db")
    try:
        vivos = reaberto.grants("wks_a", BOB.key)
        assert len(vivos) == 1
    finally:
        reaberto.close()


def test_a_grant_is_effective_immediately_for_the_next_request(bench):
    """Conceder e decidir em seguida: a autoridade vale na proxima montagem."""
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    approval = uma_escalada(bench)
    service(bench).grant(alice, "wks_a", BOB, "operator")

    bob = service(bench).authorize(quem(BOB))
    assert decisions(bench).decide(bob, "wks_a", approval.id, "seguir").accepted


def test_a_principal_built_before_a_revocation_is_stale_and_rebuilt(bench):
    """Um principal em memoria nao e a fonte da verdade.

    Ele e uma fotografia do momento em que foi montado. Revogar nao o altera --
    e por isso a autorizacao e refeita a cada requisicao, e nao guardada.
    """
    alice = com_acesso(bench, ALICE, "wks_a", "admin")
    service(bench).grant(alice, "wks_a", BOB, "operator")
    antigo = service(bench).authorize(quem(BOB))
    assert antigo.may_decide("wks_a") is True

    service(bench).revoke(alice, "wks_a", BOB)

    assert antigo.may_decide("wks_a") is True, (
        "a fotografia mudou sozinha; ela nao deveria")
    novo = service(bench).authorize(quem(BOB))
    assert novo.may_decide("wks_a") is False


# ---------------------------------------------------------------------------
# 9. Nada de atalho
# ---------------------------------------------------------------------------

def _somente_codigo(arquivo) -> str:
    """O arquivo sem comentario e sem docstring.

    Documentacao PRECISA poder citar a armadilha -- e justamente onde a regra
    fica registrada. O que nao pode e o nome virar codigo. A guarda de fronteira
    do projeto ja separa as duas coisas assim; repetir o criterio aqui evita que
    uma prosa honesta seja acusada de defeito.
    """
    import ast

    texto = arquivo.read_text(encoding="utf-8")
    prosa: set[int] = set()
    for n, linha in enumerate(texto.splitlines(), 1):
        if linha.lstrip().startswith("#"):
            prosa.add(n)
    portadores = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for no in ast.walk(ast.parse(texto, filename=str(arquivo))):
        if not isinstance(no, portadores) or not no.body:
            continue
        primeiro = no.body[0]
        if (isinstance(primeiro, ast.Expr)
                and isinstance(primeiro.value, ast.Constant)
                and isinstance(primeiro.value.value, str)):
            prosa.update(range(primeiro.lineno,
                               (primeiro.end_lineno or primeiro.lineno) + 1))
    return "\n".join(l.split("#", 1)[0]
                     for n, l in enumerate(texto.splitlines(), 1)
                     if n not in prosa)

def test_no_role_check_bypasses_the_policy():
    """Papel e ENTRADA da policy, nunca substituto dela.

    Procura, no codigo-fonte, a forma classica do atalho: um teste de papel que
    conclui a autorizacao sozinho.
    """
    import re
    from pathlib import Path

    # Padroes que significam "a autorizacao terminou AQUI, por causa de um
    # papel". A palavra `bypass` sozinha nao serve: ela aparece na lista de
    # flags que o sandbox RECUSA, que e o oposto de um atalho -- e um guard que
    # acusa uma defesa ensina a ignorar o guard.
    padroes = [
        r"is_admin",
        r"role\s*==\s*[\"']",
        r"bypass_polic|skip_polic|ignore_polic",
        r"if\s+[a-z_.]*\.admin",
    ]
    for arquivo in sorted(Path("regente").rglob("*.py")):
        # Comentario e docstring podem citar a armadilha; codigo nao.
        codigo = _somente_codigo(arquivo)
        for padrao in padroes:
            assert not re.search(padrao, codigo, re.IGNORECASE), (
                f"{arquivo}: atalho de autorizacao por papel ({padrao})")


def test_only_the_core_writes_a_grant():
    """Uma segunda funcao de concessao e a que um dia esquece uma barreira."""
    import ast
    from pathlib import Path

    permitido = {"regente/engine/access.py", "regente/engine/store_sqlite.py"}
    culpados = []
    for path in sorted(Path("regente").rglob("*.py")):
        if path.as_posix() in permitido:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("open_grant", "revoke_grant")):
                culpados.append(f"{path.as_posix()}:{node.lineno}")
    assert not culpados, ("concessao escrita fora do Core:\n  "
                          + "\n  ".join(culpados))


def test_the_api_never_writes_a_grant_itself():
    import ast
    from pathlib import Path

    tree = ast.parse(Path("regente/app/api.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert not node.func.attr.startswith(("open_grant", "revoke_grant")), (
                f"a API escreveu concessao na linha {node.lineno}")


def test_no_identity_provider_grants_authority():
    """A propriedade estrutural do marco, lida no codigo-fonte.

    Um provedor que devolvesse `abilities` recriaria exatamente o defeito que
    este marco corrigiu -- e a revisao de codigo veria uma linha plausivel.
    """
    import ast
    from pathlib import Path

    for arquivo in sorted(Path("regente/adapters/identity").rglob("*.py")):
        for node in ast.walk(ast.parse(arquivo.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "Principal":
                    nomes = {k.arg for k in node.keywords}
                    assert "abilities" not in nomes, (
                        f"{arquivo}:{node.lineno}: provedor de identidade "
                        f"concedeu autoridade")


def test_the_core_never_names_an_identity_vendor():
    """Trocar o provedor nao pode exigir mudanca em core, ports ou engine."""
    import re
    from pathlib import Path

    # `entra` fora da lista: e um verbo comum em portugues, e um guard que
    # acusa "entra na fila" ensina a ignorar o guard. O nome do produto aparece
    # como `entra id`, que a busca por palavra composta pegaria -- e nao vale a
    # pena trocar um falso positivo garantido por um falso negativo improvavel.
    fornecedores = ["oidc", "oauth", "saml", "okta", "auth0", "keycloak",
                    "ldap", "jwt"]
    faltas = []
    for pasta in ("core", "ports", "engine"):
        for arquivo in sorted((Path("regente") / pasta).rglob("*.py")):
            codigo = _somente_codigo(arquivo)
            for nome in fornecedores:
                if re.search(rf"\b{nome}\b", codigo, re.IGNORECASE):
                    faltas.append(f"{pasta}/{arquivo.name} menciona '{nome}'")
    assert not faltas, "protocolo de identidade vazou para dentro:\n  " + \
        "\n  ".join(faltas)


def test_a_substitute_provider_needs_no_change_above_the_adapter(bench):
    """A prova de substituicao que o item 2 pede.

    Um provedor inventado aqui, com outro nome e outro emissor, atravessa o
    mesmo caminho sem que nada em `core/`, `ports/` ou `engine/` saiba dele.
    """
    from regente.ports.identity import Identity, IdentityProvider

    class ProvedorQualquer(IdentityProvider):
        name = "diretorio-imaginario"
        development_only = False

        def verify(self) -> None:
            return None

        def authenticate(self, credential):
            if credential != "segredo-certo":
                return None
            return Identity(subject="u-99", display="Pessoa", method=self.name,
                            provider=self.name, issuer="issuer.invalido")

        def principal(self, identity):
            return Principal(subject=identity.subject, display=identity.display,
                             method=identity.method, provider=identity.provider,
                             issuer=identity.issuer)

    provedor = ProvedorQualquer()
    assert provedor.authenticate("errado") is None

    quem_e = provedor.principal(provedor.authenticate("segredo-certo"))
    assert quem_e.ref.key == "diretorio-imaginario:u-99"
    assert dict(quem_e.abilities) == {}

    com_acesso(bench, quem_e.ref, "wks_a", "operator")
    autorizado = service(bench).authorize(quem_e)
    assert autorizado.may_decide("wks_a") is True

    approval = uma_escalada(bench)
    assert decisions(bench).decide(autorizado, "wks_a", approval.id,
                                   "seguir").accepted is True


def test_a_row_written_straight_into_the_database_still_needs_the_policy(bench):
    """Escrever direto no banco concede a capacidade, e nao a acao.

    Quem tem o arquivo do banco pode inserir a linha que quiser -- e nao ha
    defesa contra isso num SQLite local. O que NAO pode acontecer e essa linha
    pular as outras barreiras: a policy continua sendo consultada.
    """
    bench._con.execute(
        "INSERT INTO access_grants(id, client_id, workspace_id, principal_key,"
        " principal_provider, principal_subject, abilities, granted_by,"
        " granted_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("grn_forjado", "cli_a", "wks_a", BOB.key, BOB.provider, BOB.subject,
         '["workspace.access.grant"]', "ninguem", "2026-09-10T09:00:00.000000Z"))

    bob = service(bench).authorize(quem(BOB))
    assert bob.can("wks_a", Ability.GRANT) is True, "a linha vale como concessao"

    negado = service(bench, [{"name": "nao", "effect": "DENY",
                              "match": {"action": "workspace.access.grant"}}]
                     ).grant(bob, "wks_a", ALICE, "operator")
    assert negado.refusal is Refusal.POLICY_DENIED, (
        "uma linha forjada pulou a policy")


def test_removing_the_identity_provider_does_not_fall_back_to_dev_token():
    """Sem provedor, ninguem entra. Cair para o dev-token seria um fallback de
    autoridade -- o modo mais silencioso de perder uma fronteira."""
    from regente.app.api import Api, handler_for
    from regente.engine.readmodel import ReadModel

    class SemStore:
        def workspaces(self):
            return []

    api = Api(read=ReadModel(store=SemStore()))
    handler = handler_for(api, identity=None, fallback=None)
    assert handler is not None

    import inspect

    fonte = inspect.getsource(handler)
    assert "DevToken" not in fonte and "dev-token" not in fonte, (
        "o caminho HTTP conhece o dev-token por nome")
    assert "ANONYMOUS" in fonte


# ---------------------------------------------------------------------------
# 10. As lacunas que o sweep de mutacao encontrou
# ---------------------------------------------------------------------------

def test_an_anonymous_actor_is_told_it_is_unauthenticated(bench):
    """`UNAUTHENTICATED` e `NOT_FOUND` mandam a pessoa fazer coisas diferentes.

    Sem este teste, remover a checagem de autenticacao ainda produzia recusa --
    pela checagem de capacidade, que tambem exige autenticado -- e a diferenca
    entre "faca login" e "isto nao existe" desaparecia sem nada acusar.
    """
    for acao in ("listar", "conceder", "revogar"):
        if acao == "listar":
            out = service(bench).listing(ANONYMOUS, "wks_a")
        elif acao == "conceder":
            out = service(bench).grant(ANONYMOUS, "wks_a", BOB, "operator")
        else:
            out = service(bench).revoke(ANONYMOUS, "wks_a", BOB)
        assert out.refusal is Refusal.UNAUTHENTICATED, acao


def test_a_revoked_grant_permits_nothing_even_asked_directly(bench):
    """A pergunta feita ao proprio objeto, e nao so a consulta que o filtra.

    `AccessGrant.allows` era codigo morto: nada o chamava, entao remover a
    checagem de revogacao dele nao mudava nada. Codigo morto que parece
    autoritativo e pior que codigo ausente -- o proximo a usa-lo confia nele.
    """
    concedida = AccessGrant(
        id="grn_1", client_id="cli_a", workspace_id="wks_a", principal=BOB,
        abilities=abilities_of("owner"), granted_by=ALICE.key, granted_at=T0)
    assert concedida.allows(Ability.DECIDE) is True

    revogada = AccessGrant(
        id="grn_1", client_id="cli_a", workspace_id="wks_a", principal=BOB,
        abilities=abilities_of("owner"), granted_by=ALICE.key, granted_at=T0,
        revoked_by=ALICE.key, revoked_at=T0)
    assert revogada.active is False
    assert revogada.allows(Ability.DECIDE) is False
    assert revogada.allows(Ability.GRANT) is False


def test_a_revoked_row_never_contributes_an_ability(bench):
    """A segunda linha de defesa, exercitada sozinha.

    A consulta ja filtra revogadas. Isto prova que, mesmo se ela deixasse uma
    passar, a montagem do alcance nao a usaria.
    """
    bench.open_grant(AccessGrant(
        id=ids.new_id(ids.GRANT), client_id="cli_a", workspace_id="wks_a",
        principal=BOB, abilities=abilities_of("owner"),
        granted_by=ALICE.key, granted_at=T0, revoked_by=ALICE.key,
        revoked_at=T0))

    assert service(bench).abilities_for(BOB) == {}


def test_revoking_with_the_wrong_workspace_revokes_nothing(bench):
    """A guarda do proprio store, sem passar pelas barreiras acima.

    O servico ja confere o workspace antes de chamar. Isto prova a ultima linha
    de defesa -- a que vale no dia em que uma das anteriores for removida.
    """
    bench.open_grant(AccessGrant(
        id=ids.new_id(ids.GRANT), client_id="cli_a", workspace_id="wks_a",
        principal=BOB, abilities=abilities_of("operator"),
        granted_by=ALICE.key, granted_at=T0))

    assert bench.revoke_grant("wks_b", BOB.key, revoked_by="x") is False
    assert bench.grants("wks_a", BOB.key)[0].active is True

    assert bench.revoke_grant("wks_a", BOB.key, revoked_by="x") is True
    assert bench.grants("wks_a", BOB.key) == []


def test_a_grant_naming_a_workspace_that_vanished_reveals_nothing(bench):
    """O caso que alcanca a ULTIMA checagem de escopo.

    Com capacidade num workspace que nao existe mais, a recusa vem da linha que
    confere a existencia -- e ela precisa responder igual a "nao e seu". Sem
    este teste, trocar essa resposta por uma que nomeia o workspace passava.
    """
    bench.open_grant(AccessGrant(
        id=ids.new_id(ids.GRANT), client_id="cli_a", workspace_id="wks_sumiu",
        principal=ALICE, abilities=abilities_of("owner"),
        granted_by="os-account:fundador", granted_at=T0))
    alice = service(bench).authorize(quem(ALICE))
    assert alice.can("wks_sumiu", Ability.GRANT) is True

    sumido = service(bench).grant(alice, "wks_sumiu", BOB, "operator")
    alheio = service(bench).grant(alice, "wks_b", BOB, "operator")

    assert sumido.refusal is alheio.refusal is Refusal.NOT_FOUND
    assert sumido.reason == alheio.reason
    assert "wks_sumiu" not in sumido.reason


def test_the_operating_system_subject_is_the_stable_id_not_the_name(bench):
    """O sujeito e o identificador que o sistema emite, nunca o nome.

    Um nome muda -- e uma concessao amarrada a algo que muda se transfere
    sozinha para quem herdar o nome.
    """
    from regente.adapters.identity.os_account import (OsAccountIdentity,
                                                      current_account)

    conta = current_account()
    if conta is None:
        pytest.skip("este sistema nao respondeu qual conta roda o processo")
    identificador, nome, emissor = conta

    found = OsAccountIdentity().authenticate(None)
    assert found.subject == identificador
    assert found.issuer == emissor
    if nome and nome != identificador:
        assert found.subject != nome, (
            "o nome de exibicao virou identidade")
        assert found.display == nome


def test_a_refused_administration_is_not_recorded_as_accepted(bench):
    """Recusa nao vira registro de aceite.

    O campo `result` existe para que uma trilha futura possa registrar tambem o
    que foi negado. Enquanto ela so grava aceites, o valor precisa ser
    verdadeiro para TODO evento que existe -- e nenhum evento pode existir para
    uma operacao que nao aconteceu.
    """
    alice = com_acesso(bench, ALICE, "wks_a", "operator")   # sem GRANT
    service(bench).grant(alice, "wks_a", BOB, "operator")

    eventos = [e for e in bench.events("wks_a", limit=20)
               if e.kind in (GRANTED, REVOKED, BOOTSTRAPPED)]
    assert eventos == []
    for e in bench.events("wks_a", limit=20):
        assert e.data.get("result") != "ACCEPTED" or e.kind not in (
            GRANTED, REVOKED, BOOTSTRAPPED)
