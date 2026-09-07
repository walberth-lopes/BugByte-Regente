# -*- coding: utf-8 -*-
"""O material autorizado chega ao subprocesso -- e chega SO por ali.

O marco 16 provou **autoridade**: existe uma porta unica que decide se um
adapter pode ter credencial. Este arquivo prova a outra metade, que nao vem de
graca junto.

    broker.material(use) -> ambiente do filho -> subprocesso

Uma fechadura numa porta que ninguem usa nao tranca nada. Enquanto a ferramenta
se autenticava sozinha pelo chaveiro do sistema, o broker podia recusar o dia
inteiro sem mudar o que acontecia no mundo.

**Nada aqui e provado por inspecao de codigo.** Um processo de verdade e
disparado, ele grava o que recebeu, e o teste le do disco:

  - o arquivo existe  -> o processo rodou
  - o arquivo NAO existe -> o processo nunca foi criado

A segunda leitura e a mais importante. Toda recusa deste marco tem de acontecer
ANTES de qualquer coisa sair da maquina, e "levantou uma excecao" nao prova
isso: um adapter poderia disparar o processo e so entao reclamar.

O sentinela e um valor unico, procurado depois em argv, ambiente, log, evento,
excecao, banco e saida.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from regente.adapters.cicd.github_checks import GitHubChecks
from regente.adapters.childproc import ChildEnvironment, ChildLaunch
from regente.adapters.repos import cli_process
from regente.adapters.repos.github_write import GitHubWrite
from regente.adapters.secrets import ScopedSecrets
from regente.core import ids
from regente.core.access import PrincipalRef, abilities_of
from regente.core.credential import Credential, SecretRef, Use, uses_from
from regente.core.model import Event, Workspace
from regente.core.policy import PolicyEngine
from regente.core.principal import Principal
from regente.engine.credentials import CredentialService
from regente.engine.store_sqlite import SqliteStore
from regente.ports import AdapterError
from regente.ports.support import CredentialDenied

T0 = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)

#: Unico, longo e reconhecivel. Se ele aparecer em qualquer superficie
#: observada, o teste que o encontrar diz exatamente onde.
SENTINELA = "ghs_SENTINELA61_esteValorNaoPodeVazarEmLugarNenhum_0001"

ALICE = PrincipalRef("os-account", "S-1-5-21-1")

#: Vocabulario minimo. A policy e uma barreira separada das outras, e um teste
#: que a deixasse permissiva por padrao nao provaria que ela e consultada.
REGRAS = [{"name": "usa", "effect": "ALLOW",
           "match": {"action": ["repo.read", "repo.pr", "ci.read"]}}]


# ---------------------------------------------------------------------------
# O processo filho de verdade
# ---------------------------------------------------------------------------

#: Grava o que recebeu e devolve JSON. Fica no disco do teste, e por isso o
#: caminho de saida vem do proprio diretorio dele -- o ambiente do filho e
#: montado do vazio, entao uma variavel combinada nao chegaria ate aqui, o que
#: e justamente o comportamento sob teste.
RECORDER = '''\
import json, os, sys
from pathlib import Path

aqui = Path(__file__).resolve().parent
(aqui / "recebido.json").write_text(
    json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}),
    encoding="utf-8")

modo = (aqui / "modo.txt")
if modo.exists() and modo.read_text(encoding="utf-8").strip() == "eco":
    # A ferramenta ecoa de volta o que recebeu. Acontece de verdade: um
    # provedor que rejeita um token costuma cita-lo na mensagem.
    sys.stderr.write("falha ao autenticar com " + os.environ.get("GH_TOKEN", "") + "\\n")
    sys.exit(1)

print(json.dumps({"ok": True, "login": "ninguem"}))
'''


class Ferramenta:
    """Uma CLI real no disco, que registra o que recebeu."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / "recorder.py").write_text(RECORDER, encoding="utf-8")
        if os.name == "nt":
            self.path = root / "cli.cmd"
            self.path.write_text(
                f'@echo off\r\n"{sys.executable}" "{root / "recorder.py"}" %*\r\n',
                encoding="utf-8")
        else:
            self.path = root / "cli.sh"
            self.path.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{root / "recorder.py"}" "$@"\n',
                encoding="utf-8")
            self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)

    def echo_the_credential_back(self) -> None:
        (self.root / "modo.txt").write_text("eco", encoding="utf-8")

    @property
    def ran(self) -> bool:
        return (self.root / "recebido.json").exists()

    def received(self) -> dict:
        assert self.ran, "o processo nunca foi criado"
        return json.loads((self.root / "recebido.json").read_text(encoding="utf-8"))


