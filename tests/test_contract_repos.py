# -*- coding: utf-8 -*-
"""Contrato do RepositoryProvider. Vale para TODO adapter, presente e futuro.

Roda contra dois provedores deliberadamente do tipo mais diferente possivel:
processo local sobre repositorios git de verdade, e CLI contra a hospedagem
remota. Se o contrato vale para os dois, ele nao esta escrito em volta de um.

O provedor local roda sobre repositorios git REAIS criados na hora -- nao ha
simulacao de git em lugar nenhum. O remoto exige rede e credencial, entao roda
sob marcador: `pytest -m rede`. A suite padrao continua rapida e offline, e o
contrato continua sendo o mesmo codigo nos dois casos.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from regente.adapters.repos.git_local import GitLocal, _org_repo
from regente.adapters.repos.github import GitHubRepos
from regente.adapters.repos.readonly import cli_e_leitura, git_e_leitura
from regente.ports import AdapterError, ReadOnlyRefused
from regente.ports.repository import RepoCapability, RepoRef, RepositoryProvider


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, encoding="utf-8", errors="replace")


def _cria_repo(root: Path, diretorio: str, remoto: str | None,
               base: str = "main", branches: tuple[str, ...] = ()) -> Path:
    """Cria um repositorio git DE VERDADE. Nada aqui e simulado."""
    p = root / diretorio
    p.mkdir(parents=True)
    _git(p, "init", "-q", "-b", base)
    _git(p, "config", "user.email", "teste@exemplo.invalido")
    _git(p, "config", "user.name", "Teste")
    (p / "README.md").write_text(f"# {diretorio}\n", encoding="utf-8")
    _git(p, "add", "README.md")
    _git(p, "commit", "-q", "-m", "inicial")
    for b in branches:
        _git(p, "branch", b)
    if remoto:
        _git(p, "remote", "add", "origin", remoto)
        # `origin/HEAD` sem fetch: o adapter precisa achar a base mesmo assim,
        # e este e o estado de um clone recem-configurado.
        _git(p, "update-ref", f"refs/remotes/origin/{base}", "HEAD")
        _git(p, "symbolic-ref", "refs/remotes/origin/HEAD",
             f"refs/remotes/origin/{base}")
    return p


@pytest.fixture
def clones(tmp_path) -> Path:
    root = tmp_path / "clones"
    root.mkdir()
    _cria_repo(root, "api", "https://github.com/acme/servico-api.git",
               branches=("feat/K-1-coisa", "fix/K-2-outra"))
    _cria_repo(root, "web", "git@github.com:acme/web.git", base="master")
    _cria_repo(root, "sem-remoto", None)
    return root


@pytest.fixture
def provider(clones) -> RepositoryProvider:
    return GitLocal(root=clones)


NETWORK = pytest.mark.skipif(
    os.environ.get("REGENTE_TESTE_REDE") != "1",
    reason="exige rede e credencial; ligue com REGENTE_TESTE_REDE=1")


# ---------------------------------------------------------------------------
# Identidade -- o nucleo do contrato
# ---------------------------------------------------------------------------

def test_declares_the_that_is(provider):
    d = provider.describe()
    assert d["capability"] == "repository"
    assert d["adapter"] and d["adapter"] != "unknown"


def test_list_returns_repositorios(provider):
    repos = provider.list_repositories()
    assert repos
    assert all(r.ref.key for r in repos)


def test_identity_vem_of_remote_not_of_directory(provider):
    """O defeito que este teste impede foi medido no disco real: o diretorio
    `scamchecker-legado` aponta para o repositorio `scamchecker`."""
    por_dir = {r.data["directory"]: r for r in provider.list_repositories()}
    assert por_dir["api"].ref.key == "acme/servico-api"
    assert por_dir["api"].data.get("diretorio_diverge_do_repo") is True
    assert por_dir["web"].ref.key == "acme/web"


def test_identity_is_unique(provider):
    chaves = [r.ref.key for r in provider.list_repositories()]
    assert len(chaves) == len(set(chaves))


def test_identity_carries_the_provider(provider):
    for r in provider.list_repositories():
        assert r.ref.provider == provider.name
        assert str(r.ref) == f"{r.ref.provider}:{r.ref.key}"


def test_resource_is_scoped_pelo_workspace(provider):
    """Repositorio homonimo em dois clientes precisa dar recursos DIFERENTES."""
    r = provider.list_repositories()[0]
    a = r.ref.resource("wks_a")
    b = r.ref.resource("wks_b")
    assert a != b
    assert "wks_a" in a and "wks_b" in b


def test_without_remote_ainda_tem_identity(provider):
    """Ausencia de remoto nao pode virar ausencia de repositorio."""
    chaves = {r.ref.key for r in provider.list_repositories()}
    assert any(k.startswith("local/") for k in chaves)


def test_get_returns_the_same_that_the_list(provider):
    da_lista = provider.list_repositories()[0]
    um = provider.get_repository(da_lista.ref.key)
    assert um.ref == da_lista.ref
    assert um.base_branch == da_lista.base_branch


def test_repositorio_missing_raises(provider):
    with pytest.raises(AdapterError):
        provider.get_repository("nao/existe-999")


# ---------------------------------------------------------------------------
# Branch base -- presumir 'main' custa o trabalho inteiro
# ---------------------------------------------------------------------------

def test_branch_base_is_read_never_assumed(provider):
    por_dir = {r.data["directory"]: r for r in provider.list_repositories()}
    assert por_dir["api"].base_branch == "main"
    assert por_dir["web"].base_branch == "master", "presumiu 'main'"


def test_branch_current_not_is_confused_with_the_base(provider, clones):
    """Dos 12 clones reais examinados, 11 estavam numa branch de trabalho."""
    _git(clones / "api", "checkout", "-q", "feat/K-1-coisa")
    r = provider.get_repository("acme/servico-api")
    assert r.data["branch_corrente"] == "feat/K-1-coisa"
    assert r.base_branch == "main"


def test_list_branches_without_duplicating_local_is_remote(provider):
    nomes = [b.name for b in provider.list_branches("acme/servico-api")]
    assert len(nomes) == len(set(nomes))
    assert "feat/K-1-coisa" in nomes
    assert sum(1 for b in provider.list_branches("acme/servico-api") if b.e_base) == 1


def test_filter_of_branch(provider):
    achadas = provider.list_branches("acme/servico-api", {"padrao": "K-1"})
    assert [b.name for b in achadas] == ["feat/K-1-coisa"]


# ---------------------------------------------------------------------------
# Capacidades: poder e permissao sao coisas diferentes
# ---------------------------------------------------------------------------

def test_declares_capabilities(provider):
    for r in provider.list_repositories():
        assert r.capabilities
        assert r.can(RepoCapability.READ_METADATA)


def test_not_declares_capacidade_of_write_neste_milestone(provider):
    from regente.ports.repository import WRITE_CAPS
    assert not (provider.capabilities & WRITE_CAPS), (
        "adapter de leitura declarando poder de escrita")


def test_capacidade_missing_is_declared_is_not_simulated(provider):
    """Git puro nao tem pull request -- e o adapter precisa DIZER isso."""
    assert not provider.list_repositories()[0].can(RepoCapability.READ_PULL_REQUESTS)


# ---------------------------------------------------------------------------
# Shadow mode: por INVOCACAO, nunca por verbo
# ---------------------------------------------------------------------------

PORT_WRITE_OPS = [
    ("create_branch", ("k", "b", "base")),
    ("create_commit", ("k", "b", "msg", {})),
    ("push", ("k", "b")),
    ("create_pull_request", ("k", "b", "base", "t", "c")),
    ("submit_review", ("k", 1, "sha", "corpo", "APPROVE")),
    ("merge_pull_request", ("k", 1)),
]


@pytest.mark.parametrize("operation,args", PORT_WRITE_OPS)
def test_write_not_esta_implemented(provider, operation, args):
    """O contrato existe; a implementacao nao. As duas coisas sao visiveis."""
    with pytest.raises(NotImplementedError):
        getattr(provider, operation)(*args)


@pytest.mark.parametrize("invocation", [
    ["commit", "-m", "x"], ["push"], ["remote", "set-url", "origin", "x"],
    ["remote", "add", "x", "y"], ["branch", "-D", "x"],
    ["symbolic-ref", "HEAD", "refs/heads/x"], ["config", "user.email", "x"],
    ["checkout", "x"], ["reset", "--hard"], ["clean", "-fd"],
])
def test_git_refuses_invocation_that_writes(provider, clones, invocation):
    """Cada um destes comeca com um verbo que tem forma de leitura -- e por isso
    um allowlist por verbo os deixaria passar."""
    with pytest.raises(ReadOnlyRefused):
        provider._git(clones / "api", *invocation)


@pytest.mark.parametrize("invocation", [
    ["repo", "delete", "x"], ["repo", "create", "x"], ["repo", "edit", "x"],
    ["repo", "archive", "x"], ["repo", "rename", "x"],
    ["pr", "create"], ["pr", "merge", "1"], ["pr", "close", "1"],
    ["pr", "review", "1", "--approve"], ["pr", "comment", "1"],
    ["api", "--method", "POST", "repos/x"], ["api", "-X", "DELETE", "repos/x"],
    ["api", "-f", "a=b", "repos/x"], ["api", "-F", "a=b", "repos/x"],
    ["api", "--input", "-", "repos/x"],
    ["release", "create"], ["workflow", "run"], ["secret", "set"],
])
def test_cli_refuses_invocation_that_writes(invocation):
    """`repo delete` atravessou um allowlist por verbo em 06/09/2026. Nunca mais."""
    ok, reason = cli_e_leitura(invocation)
    assert not ok, f"'{' '.join(invocation)}' passou pelo portao"
    assert reason


@pytest.mark.parametrize("invocation", [
    ["repo", "list", "org"], ["repo", "view", "x"], ["pr", "view", "1"],
    ["api", "repos/x/y"], ["api", "--method", "GET", "repos/x"], ["auth", "status"],
])
def test_cli_allows_read(invocation):
    ok, reason = cli_e_leitura(invocation)
    assert ok, f"'{' '.join(invocation)}' recusado: {reason}"


@pytest.mark.parametrize("invocation", [
    ["rev-parse", "HEAD"], ["remote", "get-url", "origin"],
    ["symbolic-ref", "--short", "HEAD"], ["for-each-ref", "refs/heads"],
    ["show", "HEAD:README.md"], ["config", "--get", "x"],
])
def test_git_allows_read(invocation):
    ok, reason = git_e_leitura(invocation)
    assert ok, f"'git {' '.join(invocation)}' recusado: {reason}"


# ---------------------------------------------------------------------------
# Dados ausentes, malformados e indisponibilidade
# ---------------------------------------------------------------------------

def test_root_missing_is_error_not_list_empty(tmp_path):
    p = GitLocal(root=tmp_path / "nao-existe")
    with pytest.raises(AdapterError):
        p.list_repositories()


def test_directory_without_git_is_ignored_without_breaking(provider, clones):
    (clones / "nao-e-repo").mkdir()
    (clones / "nao-e-repo" / "arquivo.txt").write_text("x", encoding="utf-8")
    assert len(provider.list_repositories()) == 3


def test_timeout_becomes_error_of_adapter(provider, clones, monkeypatch):
    """Testa a TRADUCAO do timeout, nao a corrida.

    Um teste que confia em `timeout=0` disparar de fato depende de o processo
    ser mais lento que a granularidade do relogio -- e portanto falha de vez em
    quando, na maquina errada, sem ninguem entender por que.
    """
    def estoura(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=0.1)
    monkeypatch.setattr(subprocess, "run", estoura)
    with pytest.raises(AdapterError, match="estourou"):
        provider._git(clones / "api", "rev-parse", "HEAD")


def test_file_missing_is_error_clear(provider):
    with pytest.raises(AdapterError):
        provider.read_file("acme/servico-api", "nao/existe.txt")


def test_le_file_of_base_by_padrao(provider):
    assert provider.read_file("acme/servico-api", "README.md").strip() == "# api"


@pytest.mark.parametrize("url,esperado", [
    ("https://github.com/acme/repo.git", "acme/repo"),
    ("git@github.com:acme/repo.git", "acme/repo"),
    ("https://gitlab.com/grupo/sub/repo", "sub/repo"),
    ("ssh://git@bitbucket.org/time/repo.git", "time/repo"),
    ("/caminho/local/repo", "local/repo"),
])
def test_identity_exits_of_qualquer_forma_of_url(url, esperado):
    """`git@host:org/repo.git` nao e uma URL valida e escapa de todo parser."""
    assert _org_repo(url) == esperado


# ---------------------------------------------------------------------------
# O outro provedor -- mesmo contrato, rede de verdade
# ---------------------------------------------------------------------------

@NETWORK
def test_hosting_remote_meets_the_same_contract():
    p = GitHubRepos(org=os.environ.get("REGENTE_TESTE_ORG", "silverguard-br"),
                    cli_path=os.environ.get("REGENTE_GH", "gh"))
    p.verify()
    repos = p.list_repositories()
    assert repos
    assert all(r.ref.key and "/" in r.ref.key for r in repos)
    assert all(r.ref.provider == "github" for r in repos)
    assert len({r.ref.key for r in repos}) == len(repos)

    um = p.get_repository(repos[0].ref.key)
    assert um.ref == repos[0].ref
    assert not um.partial and repos[0].partial


@NETWORK
def test_hosting_remote_refuses_write():
    p = GitHubRepos(org="qualquer", cli_path=os.environ.get("REGENTE_GH", "gh"))
    with pytest.raises(ReadOnlyRefused):
        p._cli(["repo", "delete", "qualquer/coisa"])
