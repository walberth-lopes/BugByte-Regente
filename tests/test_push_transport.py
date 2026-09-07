# -*- coding: utf-8 -*-
"""O `git push` governado, provado contra um `git push` de verdade.

O caminho de entrega foi provado por cinco marcos contra `FakeAreas`. O dublê
nao tem a linha que derrubava o push real -- `_looks_local`, chamada duas vezes
e definida em lugar nenhum -- e por isso `GitClone.push` levantava `NameError`
em toda execucao real sem nenhum teste ficar vermelho.

Aqui nao ha dublê. Um servidor git HTTP roda no loopback, exige `Authorization`,
e o repositorio nu do outro lado diz o que REALMENTE chegou:

    servidor.refs()      -> o push aconteceu, e este e o commit que chegou
    servidor.requests    -> vazio prova que nada saiu da maquina
    servidor.authorizations -> o material atravessou de verdade

A ordem das afirmacoes importa. "Levantou uma excecao" nao prova que nada
aconteceu: um adapter pode empurrar e so entao reclamar. Toda recusa aqui afirma
tambem que o servidor continua intocado.
"""

from __future__ import annotations

import base64
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from regente.adapters.childproc import ChildEnvironment
from regente.adapters.secrets import ScopedSecrets
from regente.adapters.workspace import git_process
from regente.adapters.workspace.local import GitClone, IsolatedDirectory
from regente.core import ids
from regente.core.access import PrincipalRef, abilities_of
from regente.core.credential import Credential, SecretRef, Use, uses_from
from regente.core.model import Workspace
from regente.core.policy import PolicyEngine
from regente.core.principal import Principal
from regente.engine.credentials import CredentialService
from regente.engine.store_sqlite import SqliteStore
from regente.ports import AdapterError
from regente.ports.support import CredentialDenied

from githttp import GitServer, a_work_area

T0 = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)

#: Longo, unico e reconhecivel. Procurado depois em argv, remote, gitconfig,
#: log, evento, excecao, banco e saida.
SENTINELA = "ghs_SENTINELA_DE_PUSH_naoDeveVazarEmLugarNenhum_0001"
USUARIO = "x-access-token"

ALICE = PrincipalRef("os-account", "S-1-5-21-1")
REPO = "acme/thing"

REGRAS = [{"name": "usa", "effect": "ALLOW",
           "match": {"action": ["repo.push", "repo.read", "repo.pr"]}}]


# ---------------------------------------------------------------------------
# O mundo real: um servidor git, um clone, e o caminho governado
# ---------------------------------------------------------------------------

@pytest.fixture
def servidor(tmp_path):
    s = GitServer(tmp_path / "remoto", USUARIO, SENTINELA)
    yield s
    s.close()


@pytest.fixture
def bench(tmp_path):
    store = SqliteStore(tmp_path / "t.db", clock=lambda: T0)
    store.migrate()
    for wid, client in (("wks_a", "cli_a"), ("wks_b", "cli_b")):
        store.save_client(client, "org", wid)
        store.save_workspace(Workspace(id=wid, client_id=client, name="main"))
    yield store
    store.close()


@pytest.fixture
def segredo(monkeypatch):
    monkeypatch.setenv("TOKEN_DE_PUSH", SENTINELA)
    return SENTINELA


def quem(workspace="wks_a", role="owner") -> Principal:
    return Principal(subject=ALICE.subject, display="alice", method="os-account",
                     provider="os-account", issuer="maquina", authenticated_at=T0,
                     abilities={workspace: abilities_of(role)})


def credencial(store, workspace="wks_a", client="cli_a",
               provider="repository_write", capabilities=("repo.push",),
               revogada=False, expires_at=None):
    c = Credential(
        id=ids.new_id(ids.CREDENTIAL), client_id=client, workspace_id=workspace,
        name="principal", provider=provider, kind="token",
        secret_ref=SecretRef.parse("env:TOKEN_DE_PUSH"),
        capabilities=uses_from(capabilities), granted_by=ALICE.key,
        granted_at=T0, expires_at=expires_at,
        revoked_by=(ALICE.key if revogada else ""),
        revoked_at=(T0 if revogada else None))
    store.open_credential(c)
    return c


def porta(store, actor=None, workspace="wks_a", provider="repository_write",
          rules=None):
    servico = CredentialService(
        store=store,
        policy=PolicyEngine.from_config(REGRAS if rules is None else rules),
        secrets=ScopedSecrets(workspace="main", allow_any=True),
        clock=lambda: T0, organization="org", client="Acme",
        workspace_name="main")
    return servico.broker(actor if actor is not None else quem(), workspace,
                          provider)


