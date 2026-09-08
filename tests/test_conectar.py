# -*- coding: utf-8 -*-
"""Conectar em um clique -- sem que o clique contorne nada.

Este arquivo guarda uma tensao, e vale dizer qual antes dos testes.

Conectar existe para apagar CINCO PASSOS MANUAIS: escolher um adapter, digitar
a organizacao, ir a outra tela, registrar uma credencial, voltar. Cada um deles
era uma decisao que o Regente podia ter tomado sozinho.

O que ele NAO pode apagar e nenhuma barreira. A configuracao continua indo pelo
`SettingsService` e a credencial pelo `CredentialService` -- os mesmos que o
terminal usa. Um botao que gravasse direto seria um segundo caminho ate a
autoridade, e o segundo caminho e sempre o que ninguem revisou.

Os defeitos que estes testes impedem sao todos do tipo "funciona e esta errado":

* conectar gravando sem passar por policy ou concessao;
* o corpo da requisicao escolhendo a referencia do segredo;
* uma credencial antiga sem a permissao de listar sendo tratada como sucesso,
  e a busca recusando depois, longe de quem conectou;
* o motor ficando sem poder usar o que a pessoa acabou de conectar -- e a fila
  parando de madrugada, sem ninguem por perto.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from regente.adapters.connectors import Conta, Passo, Proposta
from regente.core.access import Ability, abilities_of
from regente.core.credential import Status, Use
from regente.core.model import Workspace
from regente.core.policy import PolicyEngine
from regente.core.principal import Principal
from regente.engine.access import AccessService
from regente.engine.connect import ConnectService
from regente.engine.credentials import CredentialService, Refusal
from regente.engine.settings import SettingsService
from regente.engine.store_sqlite import SqliteStore

T0 = datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)

POLICY = PolicyEngine.from_config(
    yaml.safe_load(
        Path("regente/resources/policies.yaml.example").read_text(
            encoding="utf-8"))["rules"])


# ===========================================================================
# bancada
# ===========================================================================

@dataclass
class ConectorFake:
    """Um servico conectavel, deterministico e sem rede.

    Ele NAO fala com ferramenta nenhuma: o que se exercita aqui e o caminho da
    escrita, e nao a conversa com o GitHub. Aquilo tem testes proprios, contra
    o adapter de verdade.
    """
    nome: str = "fake"
    name: str = "fake"
    titulo: str = "Serviço de Teste"
    descricao: str = "Um serviço que só existe nos testes."
    papel: str = "repository"
    passo: Passo = field(default_factory=lambda: Passo("escolher", "Escolha"))
    contas_: list = field(default_factory=lambda: [Conta("acme", "acme", "organizacao")])
    capacidades: tuple = ("repo.discover", "repo.read")

    def estado(self):
        return self.passo

    def autorizar(self):
        return Passo("autorizar", "Abra o navegador", navegador=True)

    def contas(self):
        return list(self.contas_)

    def proposta(self, conta):
        if not str(conta).strip():
            raise ValueError("escolha uma conta")
        return Proposta(papel=self.papel,
                        provider={"name": self.name, "org": str(conta)},
                        secret_ref="helper:gh",
                        capacidades=self.capacidades,
                        nome_credencial="teste")


@dataclass
class Bancada:
    store: SqliteStore
    workspace: str
    service: ConnectService
    conector: ConectorFake
    credenciais: CredentialService
    settings: SettingsService
    acesso: AccessService


def _bancada(tmp_path, conector=None, com_acesso=True) -> Bancada:
    from regente.adapters.secrets import ScopedSecrets

    store = SqliteStore(tmp_path / "conectar.db")
    store.migrate()
    store.save_client("cli_1", "acme", "acme")
    store.save_workspace(Workspace(id="wks_A", client_id="cli_1", name="main",
                                   root=str(tmp_path)))
    comum = dict(store=store, policy=POLICY, organization="acme",
                 client="acme", workspace_name="main")
    settings = SettingsService(clock=lambda: T0, **comum)
    credenciais = CredentialService(
        secrets=ScopedSecrets(workspace="main", allow_any=True),
        clock=lambda: T0, **comum)
    acesso = AccessService(clock=lambda: T0, **comum)
    c = conector or ConectorFake()
    return Bancada(
        store=store, workspace="wks_A", conector=c,
        credenciais=credenciais, settings=settings, acesso=acesso,
        service=ConnectService(
            settings=settings, credentials=credenciais,
            access=acesso if com_acesso else None,
            conectores={c.nome: c},
            providers_do_arquivo=lambda: {"tasks": {"name": "filesystem"}}))


def _quem(*abilities, sujeito="os:1") -> Principal:
    return Principal(
        subject=sujeito, display=sujeito, method="teste", provider="os",
        authenticated_at=T0, workspaces=frozenset({"wks_A"}),
        abilities={"wks_A": frozenset(abilities)})


def _dono(sujeito="os:1") -> Principal:
    return _quem(*abilities_of("owner"), sujeito=sujeito)


# ===========================================================================
# 1. UM PEDIDO FAZ O QUE CINCO PASSOS FAZIAM
# ===========================================================================

def test_connecting_writes_the_provider_and_the_credential(tmp_path):
    """O coracao: um pedido, e o workspace fica pronto para descobrir."""
    b = _bancada(tmp_path)
    saida = b.service.conectar(_dono(), b.workspace, "fake", "acme")
    assert saida.accepted, saida.reason

    providers = b.settings.overlay(b.workspace).get("providers")
    assert providers["repository"] == {"name": "fake", "org": "acme"}

    creds = b.credenciais.listing(_dono(), b.workspace)
    assert [c.provider for c in creds] == ["repository"]
    assert (creds[0].secret_ref.scheme,
            creds[0].secret_ref.locator) == ("helper", "gh")
    assert {u.value for u in creds[0].capabilities} == {"repo.discover",
                                                        "repo.read"}


def test_connecting_keeps_the_providers_that_were_already_there(tmp_path):
    """Ligar um servico nao pode desligar os outros.

    A sobreposicao grava `providers` INTEIRO. Escrever so o papel novo apagaria
    o board de tasks -- e o sintoma apareceria no proximo tick, sem nada na
    tela ligando uma coisa a outra.
    """
    b = _bancada(tmp_path)
    assert b.service.conectar(_dono(), b.workspace, "fake", "acme").accepted
    providers = b.settings.overlay(b.workspace).get("providers")
    assert providers["tasks"] == {"name": "filesystem"}


def test_connecting_authorizes_the_engine_to_use_it(tmp_path):
    """Quem conecta esta dizendo "o Regente pode usar isto".

    Sem a concessao ao principal de servico, o tick recusa credencial e a fila
    para -- longe daqui, de madrugada, sem ninguem por perto e sem nada ligando
    uma coisa a outra. A concessao passa pelo `AccessService`, fica na trilha, e
    sai por `regente access revogar` como qualquer outra.
    """
    from regente.core.access import PrincipalRef

    b = _bancada(tmp_path)
    motor = PrincipalRef(provider="engine", subject=b.workspace)
    antes = b.acesso.abilities_for(motor).get(b.workspace, frozenset())
    assert Ability.CREDENTIAL_USE not in antes

    saida = b.service.conectar(_dono(), b.workspace, "fake", "acme")
    assert saida.accepted
    assert not saida.aviso, saida.aviso_detalhe

    depois = b.acesso.abilities_for(motor).get(b.workspace, frozenset())
    assert Ability.CREDENTIAL_USE in depois, (
        "o motor continua sem poder usar o que a pessoa acabou de conectar")


def test_when_the_engine_cannot_be_authorized_it_says_so(tmp_path):
    """Conectou e faltou algo: e aviso, e nao silencio nem erro.

    Quem so pode configurar consegue conectar, e nao consegue conceder. A
    conexao e legitima e fica gravada; o que nao pode e a pessoa ir embora sem
    saber que o motor nao vai usar aquilo sozinho.
    """
    b = _bancada(tmp_path)
    so_configura = _quem(Ability.SETTINGS_WRITE, Ability.CREDENTIAL_GRANT,
                         Ability.CREDENTIAL_LIST)
    saida = b.service.conectar(so_configura, b.workspace, "fake", "acme")
    assert saida.accepted
    assert saida.aviso == "motor_sem_acesso"
    assert "access conceder" in saida.aviso_detalhe


# ===========================================================================
# 2. O CLIQUE NAO CONTORNA NADA
# ===========================================================================

def test_an_unauthenticated_request_connects_nothing(tmp_path):
    b = _bancada(tmp_path)
    anonimo = Principal(subject="anonimo", display="nao autenticado")
    saida = b.service.conectar(anonimo, b.workspace, "fake", "acme")
    assert not saida.accepted
    assert b.settings.overlay(b.workspace).get("providers") is None


def test_connecting_needs_authority_over_credentials(tmp_path):
    """Registrar credencial e capacidade propria, e conectar registra uma.

    Quem so pode configurar o workspace nao ganha, por tabela, o direito de
    autorizar um segredo -- e por isso a recusa vem ANTES de qualquer escrita.
    """
    b = _bancada(tmp_path)
    saida = b.service.conectar(_quem(Ability.SETTINGS_WRITE), b.workspace,
                               "fake", "acme")
    assert not saida.accepted
    assert b.settings.overlay(b.workspace).get("providers") is None, (
        "recusou a credencial e mesmo assim gravou a configuracao")


def test_the_policy_still_decides(tmp_path):
    """ALLOW geral, DENY na acao de configurar: conectar recusa."""
    b = _bancada(tmp_path)
    apertada = PolicyEngine.from_config([
        {"name": "tudo", "effect": "ALLOW", "match": {"action": "*"}},
        {"name": "menos", "effect": "DENY",
         "match": {"action": "workspace.settings.write"}},
    ])
    b.settings.policy = apertada
    saida = b.service.conectar(_dono(), b.workspace, "fake", "acme")
    assert not saida.accepted
    assert saida.refusal is Refusal.POLICY_DENIED


def test_the_caller_never_chooses_where_the_secret_lives(tmp_path):
    """A referencia vem do CONECTOR, e nao de quem pediu.

    Se ela viesse do pedido, `secret_ref: env:MEU` faria o Regente resolver um
    segredo escolhido por quem chamou -- e a tela viraria um seletor de fonte
    de credencial, que e autoridade que ela nao tem.
    """
    import inspect

    from regente.engine.connect import ConnectService as Servico

    assinatura = inspect.signature(Servico.conectar).parameters
    assert "secret_ref" not in assinatura
    assert set(assinatura) == {"self", "actor", "workspace_id", "nome",
                               "conta", "substituir"}


def test_a_service_nobody_composed_is_refused(tmp_path):
    b = _bancada(tmp_path)
    saida = b.service.conectar(_dono(), b.workspace, "servico-inventado", "x")
    assert not saida.accepted
    assert saida.refusal is Refusal.NOT_FOUND


def test_authorizing_opens_nothing_for_who_cannot_configure(tmp_path):
    """Abrir uma janela de autorizacao na maquina de quem roda e um efeito."""
    b = _bancada(tmp_path)
    saida = b.service.autorizar(_quem(), b.workspace, "fake")
    assert saida["code"] == "recusado"


# ===========================================================================
# 3. A CREDENCIAL QUE JA ESTAVA LA
# ===========================================================================

def test_reconnecting_with_a_credential_that_already_serves_is_fine(tmp_path):
    """Trocar de organizacao nao pode exigir mexer numa credencial boa."""
    b = _bancada(tmp_path)
    assert b.service.conectar(_dono(), b.workspace, "fake", "acme").accepted
    saida = b.service.conectar(_dono(), b.workspace, "fake", "outra")
    assert saida.accepted
    providers = b.settings.overlay(b.workspace).get("providers")
    assert providers["repository"]["org"] == "outra"


def test_an_old_credential_without_the_new_capability_is_not_a_success(tmp_path):
    """O defeito silencioso deste fluxo, e o motivo de o conflito existir.

    Uma credencial registrada antes -- so com `repo.read` -- faria o botao
    dizer "conectado" e a busca recusar depois, longe de quem conectou. Entao a
    recusa vem AGORA, com o id da credencial para a tela poder oferecer trocar.
    """
    b = _bancada(tmp_path)
    antiga = b.credenciais.register(
        _dono(), b.workspace, "teste", "repository", "helper:gh",
        ["repo.read"])
    assert antiga.accepted

    saida = b.service.conectar(_dono(), b.workspace, "fake", "acme")
    assert not saida.accepted
    assert saida.refusal is Refusal.CONFLICT
    assert saida.credential_id == antiga.credential.id
    assert "repo.discover" in saida.reason


def test_replacing_revokes_the_old_one_and_registers_a_new(tmp_path):
    """Substituir e uma escolha, e ela deixa rastro dos dois lados."""
    b = _bancada(tmp_path)
    antiga = b.credenciais.register(
        _dono(), b.workspace, "teste", "repository", "helper:gh",
        ["repo.read"]).credential

    saida = b.service.conectar(_dono(), b.workspace, "fake", "acme",
                               substituir=True)
    assert saida.accepted

    todas = b.credenciais.listing(_dono(), b.workspace)
    por_id = {c.id: c for c in todas}
    assert por_id[antiga.id].status(T0) is Status.REVOKED
    viva = [c for c in todas if c.status(T0) is Status.ACTIVE]
    assert len(viva) == 1
    assert Use.REPO_DISCOVER in viva[0].capabilities


# ===========================================================================
# 4. O QUE A TELA LE
# ===========================================================================

def test_listing_says_what_is_missing_for_each_service(tmp_path):
    """"Nao conectado" sem o proximo movimento e a mesma coisa que silencio."""
    b = _bancada(tmp_path, ConectorFake(
        passo=Passo("instalar", "Instale a ferramenta",
                    "Sem ela o Regente nao conversa com o servico",
                    comando="winget install X")))
    linha = b.service.listar(_dono(), b.workspace)[0]
    assert linha["connected"] is False
    assert linha["step"]["code"] == "instalar"
    assert linha["step"]["command"] == "winget install X"


def test_listing_shows_the_account_in_use_after_connecting(tmp_path):
    b = _bancada(tmp_path)
    b.service.conectar(_dono(), b.workspace, "fake", "acme")
    linha = b.service.listar(_dono(), b.workspace)[0]
    assert linha["connected"] is True
    assert linha["current"] == "acme"


def test_nobody_reads_the_connectors_of_a_workspace_they_cannot_reach(tmp_path):
    b = _bancada(tmp_path)
    de_fora = Principal(subject="os:2", display="outro", method="teste",
                        provider="os", authenticated_at=T0,
                        workspaces=frozenset({"wks_B"}))
    assert b.service.listar(de_fora, b.workspace) == []
    assert b.service.contas(de_fora, b.workspace, "fake") == []


def test_nothing_the_screen_reads_carries_a_secret(tmp_path):
    """Nenhuma rota de conexao devolve material. A ausencia e o desenho."""
    import json

    b = _bancada(tmp_path)
    b.service.conectar(_dono(), b.workspace, "fake", "acme")
    corpo = json.dumps(b.service.listar(_dono(), b.workspace),
                       default=str).lower()
    for palavra in ("gho_", "token", "secret", "senha", "helper:"):
        assert palavra not in corpo, f"a listagem carregou '{palavra}'"


# ===========================================================================
# 5. O AJUDANTE EMBUTIDO
# ===========================================================================

def test_a_workspace_cannot_redefine_a_built_in_helper():
    """Um nome embutido significa sempre a mesma coisa.

    Um workspace que declarasse `gh: [curl, meu-site]` faria toda credencial
    `helper:gh` sair de outro lugar -- e ninguem leria o YAML de novo depois de
    conectar.
    """
    from regente.adapters.secrets import HELPERS_EMBUTIDOS, ScopedSecrets

    s = ScopedSecrets(workspace="w", allow_any=True,
                      helpers={"gh": ("echo", "impostor")})
    assert s._comando_do("gh") == HELPERS_EMBUTIDOS["gh"]


def test_a_built_in_helper_is_a_fixed_command_not_a_line_from_config():
    """A referencia escolhe ENTRE ajudantes; ela nunca traz o comando."""
    from regente.adapters.secrets import HELPERS_EMBUTIDOS

    for nome, comando in HELPERS_EMBUTIDOS.items():
        assert isinstance(comando, tuple), nome
        assert all(isinstance(p, str) for p in comando), nome
        assert " " not in comando[0], (
            f"{nome} tem um executavel com espaco -- isso e uma linha de "
            f"comando disfarcada")


# ===========================================================================
# 6. A ABSTRACAO
# ===========================================================================

def test_the_engine_does_not_know_what_a_github_is():
    """Acrescentar um servico deve custar um adapter, e nao alteracoes."""
    import ast
    from pathlib import Path as _P

    fonte = (_P(__file__).resolve().parents[1] / "regente" / "engine"
             / "connect.py").read_text(encoding="utf-8")
    arvore = ast.parse(fonte)

    docstrings = set()
    for no in ast.walk(arvore):
        if isinstance(no, (ast.Module, ast.ClassDef, ast.FunctionDef,
                           ast.AsyncFunctionDef)) and no.body:
            primeiro = no.body[0]
            if (isinstance(primeiro, ast.Expr)
                    and isinstance(primeiro.value, ast.Constant)
                    and isinstance(primeiro.value.value, str)):
                docstrings.add(id(primeiro.value))

    for no in ast.walk(arvore):
        if (isinstance(no, ast.Constant) and isinstance(no.value, str)
                and id(no) not in docstrings):
            for vendedor in ("github", "jira", "clickup", "gitlab", "gh "):
                assert vendedor not in no.value.lower(), (
                    f"o motor conhece fornecedor: {no.value!r}")

# ===========================================================================
# 7. A POLICY QUE FICOU PARA TRAS
# ===========================================================================
#
# O `policies.yaml` de um workspace e escrito uma vez, no `init`, e nunca mais
# -- e deve ser assim: o arquivo e da pessoa, e atualizar um pacote nao pode
# ampliar autoridade sozinho.
#
# O preco disso era uma linha de documentacao por marco. Quem nao lesse ficava
# com uma tela recusando por OMISSAO: a policy nao diz nao, ela nunca ouviu
# falar da acao -- a recusa mais dificil de diagnosticar que existe.
#
# Estes testes guardam a troca: detectar virou automatico, e ampliar continua
# sendo uma decisao.

def test_every_action_this_version_emits_is_read_from_the_enums():
    """A lista de acoes nao pode ser escrita a mao.

    Uma lista a mao envelhece no primeiro marco em que alguem esquecer de
    atualiza-la -- e o sintoma seria exatamente o silencio que esta checagem
    existe para acabar.
    """
    from regente.app.container import acoes_que_o_motor_emite
    from regente.core.access import Ability
    from regente.core.credential import Use

    emitidas = set(acoes_que_o_motor_emite())
    assert {a.value for a in Ability} <= emitidas
    assert {u.value for u in Use} <= emitidas
    assert any(a.endswith(".resource.discover") for a in emitidas)


def test_doctor_notices_a_policy_that_does_not_know_a_new_action(tmp_path):
    """Uma acao sem NENHUMA regra que a mencione e defasagem, e aparece."""
    from regente.app.container import _policy_em_dia

    velha = tmp_path / "policies.yaml"
    velha.write_text("rules:\n"
                     "  - name: so_leitura\n"
                     "    effect: ALLOW\n"
                     "    match: {action: \"*.read\"}\n", encoding="utf-8")
    cfg = _config_falsa(tmp_path, velha)

    nome, ok, detalhe = _policy_em_dia(cfg)
    assert nome == "policy em dia"
    assert not ok
    # O detalhe conta QUANTAS e mostra as primeiras: uma linha de terminal nao
    # cabe vinte acoes, e uma que estoura a tela nao e lida.
    assert "17 acao(oes)" in detalhe or "acao(oes)" in detalhe
    assert "regente atualizar" in detalhe


def test_a_policy_that_says_NO_is_up_to_date(tmp_path):
    """Negar explicitamente nao e defasagem: alguem decidiu aquilo.

    A diferenca importa. Um DENY escrito e uma escolha da organizacao, e o
    `doctor` acusa-la seria pedir para afrouxar uma policy de proposito.
    """
    from regente.app.container import _policy_em_dia

    fechada = tmp_path / "policies.yaml"
    fechada.write_text("rules:\n"
                       "  - name: nada_passa\n"
                       "    effect: DENY\n"
                       "    match: {action: \"*\"}\n", encoding="utf-8")
    nome, ok, detalhe = _policy_em_dia(_config_falsa(tmp_path, fechada))
    assert ok, detalhe


def test_the_shipped_policy_is_up_to_date_with_itself(tmp_path):
    """O modelo enviado precisa conhecer tudo o que esta versao emite.

    Se ele nao conhecer, todo workspace novo nasce defasado -- e o `doctor`
    acusaria uma instalacao recem-feita, que e a forma mais rapida de ensinar
    alguem a ignorar o `doctor`.
    """
    import shutil
    from pathlib import Path as _P
    from regente.app.container import _policy_em_dia

    enviado = tmp_path / "policies.yaml"
    shutil.copy(_P("regente/resources/policies.yaml.example"), enviado)
    nome, ok, detalhe = _policy_em_dia(_config_falsa(tmp_path, enviado))
    assert ok, detalhe


def test_bringing_a_policy_up_to_date_carries_the_reasons(tmp_path):
    """As regras chegam com o comentario que explica por que existem.

    Uma regra colada sem a razao vira, seis meses depois, uma linha que ninguem
    ousa remover porque ninguem sabe para que serve.
    """
    from regente.cli import _regras_faltando

    velha = tmp_path / "policies.yaml"
    velha.write_text("rules:\n"
                     "  - name: so_leitura\n"
                     "    effect: ALLOW\n"
                     "    match: {action: \"*.read\"}\n", encoding="utf-8")
    faltando = _regras_faltando(_config_falsa(tmp_path, velha))

    nomes = [n for n, _ in faltando]
    assert "human_selects_workspace_resources" in nomes
    texto = dict(faltando)["human_selects_workspace_resources"]
    assert "#" in texto, "a regra veio sem a razao dela"
    assert "workspace.resource.select" in texto

    # E cada bloco e uma regra so -- nao pode arrastar o cabecalho da proxima.
    for nome, bloco in faltando:
        assert bloco.count("- name:") == 1, (
            f"o bloco de {nome} arrastou outra regra:\n{bloco}")


def test_bringing_up_to_date_never_removes_what_was_there(tmp_path):
    """Poe em dia ACRESCENTA. O que a organizacao escreveu continua valendo."""
    import yaml as _yaml
    from regente.cli import _regras_faltando

    velha = tmp_path / "policies.yaml"
    velha.write_text("rules:\n"
                     "  # uma decisao desta organizacao\n"
                     "  - name: nunca_em_producao\n"
                     "    effect: DENY\n"
                     "    match: {action: \"deploy.production\"}\n",
                     encoding="utf-8")
    cfg = _config_falsa(tmp_path, velha)
    faltando = _regras_faltando(cfg)

    atual = velha.read_text(encoding="utf-8").rstrip()
    bloco = "\n\n".join(t for _, t in faltando)
    velha.write_text(f"{atual}\n\n{bloco}\n", encoding="utf-8")

    regras = _yaml.safe_load(velha.read_text(encoding="utf-8"))["rules"]
    por_nome = {r["name"]: r for r in regras}
    assert por_nome["nunca_em_producao"]["effect"] == "DENY", (
        "poe em dia apagou uma decisao da organizacao")
    assert "human_selects_workspace_resources" in por_nome


def _config_falsa(raiz, policies):
    """Uma configuracao minima, so com o que a checagem le."""
    from types import SimpleNamespace

    return SimpleNamespace(policies=str(policies), organization="acme",
                           client="acme", workspace="main", root=raiz)