@pytest.fixture
def tool(tmp_path):
    return Ferramenta(tmp_path / "ferramenta")


# ---------------------------------------------------------------------------
# O caminho governado, de verdade: store, concessao, policy, credencial
# ---------------------------------------------------------------------------

@pytest.fixture
def bench(tmp_path):
    store = SqliteStore(tmp_path / "t.db", clock=lambda: T0)
    store.migrate()
    for wid, client in (("wks_a", "cli_a"), ("wks_b", "cli_b")):
        store.save_client(client, "org", wid)
        store.save_workspace(Workspace(id=wid, client_id=client, name="main"))
    yield store
    store.close()


def quem(workspace="wks_a", role="owner") -> Principal:
    return Principal(
        subject=ALICE.subject, display="alice", method="os-account",
        provider="os-account", issuer="maquina", authenticated_at=T0,
        abilities={workspace: abilities_of(role)})


def credencial(store, workspace="wks_a", client="cli_a",
               provider="repository_write", capabilities=("repo.read",),
               ref="env:TOKEN_DE_TESTE", revogada=False, expires_at=None):
    c = Credential(
        id=ids.new_id(ids.CREDENTIAL), client_id=client, workspace_id=workspace,
        name="principal", provider=provider, kind="token",
        secret_ref=SecretRef.parse(ref), capabilities=uses_from(capabilities),
        granted_by=ALICE.key, granted_at=T0, expires_at=expires_at,
        revoked_by=(ALICE.key if revogada else ""),
        revoked_at=(T0 if revogada else None))
    store.open_credential(c)
    return c


def porta(store, actor=None, workspace="wks_a", provider="repository_write",
          rules=None, at=None):
    """Um `BoundBroker` REAL: mesma montagem que a composicao faz."""
    servico = CredentialService(
        store=store,
        policy=PolicyEngine.from_config(REGRAS if rules is None else rules),
        secrets=ScopedSecrets(workspace="main", allow_any=True),
        clock=(lambda: at) if at else (lambda: T0),
        organization="org", client="Acme", workspace_name="main")
    return servico.broker(actor if actor is not None else quem(), workspace,
                          provider)


@pytest.fixture
def segredo(monkeypatch):
    """O material existe no ambiente do MOTOR -- e so o motor o alcanca."""
    monkeypatch.setenv("TOKEN_DE_TESTE", SENTINELA)
    return SENTINELA


def adapter(tool, broker, **kw) -> GitHubWrite:
    return GitHubWrite(org="acme", cli_path=str(tool.path), credentials=broker,
                       **kw)


# ===========================================================================
# TRANSPORTE
# ===========================================================================

def test_the_material_reaches_the_child_through_the_environment(bench, tool, segredo):
    """O caminho inteiro, de ponta a ponta, com um processo de verdade."""
    credencial(bench)
    saida = adapter(tool, porta(bench))._cli(
        ["api", "user"], write=False, use=Use.REPO_READ, json_expected=False)

    recebido = tool.received()
    assert recebido["env"].get("GH_TOKEN") == SENTINELA, \
        "o material nao chegou ao processo filho"
    assert "ok" in saida


def test_the_material_is_never_in_argv(bench, tool, segredo):
    """`argv` e legivel por QUALQUER usuario da maquina; o ambiente, so pelo dono.

    Nao e uma diferenca de estilo -- e a diferenca entre um vazamento que exige
    ja ser voce e um que nao exige nada.
    """
    credencial(bench)
    adapter(tool, porta(bench))._cli(["api", "user"], write=False,
                                     use=Use.REPO_READ, json_expected=False)

    argv = tool.received()["argv"]
    assert SENTINELA not in " ".join(argv)
    assert not any(SENTINELA in a for a in argv)