def areas_de(tmp_path, servidor, broker, fonte=None, remote=None) -> GitClone:
    fonte = fonte or a_work_area(tmp_path, servidor.url)
    return GitClone(root=tmp_path / "areas", sources={REPO: str(fonte)},
                    remotes={REPO: remote or servidor.url},
                    credentials=broker, credential_user=USUARIO)


def uma_area(areas, chave="K-1"):
    """Uma area preparada com uma mudanca ja commitada. Devolve (area, sha)."""
    area = areas.prepare(chave, repo=REPO, branch=f"regente/{chave.lower()}",
                         base="main")
    (Path(area.path) / "novo.txt").write_text("mudanca\n", encoding="utf-8")
    return area, areas.commit(area, "uma mudanca")


# ===========================================================================
# O PUSH REAL
# ===========================================================================

def test_a_real_push_authenticates_only_through_the_governed_path(
        bench, tmp_path, servidor, segredo):
    """O caminho inteiro, com um `git` de verdade e um servidor de verdade."""
    credencial(bench)
    areas = areas_de(tmp_path, servidor, porta(bench))
    area, sha = uma_area(areas)

    devolvido = areas.push(area, expected_sha=sha)

    assert devolvido == sha
    assert servidor.refs().get("refs/heads/regente/k-1") == sha, \
        "o commit nao chegou ao repositorio do outro lado"
    assert servidor.esperado in servidor.authorizations, \
        "o servidor nunca viu a credencial; o push nao se autenticou por ela"


def test_without_the_broker_the_push_cannot_authenticate(
        bench, tmp_path, servidor, segredo):
    """Sem a porta, ele para. Nao procura credencial em lugar nenhum."""
    areas = areas_de(tmp_path, servidor, None)
    area, sha = uma_area(areas)

    with pytest.raises(AdapterError) as erro:
        areas.push(area, expected_sha=sha)

    assert "nao vai ler" in str(erro.value)
    assert servidor.untouched, "algo saiu da maquina apesar da recusa"


def test_the_global_credential_helper_cannot_supply_the_credential(
        bench, tmp_path, servidor, monkeypatch):
    """A contraprova do bypass ambiente, medida nas duas direcoes.

    Esta maquina tem `credential.helper = manager` na configuracao do sistema.
    Sem isolamento, qualquer `git push` do motor se autenticaria por ele -- sem
    identidade, sem concessao, sem capacidade, sem revogacao.
    """
    lar = tmp_path / "lar"
    lar.mkdir()
    ajudante = tmp_path / "ajudante.sh"
    ajudante.write_text(
        f"#!/bin/sh\necho username={USUARIO}\necho password={SENTINELA}\n",
        encoding="utf-8")
    (lar / ".gitconfig").write_text(
        f'[credential]\n\thelper = "!sh {ajudante.as_posix()}"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(lar))
    monkeypatch.setenv("USERPROFILE", str(lar))

    def helpers(ambiente):
        p = subprocess.run(["git", "config", "--get-all", "credential.helper"],
                           capture_output=True, encoding="utf-8", env=ambiente)
        return (p.stdout or "").strip()

    # 1. Sem isolamento, o `git` ENXERGA o ajudante. Sem esta metade, a outra
    #    passaria por o ajudante estar quebrado.
    cru = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "HOME", "USERPROFILE")
           if k in os.environ}
    assert "ajudante" in helpers(cru), \
        "o ajudante nao foi encontrado nem sem isolamento; o teste nao prova nada"

    # 2. Com o ambiente que o adapter monta, ele desaparece.
    isolado = ChildEnvironment(
        allow=git_process.ALLOWLIST, fixed=git_process.fixed_variables()).plain()
    assert helpers(isolado.env) == "", \
        "a configuracao global ainda fornece um ajudante de credencial"

    # 3. E o push de verdade, sem porta, nao se autentica por ele.
    areas = areas_de(tmp_path, servidor, None)
    area, sha = uma_area(areas)
    with pytest.raises(AdapterError):
        areas.push(area, expected_sha=sha)
    assert servidor.untouched


