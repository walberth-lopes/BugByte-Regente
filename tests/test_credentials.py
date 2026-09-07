# -*- coding: utf-8 -*-
"""Credencial: a autoridade de usar um segredo, e nunca o segredo.

    Identity     quem e voce?                provado por um provedor
    AccessGrant  voce manda neste workspace? concessao gravada (M14)
    Credential   este segredo pode ser usado aqui, para isto, agora?
    Policy       a organizacao permite?      independente das tres acima
    SecretRef    onde o material vive        um endereco, nunca um valor

O que se ataca aqui e a fronteira entre autoridade e material. Um teste que so
prova o caminho feliz prova que as camadas existem quando tudo da certo -- que e
exatamente quando nenhuma delas importa.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from regente.core import ids
from regente.core.access import Ability, AccessGrant, PrincipalRef, abilities_of
from regente.core.credential import (Credential, SecretRef, Status, Use,
                                     uses_from)
from regente.core.model import Workspace
from regente.core.policy import PolicyEngine
from regente.core.principal import ANONYMOUS, Principal
from regente.engine.credentials import (DENIED, REGISTERED, RESOLVED, REVOKED,
                                        USED, CredentialOutcome,
                                        CredentialService, Reach, Refusal,
                                        Resolved)
from regente.engine.store_sqlite import SqliteStore
from regente.ports import AdapterError

T0 = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
SEGREDO = "gho_estenaoehumtokendeverdade00000000000"

TUDO = [{"name": "cred", "effect": "ALLOW",
         "match": {"action": ["workspace.credential.grant",
                              "workspace.credential.revoke",
                              "workspace.credential.list",
                              "repo.read", "repo.push", "task.read"]}}]

ALICE = PrincipalRef("os-account", "S-1-5-21-1")
BOB = PrincipalRef("os-account", "S-1-5-21-2")


class FonteFake:
    """Uma fonte de segredo. Devolve material, ou explica por que nao pode."""

    def __init__(self, valores=None, quebrada=False):
        self.valores = valores or {"env:TOKEN": SEGREDO}
        self.quebrada = quebrada
        self.pedidos = []

    def resolve(self, reference: str) -> str:
        self.pedidos.append(reference)
        if self.quebrada:
            raise AdapterError("a fonte de segredo nao respondeu")
        if reference not in self.valores:
            raise AdapterError(f"referencia desconhecida: {reference}")
        return self.valores[reference]


@pytest.fixture
def bench(tmp_path):
    store = SqliteStore(tmp_path / "c.db", clock=lambda: T0)
    store.migrate()
    for wid, client, nome in (("wks_a", "cli_a", "Acme"),
                              ("wks_b", "cli_b", "Beta")):
        store.save_client(client, "org", nome)
        store.save_workspace(Workspace(id=wid, client_id=client, name="main"))
    yield store
    store.close()


def service(store, rules=None, fonte=None, at=None) -> CredentialService:
    return CredentialService(
        store=store,
        policy=PolicyEngine.from_config(TUDO if rules is None else rules),
        secrets=FonteFake() if fonte is None else fonte,
        clock=(lambda: at) if at else (lambda: T0),
        organization="org", client="Acme", workspace_name="main")


def quem(ref: PrincipalRef, workspace="wks_a", role="owner") -> Principal:
    """Identidade autenticada COM concessao humana do marco 14."""
    return Principal(
        subject=ref.subject, display=ref.subject, method=ref.provider,
        provider=ref.provider, issuer="maquina", authenticated_at=T0,
        abilities={workspace: abilities_of(role)})


def uma_credencial(store, workspace="wks_a", name="principal",
                   provider="repository", capabilities=("repo.read",),
                   ref="env:TOKEN", expires_at=None, revogada=False,
                   client="cli_a") -> Credential:
    c = Credential(
        id=ids.new_id(ids.CREDENTIAL), client_id=client,
        workspace_id=workspace, name=name, provider=provider, kind="token",
        secret_ref=SecretRef.parse(ref), capabilities=uses_from(capabilities),
        granted_by=ALICE.key, granted_at=T0, expires_at=expires_at,
        revoked_by=(ALICE.key if revogada else ""),
        revoked_at=(T0 if revogada else None))
    store.open_credential(c)
    return c


# ---------------------------------------------------------------------------
# 1. O material nunca sobe
# ---------------------------------------------------------------------------

def test_the_secret_never_appears_in_a_repr_or_an_exception(bench):
    """Um `print` de depuracao e um traceback sao onde segredo vaza primeiro."""
    r = Resolved(credential_id="crd_1", provider="repository",
                 use=Use.REPO_READ, _material=SEGREDO)

    for texto in (repr(r), str(r), f"{r}", "%s" % (r,)):
        assert SEGREDO not in texto
        assert "oculto" in texto

    try:
        raise AdapterError(f"falhou usando {r}")
    except AdapterError as e:
        assert SEGREDO not in str(e)


def test_the_material_leaves_once_and_then_the_door_closes(bench):
    r = Resolved(credential_id="crd_1", provider="repository",
                 use=Use.REPO_READ, _material=SEGREDO)
    assert r.use_secret() == SEGREDO
    with pytest.raises(AdapterError, match="ja foi entregue"):
        r.use_secret()


def test_no_audit_event_ever_carries_the_material(bench):
    """A varredura forte: toda linha de todo evento, procurando o valor."""
    uma_credencial(bench)
    svc = service(bench)
    saida = svc.resolve(quem(ALICE), "wks_a", "repository", Use.REPO_READ)
    assert isinstance(saida, Resolved)
    svc.record_use(quem(ALICE), saida.credential_id, "wks_a", Use.REPO_READ,
                   ok=True, detail="leitura")

    for e in bench.events("wks_a", limit=50):
        texto = f"{e.summary} {e.data}"
        assert SEGREDO not in texto, f"o material vazou em {e.kind}"
    # O ENDERECO entra, e deve: e onde procurar, nao o que foi achado.
    assert any("env:TOKEN" in str(e.data) for e in bench.events("wks_a", limit=50))


def test_nothing_in_the_whole_database_carries_the_material(bench):
    uma_credencial(bench)
    service(bench).resolve(quem(ALICE), "wks_a", "repository", Use.REPO_READ)

    vazamentos = []
    for (t,) in bench._con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in bench._con.execute(f"SELECT * FROM {t}"):
            texto = " ".join(str(v) for v in tuple(row) if v is not None)
            if SEGREDO in texto:
                vazamentos.append(t)
    assert not vazamentos, f"material secreto no banco: {vazamentos}"


def test_a_literal_reference_cannot_even_be_constructed(bench):
    """Se existisse, o primeiro segredo de producao iria para o YAML."""
    with pytest.raises(ValueError, match="literal"):
        SecretRef.parse(f"literal:{SEGREDO}")


# ---------------------------------------------------------------------------
# 2. Estado: expirada, revogada, sem capacidade
# ---------------------------------------------------------------------------

def test_a_revoked_credential_is_denied_and_says_which(bench):
    uma_credencial(bench, revogada=True)
    out = service(bench).resolve(quem(ALICE), "wks_a", "repository",
                                 Use.REPO_READ)

    assert isinstance(out, CredentialOutcome)
    assert out.refusal is Refusal.REVOKED
    assert "revogada" in out.reason


def test_an_expired_credential_is_denied_and_is_not_the_same_as_revoked(bench):
    """Renovar e conversar com quem revogou sao acoes diferentes."""
    uma_credencial(bench, expires_at=T0 - timedelta(seconds=1))
    out = service(bench).resolve(quem(ALICE), "wks_a", "repository",
                                 Use.REPO_READ)

    assert out.refusal is Refusal.EXPIRED
    assert out.refusal is not Refusal.REVOKED


def test_the_exact_instant_of_expiry_is_already_expired(bench):
    """O limite pertence ao lado de fora, e a escolha e explicita."""
    vence = T0 + timedelta(hours=1)
    c = uma_credencial(bench, expires_at=vence)

    assert c.status(vence - timedelta(seconds=1)) is Status.ACTIVE
    assert c.status(vence) is Status.EXPIRED
    assert c.status(vence + timedelta(seconds=1)) is Status.EXPIRED


def test_revoked_wins_over_expired(bench):
    """Para quem investiga, o fato relevante e a decisao, nao o relogio."""
    c = Credential(
        id="crd_1", client_id="cli_a", workspace_id="wks_a", name="x",
        provider="repository", kind="token",
        secret_ref=SecretRef.parse("env:TOKEN"),
        capabilities=uses_from(["repo.read"]), granted_by=ALICE.key,
        granted_at=T0, expires_at=T0, revoked_by=ALICE.key, revoked_at=T0)
    assert c.status(T0 + timedelta(days=1)) is Status.REVOKED


def test_a_read_credential_is_never_a_push_credential(bench):
    """provider capability != credential capability != policy authority."""
    uma_credencial(bench, capabilities=("repo.read",))
    svc = service(bench)

    assert isinstance(svc.resolve(quem(ALICE), "wks_a", "repository",
                                  Use.REPO_READ), Resolved)
    negado = svc.resolve(quem(ALICE), "wks_a", "repository", Use.REPO_PUSH)
    assert negado.refusal is Refusal.NO_CAPABILITY
    assert "repo.read" in negado.reason


def test_a_credential_without_any_capability_is_refused_at_registration(bench):
    out = service(bench).register(
        quem(ALICE), "wks_a", name="vazia", provider="repository",
        secret_ref="env:TOKEN", capabilities=["inventada"])
    assert out.refusal is Refusal.INVALID
    assert bench.credentials("wks_a") == []


# ---------------------------------------------------------------------------
# 3. Identidade, acesso e policy continuam valendo
# ---------------------------------------------------------------------------

def test_an_unauthenticated_request_resolves_nothing(bench):
    uma_credencial(bench)
    out = service(bench).resolve(ANONYMOUS, "wks_a", "repository",
                                 Use.REPO_READ)
    assert out.refusal is Refusal.UNAUTHENTICATED


def test_an_identity_without_access_to_the_workspace_resolves_nothing(bench):
    """Autenticado nao e autorizado, e o marco 14 continua valendo."""
    uma_credencial(bench)
    forasteiro = Principal(subject="x", method="os-account",
                           provider="os-account")
    out = service(bench).resolve(forasteiro, "wks_a", "repository",
                                 Use.REPO_READ)
    assert out.refusal is Refusal.NOT_FOUND


def test_policy_deny_stops_a_credential_that_is_otherwise_valid(bench):
    """Autoridade independente: valida, autorizada, e ainda assim proibida."""
    uma_credencial(bench)
    out = service(bench, [{"name": "nao", "effect": "DENY",
                           "match": {"action": "repo.read"}}]
                  ).resolve(quem(ALICE), "wks_a", "repository", Use.REPO_READ)
    assert out.refusal is Refusal.POLICY_DENIED


def test_an_absent_policy_blocks_because_the_default_is_deny(bench):
    uma_credencial(bench)
    out = service(bench, []).resolve(quem(ALICE), "wks_a", "repository",
                                     Use.REPO_READ)
    assert out.refusal is Refusal.POLICY_DENIED


def test_registering_needs_the_keeper_ability_not_just_access(bench):
    """Quem decide escalada nao ganha, por tabela, o direito de apontar para
    onde os segredos do cliente vivem."""
    operador = quem(ALICE, role="operator")
    assert operador.can("wks_a", Ability.DECIDE) is True

    out = service(bench).register(
        operador, "wks_a", name="x", provider="repository",
        secret_ref="env:TOKEN", capabilities=["repo.read"])
    assert out.refusal is Refusal.NOT_FOUND
    assert bench.credentials("wks_a") == []


# ---------------------------------------------------------------------------
# 4. A fonte de segredo
# ---------------------------------------------------------------------------

def test_an_unavailable_source_is_a_refusal_and_never_a_pass(bench):
    """Nao saber NAO e permissao -- e uma recusa com nome proprio."""
    uma_credencial(bench)
    out = service(bench, fonte=FonteFake(quebrada=True)).resolve(
        quem(ALICE), "wks_a", "repository", Use.REPO_READ)

    assert isinstance(out, CredentialOutcome)
    assert out.refusal is Refusal.SOURCE_UNAVAILABLE
    assert out.refusal not in (Refusal.EXPIRED, Refusal.REVOKED)


def test_no_source_configured_is_also_a_refusal(bench):
    uma_credencial(bench)
    svc = service(bench)
    svc.secrets = None
    out = svc.resolve(quem(ALICE), "wks_a", "repository", Use.REPO_READ)
    assert out.refusal is Refusal.SOURCE_UNAVAILABLE


def test_the_source_is_only_asked_after_every_barrier_passed(bench):
    """A ordem, provada por quem NAO foi perguntado.

    Uma fonte consultada antes da policy ja teria trazido o material para dentro
    do processo -- e a recusa posterior nao o tiraria de la.
    """
    uma_credencial(bench, revogada=True)
    fonte = FonteFake()
    service(bench, fonte=fonte).resolve(quem(ALICE), "wks_a", "repository",
                                        Use.REPO_READ)
    assert fonte.pedidos == [], "a fonte foi consultada antes das barreiras"

    uma_credencial(bench, name="viva")
    service(bench, fonte=fonte).resolve(quem(ALICE), "wks_a", "repository",
                                        Use.REPO_READ)
    assert fonte.pedidos == ["env:TOKEN"]


def test_policy_denial_also_happens_before_the_source_is_asked(bench):
    uma_credencial(bench)
    fonte = FonteFake()
    service(bench, [{"name": "nao", "effect": "DENY",
                     "match": {"action": "repo.read"}}], fonte=fonte).resolve(
        quem(ALICE), "wks_a", "repository", Use.REPO_READ)
    assert fonte.pedidos == []


# ---------------------------------------------------------------------------
# 5. Isolamento entre clientes e workspaces
# ---------------------------------------------------------------------------

def test_the_same_name_and_provider_in_two_clients_are_two_credentials(bench):
    """Mesmo nome, mesmo provider, mesma capacidade, dois clientes."""
    uma_credencial(bench, workspace="wks_a", client="cli_a", ref="env:A")
    uma_credencial(bench, workspace="wks_b", client="cli_b", ref="env:B")

    fonte = FonteFake({"env:A": "material-de-A", "env:B": "material-de-B"})
    a = service(bench, fonte=fonte).resolve(
        quem(ALICE, "wks_a"), "wks_a", "repository", Use.REPO_READ)
    b = service(bench, fonte=fonte).resolve(
        quem(BOB, "wks_b"), "wks_b", "repository", Use.REPO_READ)

    assert a.use_secret() == "material-de-A"
    assert b.use_secret() == "material-de-B"


def test_a_principal_of_one_workspace_never_resolves_the_other(bench):
    uma_credencial(bench, workspace="wks_b", client="cli_b", ref="env:B")
    out = service(bench).resolve(quem(ALICE, "wks_a"), "wks_b", "repository",
                                 Use.REPO_READ)
    assert out.refusal is Refusal.NOT_FOUND


def test_revoking_in_one_workspace_does_not_touch_the_other(bench):
    a = uma_credencial(bench, workspace="wks_a", client="cli_a")
    b = uma_credencial(bench, workspace="wks_b", client="cli_b")

    service(bench).revoke(quem(ALICE, "wks_a"), "wks_a", a.id)

    assert bench.credentials("wks_a") == []
    assert [c.id for c in bench.credentials("wks_b")] == [b.id]
    assert isinstance(service(bench).resolve(
        quem(BOB, "wks_b"), "wks_b", "repository", Use.REPO_READ), Resolved)


def test_a_credential_id_from_another_tenant_cannot_be_revoked(bench):
    outra = uma_credencial(bench, workspace="wks_b", client="cli_b")
    out = service(bench).revoke(quem(ALICE, "wks_a"), "wks_a", outra.id)

    assert out.refusal is Refusal.NOT_FOUND
    assert bench.credentials("wks_b")[0].active is True


def test_a_refusal_reveals_nothing_about_the_other_tenant(bench):
    uma_credencial(bench, workspace="wks_b", client="cli_b", name="segredo-b")
    out = service(bench).listing(quem(ALICE, "wks_a"), "wks_b")

    dito = f"{out.reason} {out.refusal.value}"
    for vazamento in ("wks_b", "Beta", "segredo-b"):
        assert vazamento not in dito


# ---------------------------------------------------------------------------
# 6. Auditoria
# ---------------------------------------------------------------------------

def test_every_step_writes_its_own_verb(bench):
    """Registrada, resolvida, usada, negada, revogada: cinco fatos diferentes."""
    svc = service(bench)
    svc.register(quem(ALICE), "wks_a", name="p", provider="repository",
                 secret_ref="env:TOKEN", capabilities=["repo.read"])
    resolvida = svc.resolve(quem(ALICE), "wks_a", "repository", Use.REPO_READ)
    svc.record_use(quem(ALICE), resolvida.credential_id, "wks_a",
                   Use.REPO_READ, ok=True)
    svc.resolve(quem(ALICE), "wks_a", "repository", Use.REPO_PUSH)
    svc.revoke(quem(ALICE), "wks_a", resolvida.credential_id)

    kinds = [e.kind for e in bench.events("wks_a", limit=50)]
    for verbo in (REGISTERED, RESOLVED, USED, DENIED, REVOKED):
        assert verbo in kinds, f"faltou {verbo}"


def test_the_audit_names_the_actor_the_workspace_and_the_use(bench):
    uma_credencial(bench)
    service(bench).resolve(quem(ALICE), "wks_a", "repository", Use.REPO_READ)

    e = [x for x in bench.events("wks_a", limit=20) if x.kind == RESOLVED][0]
    assert e.data["actor"] == ALICE.key
    assert e.data["workspace_id"] == "wks_a"
    assert e.data["client_id"] == "cli_a"
    assert e.data["provider"] == "repository"
    assert e.data["use"] == "repo.read"
    assert e.data["secret_ref"] == "env:TOKEN"
    assert e.data["result"] == "ACCEPTED"


def test_a_denial_is_audited_too(bench):
    """Uma negativa silenciosa nao ajuda ninguem depois."""
    uma_credencial(bench, revogada=True)
    service(bench).resolve(quem(ALICE), "wks_a", "repository", Use.REPO_READ)

    negados = [e for e in bench.events("wks_a", limit=20) if e.kind == DENIED]
    assert len(negados) == 1
    assert negados[0].data["refusal"] == "REVOKED"
    assert negados[0].data["result"] == "DENIED"


# ---------------------------------------------------------------------------
# 7. Teste de conexao: quatro fatos que nao viram um o outro
# ---------------------------------------------------------------------------

class SondaFake:
    def __init__(self, reach, supported=frozenset({Use.REPO_READ})):
        self.reach = reach
        self.supported = supported
        self.recebido = []

    def supports(self, use):
        return use in self.supported

    def check(self, material):
        self.recebido.append(material)
        return self.reach, "resposta do fake"


def test_a_provider_rejection_is_not_a_regente_refusal(bench):
    """Autorizado aqui, recusado la. As duas coisas na mesma tela."""
    uma_credencial(bench)
    r = service(bench).test_connection(
        quem(ALICE), "wks_a", "repository", Use.REPO_READ,
        SondaFake(Reach.REJECTED))

    assert r.authorized is True
    assert r.reach is Reach.REJECTED
    assert r.usable is False


def test_an_unreachable_provider_is_not_a_rejected_credential(bench):
    """Tratar indisponibilidade como recusa manda trocar um token que estava bom."""
    uma_credencial(bench)

    class Caindo(SondaFake):
        def check(self, material):
            raise AdapterError("a rede nao respondeu")

    r = service(bench).test_connection(
        quem(ALICE), "wks_a", "repository", Use.REPO_READ,
        Caindo(Reach.UNKNOWN))
    assert r.reach is Reach.UNAVAILABLE
    assert r.reach is not Reach.REJECTED


def test_authentication_is_not_authorisation_for_a_mutation(bench):
    """O provider aceita a credencial e ela continua sem poder empurrar."""
    uma_credencial(bench, capabilities=("repo.read",))
    r = service(bench).test_connection(
        quem(ALICE), "wks_a", "repository", Use.REPO_PUSH,
        SondaFake(Reach.AUTHENTICATED, supported=frozenset({Use.REPO_PUSH})))

    assert r.authorized is False
    assert r.refusal is Refusal.NO_CAPABILITY
    assert r.usable is False


def test_a_revoked_credential_never_reaches_the_provider(bench):
    """Testar uma credencial que o Regente nao autorizaria informaria sobre a
    credencial e nada sobre o sistema."""
    uma_credencial(bench, revogada=True)
    sonda = SondaFake(Reach.AUTHENTICATED)
    r = service(bench).test_connection(quem(ALICE), "wks_a", "repository",
                                       Use.REPO_READ, sonda)

    assert sonda.recebido == [], "o material foi ao provedor mesmo revogado"
    assert r.authorized is False
    assert r.refusal is Refusal.REVOKED


# ---------------------------------------------------------------------------
# 8. Administracao
# ---------------------------------------------------------------------------

def test_two_live_credentials_for_the_same_use_conflict(bench):
    svc = service(bench)
    assert svc.register(quem(ALICE), "wks_a", name="p", provider="repository",
                        secret_ref="env:TOKEN",
                        capabilities=["repo.read"]).accepted is True
    out = svc.register(quem(ALICE), "wks_a", name="p", provider="repository",
                       secret_ref="env:OUTRO", capabilities=["repo.push"])

    assert out.refusal is Refusal.CONFLICT
    vivas = bench.credentials("wks_a")
    assert len(vivas) == 1 and vivas[0].secret_ref.text == "env:TOKEN"


def test_revoking_keeps_the_history(bench):
    c = uma_credencial(bench)
    service(bench).revoke(quem(ALICE), "wks_a", c.id, reason="rotacao")

    assert bench.credentials("wks_a") == []
    historia = bench.credentials("wks_a", include_revoked=True)
    assert len(historia) == 1
    assert historia[0].revoked_by == ALICE.key
    assert historia[0].granted_by == ALICE.key
    assert historia[0].revoked_at is not None
    assert "rotacao" in historia[0].note


def test_revoking_twice_does_not_rewrite_the_first_revocation(bench):
    c = uma_credencial(bench)
    svc = service(bench)
    svc.revoke(quem(ALICE), "wks_a", c.id)
    out = svc.revoke(quem(BOB, "wks_a"), "wks_a", c.id)

    assert out.refusal is Refusal.CONFLICT
    assert bench.credentials("wks_a", include_revoked=True)[0].revoked_by == ALICE.key


# ---------------------------------------------------------------------------
# 9. Estrutura
# ---------------------------------------------------------------------------

def test_the_core_never_holds_secret_material():
    """Se um segredo couber num objeto do dominio, o desenho esta errado."""
    import ast
    from pathlib import Path

    # `tokens` no plural e uma CONTAGEM (quantos tokens um agente gastou), e
    # nao um segredo. Um guard que acusa isso ensina a ignorar o guard.
    suspeitos = ("secret", "password", "senha", "material", "credencial")
    contagens = ("tokens",)
    faltas = []
    for arquivo in sorted(Path("regente/core").rglob("*.py")):
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            if not isinstance(no, ast.ClassDef):
                continue
            for campo in no.body:
                if not isinstance(campo, ast.AnnAssign):
                    continue
                nome = getattr(campo.target, "id", "")
                # `secret_ref` e um ENDERECO, e o nome diz isso.
                if nome.endswith("_ref") or nome == "secret_ref":
                    continue
                if nome.lower() in contagens:
                    continue
                if any(s in nome.lower() for s in suspeitos):
                    faltas.append(f"{arquivo.name}:{no.name}.{nome}")
    assert not faltas, "campo de material secreto no dominio:\n  " + \
        "\n  ".join(faltas)


def test_only_the_core_resolves_a_secret_in_the_governed_path():
    """Uma segunda porta ate o material e a que esquece uma barreira."""
    import ast
    from pathlib import Path

    permitido = {"regente/engine/credentials.py"}
    culpados = []
    for path in sorted(Path("regente/engine").rglob("*.py")):
        if path.as_posix() in permitido:
            continue
        for no in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute)
                    and no.func.attr == "resolve"
                    and isinstance(no.func.value, ast.Attribute)
                    and no.func.value.attr == "secrets"):
                culpados.append(f"{path.as_posix()}:{no.lineno}")
    assert not culpados, ("segredo resolvido fora do caminho governado:\n  "
                          + "\n  ".join(culpados))


def test_the_api_has_no_route_that_returns_material():
    """Nao existe, e a ausencia nao e uma lacuna a preencher."""
    from pathlib import Path

    fonte = Path("regente/app/api.py").read_text(encoding="utf-8")
    # `resolve(` sozinho pegaria `Api.resolve`, que e o roteador -- e um guard
    # que acusa a propria rota ensina a ignorar o guard. O que se proibe e
    # alcancar MATERIAL: a porta governada e a fonte de segredo.
    for proibido in ("use_secret", "_material", "credentials.resolve(",
                     ".secrets.resolve", "SecretProvider"):
        assert proibido not in fonte, f"a API alcanca material: {proibido}"

    # A prosa da tela PODE dizer "material secreto" -- e ali que a regra fica
    # escrita para quem le. O que nao pode e um campo ou uma rota que o carregue.
    tela = Path("regente/app/ui/app.js").read_text(encoding="utf-8")
    for proibido in ("secret_value", "use_secret", "/secret", ".material",
                     'c.secret"', "credential.secret"):
        assert proibido not in tela, f"a tela alcanca material: {proibido}"


def test_a_substitute_secret_source_needs_no_change_above_the_adapter(bench):
    """Duas fontes conceitualmente diferentes, o mesmo caminho acima.

    Uma le do processo; a outra pergunta a um programa. Nada em `core/`,
    `ports/` ou `engine/` muda entre as duas.
    """
    class FonteDeSessao:
        """Uma sessao ja aberta: o material nao esta guardado em lugar nenhum."""

        def __init__(self):
            self.chamadas = 0

        def resolve(self, reference: str) -> str:
            if reference != "sessao:qualquer":
                raise AdapterError("nao sei resolver isto")
            self.chamadas += 1
            return f"material-efemero-{self.chamadas}"

    uma_credencial(bench, ref="sessao:qualquer")
    fonte = FonteDeSessao()
    r = service(bench, fonte=fonte).resolve(quem(ALICE), "wks_a", "repository",
                                            Use.REPO_READ)

    assert isinstance(r, Resolved)
    assert r.use_secret() == "material-efemero-1"
    assert fonte.chamadas == 1

# ---------------------------------------------------------------------------
# 10. As lacunas que o sweep de mutacao encontrou
# ---------------------------------------------------------------------------
#
# Quatro delas moravam no ajudante de credencial -- a peca que executa um
# programa. Nenhum teste a alcancava, e ela e a unica do marco que roda codigo
# de fora do processo.

def test_the_store_refuses_to_revoke_a_credential_of_another_workspace(bench):
    """A ultima linha de defesa, exercitada sozinha.

    O servico ja le a credencial escopada antes de revogar. Isto prova a guarda
    do proprio store -- a que vale no dia em que a de cima for removida.
    """
    c = uma_credencial(bench, workspace="wks_a")

    assert bench.revoke_credential("wks_b", c.id, revoked_by="x") is False
    assert bench.credentials("wks_a")[0].active is True

    assert bench.revoke_credential("wks_a", c.id, revoked_by="x") is True
    assert bench.credentials("wks_a") == []


def test_an_unregistered_helper_is_never_executed_as_a_command(tmp_path):
    """A referencia ESCOLHE entre ajudantes; ela nunca traz o comando.

    Sem esta guarda, quem escrevesse uma referencia escreveria o que o processo
    executa -- e referencias vem de configuracao, de banco e, um dia, de uma
    tela.
    """
    import sys

    from regente.adapters.secrets import ScopedSecrets

    marca = tmp_path / "executou.txt"
    fonte = ScopedSecrets(allow_any=True, helpers={})

    with pytest.raises(AdapterError, match="nao esta registrado"):
        fonte.resolve(f"helper:{sys.executable} -c open(r'{marca}','w')")
    assert not marca.exists(), "a referencia virou execucao de comando"


def test_the_helper_receives_a_minimal_environment(tmp_path):
    """A mesma disciplina do marco 7: o ambiente e COMPOSTO, nao filtrado.

    Uma lista de exclusao esquece o que aparecer amanha; uma de inclusao nao.
    """
    import os
    import sys

    from regente.adapters.secrets import ScopedSecrets

    os.environ["SEGREDO_DE_OUTRO_LUGAR"] = "nao-deveria-vazar"
    try:
        script = tmp_path / "ajudante.py"
        script.write_text(
            "import os\n"
            "print('SEGREDO_DE_OUTRO_LUGAR' in os.environ)\n",
            encoding="utf-8")
        fonte = ScopedSecrets(
            allow_any=True,
            helpers={"x": (sys.executable, str(script))})
        assert fonte.resolve("helper:x") == "False", (
            "o ajudante herdou o ambiente do processo")
    finally:
        os.environ.pop("SEGREDO_DE_OUTRO_LUGAR", None)


def test_the_helpers_error_output_is_never_repassed(tmp_path):
    """Um ajudante pode ecoar o proprio segredo ao falhar.

    Essa mensagem viraria excecao, log e evento. O codigo de saida basta para
    saber que falhou; o texto nao acrescenta nada que valha o risco.
    """
    import sys

    from regente.adapters.secrets import ScopedSecrets

    script = tmp_path / "falha.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write('gho_segredo_ecoado_por_engano')\n"
        "sys.exit(3)\n",
        encoding="utf-8")
    fonte = ScopedSecrets(allow_any=True,
                          helpers={"x": (sys.executable, str(script))})

    with pytest.raises(AdapterError) as erro:
        fonte.resolve("helper:x")
    assert "gho_segredo_ecoado_por_engano" not in str(erro.value)
    assert "3" in str(erro.value)


def test_the_helper_returns_the_material_and_nothing_else(tmp_path):
    import sys

    from regente.adapters.secrets import ScopedSecrets

    script = tmp_path / "ok.py"
    script.write_text("print('  material-do-chaveiro  ')\n", encoding="utf-8")
    fonte = ScopedSecrets(allow_any=True,
                          helpers={"x": (sys.executable, str(script))})
    assert fonte.resolve("helper:x") == "material-do-chaveiro"


def test_an_audit_event_never_carries_a_field_that_holds_material(bench):
    """A guarda estrutural: nenhum campo do evento pode vir do material.

    A varredura por valor pega o vazamento de hoje. Esta pega a FORMA dele: um
    campo novo no evento que nasca de algo chamado material, segredo ou token.
    """
    import ast
    from pathlib import Path

    fonte = Path("regente/engine/credentials.py").read_text(encoding="utf-8")
    arvore = ast.parse(fonte)
    suspeitos = ("material", "secret", "token", "senha")
    faltas = []
    for no in ast.walk(arvore):
        if not isinstance(no, ast.Dict):
            continue
        for chave, valor in zip(no.keys, no.values):
            nome = getattr(chave, "value", "")
            if not isinstance(nome, str):
                continue
            # `secret_ref` e um endereco, e o nome diz isso.
            if nome.endswith("_ref"):
                continue
            if any(s in nome.lower() for s in suspeitos):
                faltas.append(f"linha {no.lineno}: campo '{nome}' no evento")
    assert not faltas, "campo de material na auditoria:\n  " + "\n  ".join(faltas)

# ---------------------------------------------------------------------------
# 6. A porta, exercitada
# ---------------------------------------------------------------------------

def test_working_in_a_workspace_is_not_permission_to_use_its_credentials(bench):
    """Quem responde a fila humana nao vira usuario de credencial por tabela.

    A primeira versao aceitava "qualquer capacidade neste workspace" como prova
    de pertencimento -- e pertencer nao e a mesma pergunta que poder usar o
    segredo do cliente.
    """
    uma_credencial(bench)
    operador = quem(ALICE, role="operator")          # so `approval.decide`
    assert operador.can("wks_a", Ability.DECIDE) is True

    out = service(bench).resolve(operador, "wks_a", "repository", Use.REPO_READ)
    assert isinstance(out, CredentialOutcome)
    assert out.refusal is Refusal.NOT_FOUND


def test_the_broker_turns_a_refusal_into_an_error_and_never_material(bench):
    """A porta devolve material OU levanta. Nunca as duas, nunca nenhuma."""
    from regente.ports.support import CredentialDenied

    uma_credencial(bench, revogada=True)
    porta = service(bench).broker(quem(ALICE), "wks_a", "repository")

    with pytest.raises(CredentialDenied) as erro:
        porta.material(Use.REPO_READ)
    assert erro.value.refusal == "REVOKED"
    assert SEGREDO not in str(erro.value)


def test_the_broker_asks_the_governed_path_every_single_time(bench):
    """Revogar fecha a porta sem reiniciar o motor.

    A mesma porta, o mesmo objeto: autoriza antes, recusa depois. Se ela
    guardasse material ou veredito, a segunda chamada continuaria passando.
    """
    from regente.ports.support import CredentialDenied

    c = uma_credencial(bench)
    porta = service(bench).broker(quem(ALICE), "wks_a", "repository")
    assert porta.material(Use.REPO_READ) == SEGREDO

    service(bench).revoke(quem(ALICE), "wks_a", c.id)

    with pytest.raises(CredentialDenied):
        porta.material(Use.REPO_READ)


def test_the_broker_is_bound_to_one_workspace_and_one_provider(bench):
    """O adapter nao escolhe escopo. Se escolhesse, escolheria o conveniente."""
    uma_credencial(bench, workspace="wks_b", client="cli_b", ref="env:TOKEN")
    porta = service(bench).broker(quem(ALICE, "wks_a"), "wks_a", "repository")

    assert porta.workspace_id == "wks_a"
    assert porta.provider == "repository"
    assert porta.allows(Use.REPO_READ) is False


def test_an_agent_without_a_broker_refuses_instead_of_reading_the_environment(
        tmp_path, monkeypatch):
    """Sem porta governada, o agente NAO cai para o ambiente.

    Cair seria o caminho legado voltando pela porta dos fundos -- e funcionaria,
    porque a variavel costuma estar la.
    """
    import os
    import sys

    from regente.adapters.runner.headless import HeadlessAgent, SandboxProfile
    from regente.ports import AdapterError

    monkeypatch.setenv("TOKEN_DO_AGENTE", "material-do-ambiente")
    agente = HeadlessAgent(
        command=[sys.executable, "-c", "pass"],
        sandbox=SandboxProfile(credential_env=("TOKEN_DO_AGENTE",),
                               broker=None))

    with pytest.raises(AdapterError, match="governed credential path"):
        agente._child_env()


def test_agent_readiness_says_no_when_no_credential_authorises_it(tmp_path):
    """A prontidao pergunta ao caminho governado, e aceita o nao.

    Antes ela perguntava "algum valor foi lido na construcao?" -- que respondia
    SIM para um segredo que ninguem autorizou, e continuaria respondendo SIM
    depois da revogacao.
    """
    import sys

    from regente.adapters.runner.headless import HeadlessAgent, SandboxProfile
    from regente.ports.agent import AuthMode

    class PortaQueNega:
        def material(self, use):
            raise AssertionError("a prontidao nao deve resolver material")

        def allows(self, use):
            return False

    agente = HeadlessAgent(
        command=[sys.executable, "-c", "pass"],
        auth_mode=AuthMode.RESOLVED_SECRET,
        sandbox=SandboxProfile(credential_env=("K",), broker=PortaQueNega()))

    check = agente.availability().authentication
    assert check.ok is False
    assert "agent.run" in check.detail


def test_the_broker_never_crosses_to_another_providers_credential(bench):
    """Uma credencial de tasks nao serve ao repositorio.

    Sem esta prova, fixar o provider no caminho do broker passava despercebido:
    todos os outros testes usam o mesmo nome, e um valor fixo casa com ele.
    """
    from regente.ports.support import CredentialDenied

    uma_credencial(bench, provider="repository", name="do-repo",
                   capabilities=("repo.read",))
    porta_de_tasks = service(bench).broker(quem(ALICE), "wks_a", "tasks")

    assert porta_de_tasks.allows(Use.REPO_READ) is False
    with pytest.raises(CredentialDenied) as erro:
        porta_de_tasks.material(Use.REPO_READ)
    assert erro.value.refusal == "NOT_FOUND"

    # E a porta do repositorio continua funcionando: o isolamento e entre
    # providers, e nao uma quebra geral.
    assert service(bench).broker(
        quem(ALICE), "wks_a", "repository").material(Use.REPO_READ) == SEGREDO