def test_the_child_starts_from_nothing_not_from_the_engines_environment(
        bench, tool, segredo, monkeypatch):
    """A contraprova do defeito que abriu este marco.

    Ate aqui os adapters chamavam `subprocess.run` sem `env=`, e o filho herdava
    o ambiente inteiro do motor -- inclusive credencial de outro provedor, que
    ninguem lhe deu e que ninguem conseguia revogar.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "token_do_vizinho_que_nao_e_deste_uso")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "de_outro_provedor_inteiramente")
    monkeypatch.setenv("MINHA_VARIAVEL_QUALQUER", "nada_de_secreto_aqui")

    credencial(bench)
    adapter(tool, porta(bench))._cli(["api", "user"], write=False,
                                     use=Use.REPO_READ, json_expected=False)

    ambiente = tool.received()["env"]
    assert "GITHUB_TOKEN" not in ambiente
    assert "AWS_SECRET_ACCESS_KEY" not in ambiente
    assert "MINHA_VARIAVEL_QUALQUER" not in ambiente
    # E o que ele PRECISA continua la, senao o processo nem comeca.
    assert "PATH" in ambiente


def test_the_tool_cannot_reach_its_own_stored_credential(bench, tool, segredo):
    """Sem isto, a ferramenta se autentica sozinha e a porta vira decoracao.

    Medido antes de escrever o codigo: com o ambiente herdado, ela aceitava sem
    token nenhum, pelo chaveiro do sistema. Com a configuracao dela apontada
    para um lugar que nao existe, ela obedece a variavel -- e so ela.
    """
    credencial(bench)
    adapter(tool, porta(bench))._cli(["api", "user"], write=False,
                                     use=Use.REPO_READ, json_expected=False)

    raiz = tool.received()["env"]["GH_CONFIG_DIR"]
    assert raiz == cli_process.NO_CONFIG_DIR
    for nome in cli_process.CONFIG_FILES:
        assert not os.path.exists(os.path.join(raiz, nome)), \
            f"a ferramenta ainda alcanca a propria credencial via {nome}"


def test_a_reachable_tool_configuration_is_refused_not_ignored(bench, tmp_path):
    """Se o isolamento for desfeito, `verify()` recusa em vez de seguir."""
    aberto = tmp_path / "config-da-ferramenta"
    aberto.mkdir()
    (aberto / "hosts.yml").write_text("github.com:\n  oauth_token: x\n",
                                      encoding="utf-8")
    with pytest.raises(AdapterError, match="por conta propria"):
        GitHubWrite(org="acme", config_dir=str(aberto)).verify()


# ===========================================================================
# AUTORIZACAO -- e, em cada caso, o processo NAO existe
# ===========================================================================

def _refusado(tool, broker, use=Use.REPO_READ, excecao=CredentialDenied):
    with pytest.raises(excecao) as erro:
        adapter(tool, broker)._cli(["api", "user"], write=False, use=use,
                                   json_expected=False)
    assert not tool.ran, "o subprocesso foi criado apesar da recusa"
    return erro.value


def test_without_a_grant_there_is_no_process(bench, tool, segredo):
    """Trabalhar num workspace nao e permissao para usar credencial dele."""
    credencial(bench)
    sem_nada = Principal(subject=ALICE.subject, display="alice",
                         method="os-account", provider="os-account",
                         issuer="maquina", authenticated_at=T0)
    erro = _refusado(tool, porta(bench, actor=sem_nada))
    assert SENTINELA not in str(erro)


def test_without_a_policy_rule_there_is_no_process(bench, tool, segredo):
    """A policy e uma barreira separada, e nao um resumo das outras."""
    credencial(bench)
    erro = _refusado(tool, porta(bench, rules=[{"name": "vazio", "effect": "ALLOW",
                                                "match": {"action": ["nada.disto"]}}]))
    assert erro.refusal == "POLICY_DENIED"


def test_a_revoked_credential_starts_no_process(bench, tool, segredo):
    credencial(bench, revogada=True)
    erro = _refusado(tool, porta(bench))
    assert erro.refusal == "REVOKED"


def test_an_expired_credential_starts_no_process(bench, tool, segredo):
    credencial(bench, expires_at=T0 - timedelta(seconds=1))
    erro = _refusado(tool, porta(bench))
    assert erro.refusal == "EXPIRED"


def test_a_read_credential_opens_no_pull_request(bench, tool, segredo):
    """A capacidade e da CREDENCIAL, e nao do que o token tecnicamente faria.

    Este e o caso que separa "o provedor aceitaria" de "o Regente autoriza". Um
    token com alcance de escrita nao ganha autoridade de escrita por existir.
    """
    credencial(bench, capabilities=("repo.read",))
    erro = _refusado(tool, porta(bench), use=Use.REPO_PR)
    assert erro.refusal == "NO_CAPABILITY"
    assert "repo.pr" in erro.reason


def test_with_the_capability_the_pull_request_call_does_run(bench, tool, segredo):
    """A contraparte: com a capacidade, o mesmo caminho passa.

    Sem isto, a recusa acima poderia ser um adapter quebrado em vez de uma
    barreira funcionando.
    """
    credencial(bench, capabilities=("repo.read", "repo.pr"))
    adapter(tool, porta(bench))._cli(
        ["pr", "create", "--repo", "acme/thing", "--head", "b", "--base", "main",
         "--title", "t", "--body", "corpo"],
        write=True, use=Use.REPO_PR, json_expected=False)
    assert tool.received()["env"].get("GH_TOKEN") == SENTINELA


# ===========================================================================
# ESCOPO
# ===========================================================================

def test_a_broker_of_one_workspace_cannot_reach_anothers_credential(
        bench, tool, segredo):
    credencial(bench, workspace="wks_b", client="cli_b")
    erro = _refusado(tool, porta(bench, actor=quem(workspace="wks_a")))
    assert erro.refusal == "NOT_FOUND"


def test_a_broker_of_one_provider_cannot_reach_anothers_credential(
        bench, tool, segredo):
    """Uma credencial de board nao serve para falar com o repositorio."""
    credencial(bench, provider="tasks", capabilities=("task.read",))
    erro = _refusado(tool, porta(bench, provider="repository_write"))
    assert erro.refusal == "NOT_FOUND"


def test_the_adapter_cannot_change_the_provider_it_asks_for(bench, tool, segredo):
    """O provider entra na CONSTRUCAO do broker, feita pela composicao.

    Se `material()` aceitasse um provider, o adapter escolheria o mais
    conveniente -- e a credencial de menor escopo seria a menos usada.
    """
    import inspect

    from regente.engine.credentials import BoundBroker

    assinatura = inspect.signature(BoundBroker.material)
    assert list(assinatura.parameters) == ["self", "use"]


# ===========================================================================
# A PORTA -- e o que acontece sem ela
# ===========================================================================

def test_an_adapter_without_the_door_refuses_instead_of_looking_around(
        bench, tool, segredo):
    """Nao ha caminho alternativo: sem porta, ele para. Nao consulta ambiente."""
    with pytest.raises(AdapterError, match="nao vai ler"):
        adapter(tool, None)._cli(["api", "user"], write=False,
                                 use=Use.REPO_READ, json_expected=False)
    assert not tool.ran


def test_an_invocation_that_needs_no_credential_asks_for_none(bench, tool):
    """`--version` prova que a ferramenta RODA, e nao que ela esta autenticada.

    Se fosse a mesma pergunta, `doctor` precisaria de credencial -- e um comando
    de saude seria o caminho mais curto para extrair material.
    """
    class PortaQueConta:
        def __init__(self):
            self.pedidos = []

        def material(self, use):
            self.pedidos.append(use)
            return SENTINELA

        def allows(self, use):
            return True

    p = PortaQueConta()
    GitHubWrite(org="acme", cli_path=str(tool.path), credentials=p).verify()
    assert p.pedidos == [], "uma checagem de saude resolveu material"
    assert tool.ran
    assert "GH_TOKEN" not in tool.received()["env"]


def test_the_environment_recipe_holds_names_never_material():
    """Construir nao e autorizar: o objeto guarda NOME, e pede na hora."""
    class PortaQueConta:
        def __init__(self):
            self.pedidos = []

        def material(self, use):
            self.pedidos.append(use)
            return SENTINELA

        def allows(self, use):
            return True

    p = PortaQueConta()
    receita = cli_process.child_environment(("GH_TOKEN",), p)
    assert p.pedidos == [], "a construcao ja resolveu material"
    assert SENTINELA not in repr(receita)

    pronto = receita.launch(Use.REPO_READ)
    assert p.pedidos == [Use.REPO_READ]
    assert pronto.env["GH_TOKEN"] == SENTINELA
    # E nem o objeto pronto se imprime inteiro.
    assert SENTINELA not in repr(pronto) and SENTINELA not in str(pronto)


# ===========================================================================
# REVOGACAO -- o mesmo broker, depois
# ===========================================================================

def test_revoking_closes_the_door_on_the_very_next_call(bench, tool, segredo):
    """Prova que nao existe material cacheado em lugar nenhum.

    O MESMO objeto broker, o MESMO adapter. Se a primeira chamada tivesse
    guardado o material, a segunda continuaria funcionando -- e a revogacao
    seria uma anotacao no banco sem efeito no mundo.
    """
    c = credencial(bench)
    broker = porta(bench)
    ferramenta = adapter(tool, broker)

    ferramenta._cli(["api", "user"], write=False, use=Use.REPO_READ,
                    json_expected=False)
    assert tool.received()["env"]["GH_TOKEN"] == SENTINELA

    (tool.root / "recebido.json").unlink()
    assert bench.revoke_credential("wks_a", c.id, ALICE.key, T0), \
        "a revogacao nao pegou; sem isso o resto do teste nao prova nada"

    with pytest.raises(CredentialDenied) as erro:
        ferramenta._cli(["api", "user"], write=False, use=Use.REPO_READ,
                        json_expected=False)
    assert erro.value.refusal == "REVOKED"
    assert not tool.ran, "o subprocesso rodou depois da revogacao"


# ===========================================================================
# VAZAMENTO
# ===========================================================================

def test_a_tool_that_echoes_the_credential_leaks_it_nowhere(bench, tool, segredo):
    """O caso real: um provedor que recusa costuma citar o que recebeu.

    O `stderr` de um adapter vira evento persistido -- e o banco e um arquivo
    que sai em backup, em anexo de bug e em captura de tela.
    """
    credencial(bench)
    tool.echo_the_credential_back()

    eventos = []

    def observador(call):
        eventos.append(call)

    ferramenta = GitHubWrite(org="acme", cli_path=str(tool.path),
                             credentials=porta(bench), observer=observador)
    with pytest.raises(Exception) as erro:
        ferramenta._cli(["api", "user"], write=False, use=Use.REPO_READ,
                        json_expected=False)

    # O filho REALMENTE recebeu o material e REALMENTE o devolveu no stderr --
    # senao este teste passaria por nao ter havido vazamento a conter.
    assert tool.received()["env"]["GH_TOKEN"] == SENTINELA
    assert eventos, "nenhum evento foi emitido; nao ha o que verificar"

    assert SENTINELA not in str(erro.value)
    assert SENTINELA not in repr(erro.value)
    for e in eventos:
        assert SENTINELA not in json.dumps(dataclasses.asdict(e), default=str), \
            "o material sobreviveu ate o evento"
        assert "<credencial>" in (e.error or ""), \
            "o material sumiu sem deixar marca; um alvo que some parece nunca ter existido"


def test_the_credential_never_reaches_the_database(bench, tool, segredo):
    """O teste mais bruto que existe: procurar o valor no arquivo do banco."""
    credencial(bench)
    tool.echo_the_credential_back()

    def observador(call):
        bench.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id="wks_a",
            kind="chamada_provedor", actor="teste",
            summary=f"{call.operation} {call.path} {call.duration_ms}ms",
            data={"operation": call.operation, "error": call.error}))

    ferramenta = GitHubWrite(org="acme", cli_path=str(tool.path),
                             credentials=porta(bench), observer=observador)
    with pytest.raises(Exception):
        ferramenta._cli(["api", "user"], write=False, use=Use.REPO_READ,
                        json_expected=False)

    bench.close()
    bruto = Path(bench.path).read_bytes()
    assert b"chamada_provedor" in bruto, \
        "o evento nao chegou ao arquivo; a varredura nao provaria nada"
    assert SENTINELA.encode() not in bruto, "o material foi parar no banco"


def test_the_credential_never_reaches_what_the_adapter_says_about_itself(
        bench, tool, segredo):
    """`describe()` sai em diagnostico e em relatorio."""
    credencial(bench)
    ferramenta = adapter(tool, porta(bench))
    ferramenta._cli(["api", "user"], write=False, use=Use.REPO_READ,
                    json_expected=False)

    texto = json.dumps(ferramenta.describe()) + repr(ferramenta)
    assert SENTINELA not in texto
    assert "governed" in json.dumps(ferramenta.describe())


def test_the_scrub_is_the_existing_redaction_not_a_second_one():
    """Duas implementacoes de redacao divergem, e a que diverge e a que esquece.

    A daqui compoe com a que ja existia -- `redact_url`, do marco 11 -- em vez
    de reimplementa-la.
    """
    from regente.core import redaction

    pronto = ChildLaunch(env={}, secrets=(SENTINELA,))
    sujo = f"falhou em https://x-access-token:{SENTINELA}@host/o/r e {SENTINELA}"
    limpo = pronto.scrub(sujo)

    assert SENTINELA not in limpo
    assert not redaction.carries_credential(limpo), "userinfo de URL sobreviveu"
    assert not redaction.carries(limpo, (SENTINELA,))


# ===========================================================================
# A MESMA PORTA PARA TODOS
# ===========================================================================

def test_every_cli_adapter_uses_the_same_door(bench, tool, segredo):
    """Tres montagens divergem; a que divergir e a que esquece do isolamento."""
    credencial(bench, provider="cicd", capabilities=("ci.read",))
    GitHubChecks(org="acme", cli_path=str(tool.path),
                 credentials=porta(bench, provider="cicd"))._cli(
        ["api", "repos/acme/thing/commits/abc/check-runs"],
        json_expected=False, use=Use.CI_READ)

    ambiente = tool.received()["env"]
    assert ambiente["GH_TOKEN"] == SENTINELA
    assert ambiente["GH_CONFIG_DIR"] == cli_process.NO_CONFIG_DIR
    assert ambiente["GH_PROMPT_DISABLED"] == "1"


def test_a_ci_credential_does_not_authorise_opening_a_pull_request(
        bench, tool, segredo):
    credencial(bench, provider="cicd", capabilities=("ci.read",))
    with pytest.raises(CredentialDenied) as erro:
        GitHubChecks(org="acme", cli_path=str(tool.path),
                     credentials=porta(bench, provider="cicd"))._cli(
            ["api", "x"], json_expected=False, use=Use.REPO_PR)
    assert erro.value.refusal == "NO_CAPABILITY"
    assert not tool.ran


# ===========================================================================
# CONTRAPROVAS ESTRUTURAIS -- a forma do caminho antigo nao existe
# ===========================================================================

import ast  # noqa: E402  (o resto do arquivo e comportamento; isto e forma)

ADAPTERS = Path("regente/adapters")

#: Onde material PODE entrar num processo filho. Em mais nenhum lugar.
#: `registry.py` aparece por um motivo diferente e nomeado: ele monta o
#: callback de credencial de um transporte HTTP, que nao cria processo nenhum.
PODEM_PEDIR_MATERIAL = {
    "regente/adapters/childproc.py",
    "regente/adapters/registry.py",
}


def _chamadas(path: Path):
    return ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))


def test_material_enters_a_child_process_in_exactly_one_place():
    """Uma porta so. Duas divergem, e a que diverge esquece do allowlist.

    Este e o teste que fica caro de burlar: qualquer adapter novo que tente
    pedir material por conta propria aparece aqui, mesmo que funcione.
    """
    culpados = []
    for path in sorted(ADAPTERS.rglob("*.py")):
        if path.as_posix() in PODEM_PEDIR_MATERIAL:
            continue
        for no in _chamadas(path):
            if (isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute)
                    and no.func.attr == "material"):
                culpados.append(f"{path.as_posix()}:{no.lineno}")
    assert not culpados, ("material pedido fora da porta unica:\n  "
                          + "\n  ".join(culpados))


def test_no_adapter_starts_a_subprocess_with_the_inherited_environment():
    """O defeito exato que este marco fechou, em forma verificavel.

    Ate aqui, `subprocess.run` sem `env=` entregava ao filho o ambiente inteiro
    do motor -- e a ferramenta encontrava sozinha uma credencial que ninguem lhe
    deu. Herdar tem de ser uma decisao escrita, nunca o default.
    """
    #: Os que deliberadamente NAO passam credencial e nao a herdam: o clone
    #: local e a sonda, que entrega material por stdin de proposito.
    #: `git` fica de fora deste marco e o levantamento diz por que.
    fora = {"regente/adapters/probe.py",
            "regente/adapters/repos/git_local.py",
            "regente/adapters/workspace/local.py",
            "regente/adapters/runner/cli_agent.py"}
    culpados = []
    for path in sorted(ADAPTERS.rglob("*.py")):
        if path.as_posix() in fora:
            continue
        for no in _chamadas(path):
            if not (isinstance(no, ast.Call)
                    and "subprocess.run" in ast.unparse(no.func)):
                continue
            if not any(k.arg == "env" for k in no.keywords):
                culpados.append(f"{path.as_posix()}:{no.lineno}")
    assert not culpados, ("subprocesso herda o ambiente do motor:\n  "
                          + "\n  ".join(culpados))


def test_no_adapter_puts_credential_material_into_argv():
    """A forma proibida: um valor de credencial dentro da lista de argumentos.

    `argv` e legivel por qualquer usuario da maquina. Este teste procura a
    FORMA -- uma chamada de subprocesso cujo primeiro argumento cite material,
    credencial ou token -- e nao um nome especifico que alguem renomeia.
    """
    marcas = ("material", "token", "secret", "credencial", "credential")
    culpados = []
    for path in sorted(ADAPTERS.rglob("*.py")):
        for no in _chamadas(path):
            if not (isinstance(no, ast.Call)
                    and "subprocess" in ast.unparse(no.func)):
                continue
            argv = ast.unparse(no.args[0]).lower() if no.args else ""
            if any(m in argv for m in marcas):
                culpados.append(f"{path.as_posix()}:{no.lineno}: {argv[:60]}")
    assert not culpados, ("material em argv:\n  " + "\n  ".join(culpados))


def test_the_transport_never_writes_the_material_anywhere():
    """Nenhum arquivo de segredo, nenhum `.env`, nenhuma linha de banco.

    O material existe em memoria pelo tempo de montar o ambiente do filho. Este
    marco nao promete apagamento seguro de memoria em Python -- promete que o
    valor nao e ESCRITO em lugar nenhum, que e uma afirmacao verificavel.
    """
    porta_unica = Path("regente/adapters/childproc.py")

    # Por CHAMADA, e nao por substring. A primeira versao deste teste procurava
    # a palavra "log" no arquivo e acusava a propria prosa que explica por que
    # nada e logado -- um guard que acusa a propria explicacao ensina a ignorar
    # o guard.
    escrita = {"open", "print", "write_text", "write_bytes", "mkdir",
               "NamedTemporaryFile", "mkstemp", "mkdtemp", "dump", "dumps",
               "info", "debug", "warning", "error", "exception"}
    culpados = []
    for no in _chamadas(porta_unica):
        if not isinstance(no, ast.Call):
            continue
        nome = (no.func.attr if isinstance(no.func, ast.Attribute)
                else getattr(no.func, "id", ""))
        if nome in escrita:
            culpados.append(f"childproc.py:{no.lineno}: {nome}()")
    assert not culpados, ("a porta de subprocesso escreve em algum lugar; ela "
                          "so monta um dict:\n  " + "\n  ".join(culpados))

    # E nao importa nada que saiba persistir ou registrar.
    importados = {ast.unparse(n) for n in _chamadas(porta_unica)
                  if isinstance(n, (ast.Import, ast.ImportFrom))}
    assert not any(m in " ".join(importados)
                   for m in ("logging", "sqlite3", "tempfile", "store")), \
        f"a porta importa algo que persiste: {importados}"