def test_the_legitimate_mechanisms_git_needs_still_work(bench, tmp_path,
                                                        servidor, segredo):
    """Fechar o bypass nao pode quebrar o `git`.

    Um isolamento que impeca o push legitimo nao e seguranca, e um motor que
    nao entrega -- e o primeiro a ser desligado por quem precisa entregar.
    """
    credencial(bench)
    areas = areas_de(tmp_path, servidor, porta(bench))
    area, sha = uma_area(areas)
    areas.push(area, expected_sha=sha)

    ambiente = ChildEnvironment(allow=git_process.ALLOWLIST,
                                fixed=git_process.fixed_variables()).plain().env
    assert "PATH" in ambiente
    for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        assert proxy in git_process.ALLOWLIST, \
            f"{proxy} saiu da allowlist; quem esta atras de proxy para de clonar"
    assert servidor.refs()


# ===========================================================================
# BARREIRAS -- e, em cada caso, NADA saiu da maquina
# ===========================================================================

def _recusa(areas, area, servidor, sha, excecao=AdapterError, **kw):
    with pytest.raises(excecao) as erro:
        areas.push(area, expected_sha=sha, **kw)
    assert servidor.untouched, "o subprocesso alcancou o remoto apesar da recusa"
    return erro.value


def test_without_a_grant_nothing_leaves_the_machine(bench, tmp_path, servidor,
                                                    segredo):
    credencial(bench)
    sem_nada = Principal(subject=ALICE.subject, display="alice",
                         method="os-account", provider="os-account",
                         issuer="maquina", authenticated_at=T0)
    areas = areas_de(tmp_path, servidor, porta(bench, actor=sem_nada))
    area, sha = uma_area(areas)
    erro = _recusa(areas, area, servidor, sha, excecao=CredentialDenied)
    assert erro.refusal == "NOT_FOUND"


def test_a_read_only_credential_never_pushes(bench, tmp_path, servidor, segredo):
    """A capacidade e da CREDENCIAL, e nao do que o token tecnicamente faria."""
    credencial(bench, capabilities=("repo.read",))
    areas = areas_de(tmp_path, servidor, porta(bench))
    area, sha = uma_area(areas)
    erro = _recusa(areas, area, servidor, sha, excecao=CredentialDenied)
    assert erro.refusal == "NO_CAPABILITY"
    assert "repo.push" in erro.reason


def test_a_revoked_credential_never_pushes(bench, tmp_path, servidor, segredo):
    credencial(bench, revogada=True)
    areas = areas_de(tmp_path, servidor, porta(bench))
    area, sha = uma_area(areas)
    assert _recusa(areas, area, servidor, sha,
                   excecao=CredentialDenied).refusal == "REVOKED"


def test_an_expired_credential_never_pushes(bench, tmp_path, servidor, segredo):
    credencial(bench, expires_at=T0 - timedelta(seconds=1))
    areas = areas_de(tmp_path, servidor, porta(bench))
    area, sha = uma_area(areas)
    assert _recusa(areas, area, servidor, sha,
                   excecao=CredentialDenied).refusal == "EXPIRED"


def test_no_policy_rule_means_nothing_leaves_the_machine(bench, tmp_path,
                                                         servidor, segredo):
    credencial(bench)
    areas = areas_de(tmp_path, servidor,
                     porta(bench, rules=[{"name": "vazio", "effect": "ALLOW",
                                          "match": {"action": ["nada.disto"]}}]))
    area, sha = uma_area(areas)
    assert _recusa(areas, area, servidor, sha,
                   excecao=CredentialDenied).refusal == "POLICY_DENIED"


def test_the_wrong_sha_never_reaches_the_remote(bench, tmp_path, servidor,
                                                segredo):
    """Empurrar um commit que ninguem validou e responder por trabalho nao visto."""
    credencial(bench)
    areas = areas_de(tmp_path, servidor, porta(bench))
    area, sha = uma_area(areas)
    erro = _recusa(areas, area, servidor, "0" * 40)
    assert "the work changed after it was validated" in str(erro)


def test_a_branch_the_run_does_not_own_never_reaches_the_remote(
        bench, tmp_path, servidor, segredo):
    credencial(bench)
    areas = areas_de(tmp_path, servidor, porta(bench))
    area, sha = uma_area(areas)
    erro = _recusa(areas, area, servidor, sha, branch="regente/de-outro-run")
    assert "the area is on" in str(erro)


def test_an_integration_branch_never_reaches_the_remote(bench, tmp_path,
                                                        servidor, segredo):
    credencial(bench)
    areas = areas_de(tmp_path, servidor, porta(bench))
    area, sha = uma_area(areas)
    for proibida in ("main", "master", "develop", "production"):
        erro = _recusa(areas, area, servidor, sha, branch=proibida)
        assert "integration branch" in str(erro)


def test_a_local_target_never_reaches_a_push(bench, tmp_path, servidor, segredo):
    """A guarda que nao existia: `git` mira `origin` no que foi clonado.

    A configuracao que parece mais segura -- clonar local, e mais rapido -- e a
    que escreve no repositorio de trabalho de outra pessoa.
    """
    credencial(bench)
    fonte = a_work_area(tmp_path, servidor.url)
    areas = areas_de(tmp_path, servidor, porta(bench), fonte=fonte,
                     remote=str(tmp_path / "checkout-de-alguem"))
    area, sha = uma_area(areas)
    erro = _recusa(areas, area, servidor, sha)
    assert "local path" in str(erro)


def test_an_ssh_target_is_refused_rather_than_silently_allowed(
        bench, tmp_path, servidor, segredo):
    """`ssh` funcionaria -- pelo agente do usuario, que ninguem concedeu.

    Recusar e a resposta honesta enquanto nao ha mecanismo governado para chave
    ssh. Deixar passar seria manter o segundo caminho de autoridade que o marco
    16 removeu, com outro nome.
    """
    credencial(bench)
    fonte = a_work_area(tmp_path, servidor.url)
    areas = areas_de(tmp_path, servidor, porta(bench), fonte=fonte,
                     remote="git@github.com:acme/thing.git")
    area, sha = uma_area(areas)
    erro = _recusa(areas, area, servidor, sha)
    assert "ssh" in str(erro) and "autoridade" in str(erro)


def test_the_push_never_rewrites_history(bench, tmp_path, servidor, segredo):
    """Nenhuma forma de forcar. Nao ha flag a passar: a invocacao e fixa."""
    fonte = Path("regente/adapters/workspace/local.py").read_text(encoding="utf-8")
    empurra = fonte[fonte.index('self._git("push"'):]
    empurra = empurra[:empurra.index(")")]
    for proibida in ("--force", "-f", "--force-with-lease", "--mirror",
                     "--delete", "--prune"):
        assert proibida not in empurra, f"o push monta '{proibida}'"
    assert "--set-upstream" in empurra


# ===========================================================================
# REVOGACAO -- o mesmo objeto, depois
# ===========================================================================

def test_revoking_closes_the_door_on_the_very_next_push(bench, tmp_path,
                                                        servidor, segredo):
    """Prova que nao ha material guardado: MESMO adapter, MESMO broker."""
    c = credencial(bench)
    areas = areas_de(tmp_path, servidor, porta(bench))
    area, sha = uma_area(areas)

    areas.push(area, expected_sha=sha)
    assert servidor.refs().get("refs/heads/regente/k-1") == sha

    (Path(area.path) / "outro.txt").write_text("mais\n", encoding="utf-8")
    sha2 = areas.commit(area, "outra mudanca")
    assert bench.revoke_credential("wks_a", c.id, ALICE.key, T0), \
        "a revogacao nao pegou; o resto do teste nao provaria nada"

    with pytest.raises(CredentialDenied) as erro:
        areas.push(area, expected_sha=sha2)
    assert erro.value.refusal == "REVOKED"
    assert servidor.refs().get("refs/heads/regente/k-1") == sha, \
        "o segundo push aconteceu depois da revogacao"


# ===========================================================================
# VAZAMENTO
# ===========================================================================

def test_the_credential_leaks_into_nothing_the_push_leaves_behind(
        bench, tmp_path, servidor, segredo):
    """Tudo o que sobra em disco depois de um push bem-sucedido."""
    credencial(bench)
    areas = areas_de(tmp_path, servidor, porta(bench))
    area, sha = uma_area(areas)
    areas.push(area, expected_sha=sha)

    codificado = base64.b64encode(f"{USUARIO}:{SENTINELA}".encode()).decode()

    # O remote persistido, lido pelo proprio git.
    remoto = subprocess.run(["git", "remote", "get-url", "--push", "origin"],
                            cwd=area.path, capture_output=True,
                            encoding="utf-8").stdout
    assert SENTINELA not in remoto and codificado not in remoto
    assert remoto.strip() == servidor.url

    # Todo arquivo que o push pode ter tocado, byte a byte.
    for alvo in (Path(area.path) / ".git" / "config",
                 Path(area.path) / ".git" / "FETCH_HEAD",
                 git_process.NO_CONFIG):
        p = Path(alvo)
        if p.is_file():
            bruto = p.read_bytes()
            assert SENTINELA.encode() not in bruto, f"{p} guardou o material"
            assert codificado.encode() not in bruto, f"{p} guardou o cabecalho"

    # E a arvore inteira da area, para o caso de eu nao ter pensado num arquivo.
    for p in Path(area.path).rglob("*"):
        if p.is_file() and p.stat().st_size < 2_000_000:
            bruto = p.read_bytes()
            assert SENTINELA.encode() not in bruto, f"{p} guardou o material"
            assert codificado.encode() not in bruto, f"{p} guardou o cabecalho"


def test_a_git_error_quoting_the_credential_is_scrubbed(bench, tmp_path,
                                                        servidor, segredo):
    """O `stderr` do git vira mensagem de recusa, e recusa vira registro."""
    credencial(bench)
    # Um alvo que existe e recusa: o servidor devolve 401 para outro caminho.
    areas = areas_de(tmp_path, servidor, porta(bench),
                     remote=f"http://127.0.0.1:{servidor.port}/nao-existe.git")
    area, sha = uma_area(areas)

    with pytest.raises(AdapterError) as erro:
        areas.push(area, expected_sha=sha)

    codificado = base64.b64encode(f"{USUARIO}:{SENTINELA}".encode()).decode()
    for texto in (str(erro.value), repr(erro.value)):
        assert SENTINELA not in texto
        assert codificado not in texto


def test_the_encoded_header_is_scrubbed_as_well_as_the_raw_material():
    """Codificado nao e protegido.

    Limpar so o token deixaria passar o base64 que o contem -- e quem le o log
    decodifica em um comando.
    """
    class Porta:
        def material(self, use):
            return SENTINELA

        def allows(self, use):
            return True

    pronto = git_process.child_environment(
        "https://host/o/r.git", Porta(), USUARIO).launch(Use.REPO_PUSH)
    codificado = base64.b64encode(f"{USUARIO}:{SENTINELA}".encode()).decode()

    assert codificado in pronto.env["GIT_CONFIG_VALUE_0"]
    sujo = f"falhou com {SENTINELA} e com Authorization: Basic {codificado}"
    limpo = pronto.scrub(sujo)
    assert SENTINELA not in limpo and codificado not in limpo
    assert SENTINELA not in repr(pronto) and codificado not in repr(pronto)


# ===========================================================================
# CLASSIFICACAO DO AMBIENTE
# ===========================================================================

def test_ambient_authority_never_reaches_git(bench, tmp_path, servidor,
                                             segredo, monkeypatch):
    """As variaveis que sao AUTORIDADE, e nao configuracao.

    Um agente ssh autentica sem o Regente saber. Mante-las por conveniencia
    seria manter o segundo caminho de autoridade com outro nome.
    """
    for nome in git_process.FORBIDDEN:
        monkeypatch.setenv(nome, f"valor-ambiente-de-{nome}")

    class Porta:
        def material(self, use):
            return SENTINELA

        def allows(self, use):
            return True

    pronto = git_process.child_environment("https://host/o/r.git", Porta(),
                                           USUARIO).launch(Use.REPO_PUSH)

    # A propriedade e "o VALOR do pai nunca chega", e nao "o nome nunca
    # aparece": `GIT_ASKPASS` esta nas duas listas de proposito -- proibido
    # herdar, e escrito vazio pelo Regente, que e mais forte que omitir.
    fixas = dict(git_process.fixed_variables())
    for nome in git_process.FORBIDDEN:
        if nome in fixas:
            assert pronto.env[nome] == fixas[nome], \
                f"{nome} chegou com valor que nao e o que o Regente escolheu"
        else:
            assert nome not in pronto.env, f"{nome} atravessou para o `git`"
        assert pronto.env.get(nome) != f"valor-ambiente-de-{nome}", \
            f"{nome} atravessou com o valor do processo pai"

    # E a allowlist nao contem nenhum deles, para o dia em que alguem ampliar.
    for nome in git_process.FORBIDDEN:
        assert nome not in git_process.ALLOWLIST


def test_the_child_environment_is_built_not_inherited(bench, tmp_path,
                                                      monkeypatch):
    monkeypatch.setenv("MINHA_VARIAVEL_QUALQUER", "nada de secreto")
    monkeypatch.setenv("GITHUB_TOKEN", "de outro provedor")
    ambiente = ChildEnvironment(allow=git_process.ALLOWLIST,
                                fixed=git_process.fixed_variables()).plain().env
    assert "MINHA_VARIAVEL_QUALQUER" not in ambiente
    assert "GITHUB_TOKEN" not in ambiente
    assert ambiente["GIT_TERMINAL_PROMPT"] == "0"
    assert ambiente["GIT_CONFIG_GLOBAL"] == git_process.NO_CONFIG
    assert ambiente["GIT_CONFIG_SYSTEM"] == git_process.NO_CONFIG


def test_the_header_is_scoped_to_the_remote_it_was_issued_for(bench, tmp_path):
    """Sem escopo, um redirecionamento entrega o cabecalho a quem respondeu."""
    fixas = dict(git_process.fixed_variables("https://github.com/acme/thing.git"))
    assert fixas["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"


def test_a_local_git_call_carries_no_credential_at_all(bench, tmp_path,
                                                       servidor, segredo):
    """`rev-parse`, `status`, `commit` nao pedem material a ninguem."""
    class PortaQueConta:
        def __init__(self):
            self.pedidos = []

        def material(self, use):
            self.pedidos.append(use)
            return SENTINELA

        def allows(self, use):
            return True

    p = PortaQueConta()
    areas = areas_de(tmp_path, servidor, p)
    area, sha = uma_area(areas)
    assert p.pedidos == [], "uma operacao local resolveu credencial"

    areas.push(area, expected_sha=sha)
    assert p.pedidos == [Use.REPO_PUSH], \
        "o push pediu material mais de uma vez, ou pediu a coisa errada"


# ===========================================================================
# O PROVEDOR PADRAO -- que fingia saber fazer isto
# ===========================================================================

def test_the_directory_provider_refuses_instead_of_crashing(tmp_path):
    """Ate o marco 6, os quatro metodos davam `AttributeError`.

    Chamavam `self._git`, que nao existe nesta classe -- e ela e o provedor
    PADRAO, o que a configuracao de exemplo traz. Um `AttributeError` nao e uma
    recusa: e um defeito com cara de bug do motor.
    """
    areas = IsolatedDirectory(tmp_path / "areas")
    area = areas.prepare("K-1", repo=REPO, branch="regente/k-1")

    for chamada in (lambda: areas.head(area),
                    lambda: areas.is_dirty(area),
                    lambda: areas.commit(area, "m"),
                    lambda: areas.push(area, expected_sha="x")):
        with pytest.raises(AdapterError) as erro:
            chamada()
        assert "pasta, nao um clone git" in str(erro.value)
        assert "workspace_provider: clone" in str(erro.value)

    # E continua fazendo bem o que ela existe para fazer.
    assert areas.push_target(area) is None
    assert Path(area.path).is_dir()


def test_the_material_is_never_in_the_git_argv(bench, tmp_path, servidor,
                                               segredo, monkeypatch):
    """Espia o `argv` REAL de todo `git` disparado durante um push.

    O sweep de mutacao mostrou por que este teste precisa existir. Uma mutacao
    que colocava o material em `git -c regente.token=<m> push` foi capturada --
    mas porque ela PEDIA material fora da porta unica, e nao porque alguem
    tivesse olhado o `argv`. Material que vazasse para o `argv` de dentro da
    propria porta nao seria visto por ninguem.

    E um espiao, nao um dublê: a chamada real acontece, o push real chega ao
    servidor, e o teste afirma isso antes de afirmar a ausencia -- senao
    passaria por nao ter havido push nenhum.
    """
    from regente.adapters.workspace import local as modulo

    credencial(bench)
    areas = areas_de(tmp_path, servidor, porta(bench))
    area, sha = uma_area(areas)

    vistos: list[list[str]] = []
    real = modulo.subprocess.run

    def espiao(args, *a, **kw):
        vistos.append([str(x) for x in args])
        return real(args, *a, **kw)

    monkeypatch.setattr(modulo.subprocess, "run", espiao)
    areas.push(area, expected_sha=sha)

    assert servidor.refs().get("refs/heads/regente/k-1") == sha, \
        "o push nao aconteceu; nao ha argv de push a examinar"
    assert any("push" in argv for argv in vistos), \
        "o espiao nao viu o push; ele nao esta olhando o lugar certo"

    codificado = base64.b64encode(f"{USUARIO}:{SENTINELA}".encode()).decode()
    for argv in vistos:
        texto = " ".join(argv)
        assert SENTINELA not in texto, f"material em argv: {argv[:3]}"
        assert codificado not in texto, f"cabecalho em argv: {argv[:3]}"
