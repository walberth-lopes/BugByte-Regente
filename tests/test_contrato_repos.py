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
from regente.adapters.repos.leitura import cli_e_leitura, git_e_leitura
from regente.ports import AdapterErro, SomenteLeitura
from regente.ports.repository import CapacidadeRepo, RepoRef, RepositoryProvider


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, encoding="utf-8", errors="replace")


def _cria_repo(raiz: Path, diretorio: str, remoto: str | None,
               base: str = "main", branches: tuple[str, ...] = ()) -> Path:
    """Cria um repositorio git DE VERDADE. Nada aqui e simulado."""
    p = raiz / diretorio
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
    raiz = tmp_path / "clones"
    raiz.mkdir()
    _cria_repo(raiz, "api", "https://github.com/acme/servico-api.git",
               branches=("feat/K-1-coisa", "fix/K-2-outra"))
    _cria_repo(raiz, "web", "git@github.com:acme/web.git", base="master")
    _cria_repo(raiz, "sem-remoto", None)
    return raiz


@pytest.fixture
def provedor(clones) -> RepositoryProvider:
    return GitLocal(raiz=clones)


REDE = pytest.mark.skipif(
    os.environ.get("REGENTE_TESTE_REDE") != "1",
    reason="exige rede e credencial; ligue com REGENTE_TESTE_REDE=1")


# ---------------------------------------------------------------------------
# Identidade -- o nucleo do contrato
# ---------------------------------------------------------------------------

def test_declara_o_que_e(provedor):
    d = provedor.descreve()
    assert d["capability"] == "repository"
    assert d["adapter"] and d["adapter"] != "desconhecido"


def test_lista_devolve_repositorios(provedor):
    repos = provedor.list_repositories()
    assert repos
    assert all(r.ref.key for r in repos)


def test_identidade_vem_do_remoto_nao_do_diretorio(provedor):
    """O defeito que este teste impede foi medido no disco real: o diretorio
    `scamchecker-legado` aponta para o repositorio `scamchecker`."""
    por_dir = {r.dados["diretorio"]: r for r in provedor.list_repositories()}
    assert por_dir["api"].ref.key == "acme/servico-api"
    assert por_dir["api"].dados.get("diretorio_diverge_do_repo") is True
    assert por_dir["web"].ref.key == "acme/web"


def test_identidade_e_unica(provedor):
    chaves = [r.ref.key for r in provedor.list_repositories()]
    assert len(chaves) == len(set(chaves))


def test_identidade_carrega_o_provedor(provedor):
    for r in provedor.list_repositories():
        assert r.ref.provider == provedor.nome
        assert str(r.ref) == f"{r.ref.provider}:{r.ref.key}"


def test_recurso_e_escopado_pelo_workspace(provedor):
    """Repositorio homonimo em dois clientes precisa dar recursos DIFERENTES."""
    r = provedor.list_repositories()[0]
    a = r.ref.recurso("wks_a")
    b = r.ref.recurso("wks_b")
    assert a != b
    assert "wks_a" in a and "wks_b" in b


def test_sem_remoto_ainda_tem_identidade(provedor):
    """Ausencia de remoto nao pode virar ausencia de repositorio."""
    chaves = {r.ref.key for r in provedor.list_repositories()}
    assert any(k.startswith("local/") for k in chaves)


def test_get_devolve_o_mesmo_que_a_lista(provedor):
    da_lista = provedor.list_repositories()[0]
    um = provedor.get_repository(da_lista.ref.key)
    assert um.ref == da_lista.ref
    assert um.branch_base == da_lista.branch_base


def test_repositorio_inexistente_levanta(provedor):
    with pytest.raises(AdapterErro):
        provedor.get_repository("nao/existe-999")


# ---------------------------------------------------------------------------
# Branch base -- presumir 'main' custa o trabalho inteiro
# ---------------------------------------------------------------------------

def test_branch_base_e_lida_nunca_presumida(provedor):
    por_dir = {r.dados["diretorio"]: r for r in provedor.list_repositories()}
    assert por_dir["api"].branch_base == "main"
    assert por_dir["web"].branch_base == "master", "presumiu 'main'"


def test_branch_corrente_nao_e_confundida_com_a_base(provedor, clones):
    """Dos 12 clones reais examinados, 11 estavam numa branch de trabalho."""
    _git(clones / "api", "checkout", "-q", "feat/K-1-coisa")
    r = provedor.get_repository("acme/servico-api")
    assert r.dados["branch_corrente"] == "feat/K-1-coisa"
    assert r.branch_base == "main"


def test_lista_branches_sem_duplicar_local_e_remota(provedor):
    nomes = [b.nome for b in provedor.list_branches("acme/servico-api")]
    assert len(nomes) == len(set(nomes))
    assert "feat/K-1-coisa" in nomes
    assert sum(1 for b in provedor.list_branches("acme/servico-api") if b.e_base) == 1


def test_filtro_de_branch(provedor):
    achadas = provedor.list_branches("acme/servico-api", {"padrao": "K-1"})
    assert [b.nome for b in achadas] == ["feat/K-1-coisa"]


# ---------------------------------------------------------------------------
# Capacidades: poder e permissao sao coisas diferentes
# ---------------------------------------------------------------------------

def test_declara_capacidades(provedor):
    for r in provedor.list_repositories():
        assert r.capacidades
        assert r.pode(CapacidadeRepo.LER_METADADOS)


def test_nao_declara_capacidade_de_escrita_neste_marco(provedor):
    from regente.ports.repository import ESCRITA
    assert not (provedor.capacidades & ESCRITA), (
        "adapter de leitura declarando poder de escrita")


def test_capacidade_ausente_e_declarada_e_nao_simulada(provedor):
    """Git puro nao tem pull request -- e o adapter precisa DIZER isso."""
    assert not provedor.list_repositories()[0].pode(CapacidadeRepo.LER_PULL_REQUESTS)


# ---------------------------------------------------------------------------
# Shadow mode: por INVOCACAO, nunca por verbo
# ---------------------------------------------------------------------------

ESCRITAS_PORTA = [
    ("create_branch", ("k", "b", "base")),
    ("create_commit", ("k", "b", "msg", {})),
    ("push", ("k", "b")),
    ("create_pull_request", ("k", "b", "base", "t", "c")),
    ("submit_review", ("k", 1, "sha", "corpo", "APPROVE")),
    ("merge_pull_request", ("k", 1)),
]


@pytest.mark.parametrize("operacao,args", ESCRITAS_PORTA)
def test_escrita_nao_esta_implementada(provedor, operacao, args):
    """O contrato existe; a implementacao nao. As duas coisas sao visiveis."""
    with pytest.raises(NotImplementedError):
        getattr(provedor, operacao)(*args)


@pytest.mark.parametrize("invocacao", [
    ["commit", "-m", "x"], ["push"], ["remote", "set-url", "origin", "x"],
    ["remote", "add", "x", "y"], ["branch", "-D", "x"],
    ["symbolic-ref", "HEAD", "refs/heads/x"], ["config", "user.email", "x"],
    ["checkout", "x"], ["reset", "--hard"], ["clean", "-fd"],
])
def test_git_recusa_invocacao_que_grava(provedor, clones, invocacao):
    """Cada um destes comeca com um verbo que tem forma de leitura -- e por isso
    um allowlist por verbo os deixaria passar."""
    with pytest.raises(SomenteLeitura):
        provedor._git(clones / "api", *invocacao)


@pytest.mark.parametrize("invocacao", [
    ["repo", "delete", "x"], ["repo", "create", "x"], ["repo", "edit", "x"],
    ["repo", "archive", "x"], ["repo", "rename", "x"],
    ["pr", "create"], ["pr", "merge", "1"], ["pr", "close", "1"],
    ["pr", "review", "1", "--approve"], ["pr", "comment", "1"],
    ["api", "--method", "POST", "repos/x"], ["api", "-X", "DELETE", "repos/x"],
    ["api", "-f", "a=b", "repos/x"], ["api", "-F", "a=b", "repos/x"],
    ["api", "--input", "-", "repos/x"],
    ["release", "create"], ["workflow", "run"], ["secret", "set"],
])
def test_cli_recusa_invocacao_que_grava(invocacao):
    """`repo delete` atravessou um allowlist por verbo em 06/09/2026. Nunca mais."""
    ok, motivo = cli_e_leitura(invocacao)
    assert not ok, f"'{' '.join(invocacao)}' passou pelo portao"
    assert motivo


@pytest.mark.parametrize("invocacao", [
    ["repo", "list", "org"], ["repo", "view", "x"], ["pr", "view", "1"],
    ["api", "repos/x/y"], ["api", "--method", "GET", "repos/x"], ["auth", "status"],
])
def test_cli_permite_leitura(invocacao):
    ok, motivo = cli_e_leitura(invocacao)
    assert ok, f"'{' '.join(invocacao)}' recusado: {motivo}"


@pytest.mark.parametrize("invocacao", [
    ["rev-parse", "HEAD"], ["remote", "get-url", "origin"],
    ["symbolic-ref", "--short", "HEAD"], ["for-each-ref", "refs/heads"],
    ["show", "HEAD:README.md"], ["config", "--get", "x"],
])
def test_git_permite_leitura(invocacao):
    ok, motivo = git_e_leitura(invocacao)
    assert ok, f"'git {' '.join(invocacao)}' recusado: {motivo}"


# ---------------------------------------------------------------------------
# Dados ausentes, malformados e indisponibilidade
# ---------------------------------------------------------------------------

def test_raiz_inexistente_e_erro_nao_lista_vazia(tmp_path):
    p = GitLocal(raiz=tmp_path / "nao-existe")
    with pytest.raises(AdapterErro):
        p.list_repositories()


def test_diretorio_sem_git_e_ignorado_sem_quebrar(provedor, clones):
    (clones / "nao-e-repo").mkdir()
    (clones / "nao-e-repo" / "arquivo.txt").write_text("x", encoding="utf-8")
    assert len(provedor.list_repositories()) == 3


def test_timeout_vira_erro_de_adapter(provedor, clones, monkeypatch):
    """Testa a TRADUCAO do timeout, nao a corrida.

    Um teste que confia em `timeout=0` disparar de fato depende de o processo
    ser mais lento que a granularidade do relogio -- e portanto falha de vez em
    quando, na maquina errada, sem ninguem entender por que.
    """
    def estoura(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=0.1)
    monkeypatch.setattr(subprocess, "run", estoura)
    with pytest.raises(AdapterErro, match="estourou"):
        provedor._git(clones / "api", "rev-parse", "HEAD")


def test_arquivo_inexistente_e_erro_claro(provedor):
    with pytest.raises(AdapterErro):
        provedor.read_file("acme/servico-api", "nao/existe.txt")


def test_le_arquivo_da_base_por_padrao(provedor):
    assert provedor.read_file("acme/servico-api", "README.md").strip() == "# api"


@pytest.mark.parametrize("url,esperado", [
    ("https://github.com/acme/repo.git", "acme/repo"),
    ("git@github.com:acme/repo.git", "acme/repo"),
    ("https://gitlab.com/grupo/sub/repo", "sub/repo"),
    ("ssh://git@bitbucket.org/time/repo.git", "time/repo"),
    ("/caminho/local/repo", "local/repo"),
])
def test_identidade_sai_de_qualquer_forma_de_url(url, esperado):
    """`git@host:org/repo.git` nao e uma URL valida e escapa de todo parser."""
    assert _org_repo(url) == esperado


# ---------------------------------------------------------------------------
# O outro provedor -- mesmo contrato, rede de verdade
# ---------------------------------------------------------------------------

@REDE
def test_hospedagem_remota_cumpre_o_mesmo_contrato():
    p = GitHubRepos(org=os.environ.get("REGENTE_TESTE_ORG", "silverguard-br"),
                    caminho_cli=os.environ.get("REGENTE_GH", "gh"))
    p.verifica()
    repos = p.list_repositories()
    assert repos
    assert all(r.ref.key and "/" in r.ref.key for r in repos)
    assert all(r.ref.provider == "github" for r in repos)
    assert len({r.ref.key for r in repos}) == len(repos)

    um = p.get_repository(repos[0].ref.key)
    assert um.ref == repos[0].ref
    assert not um.parcial and repos[0].parcial


@REDE
def test_hospedagem_remota_recusa_escrita():
    p = GitHubRepos(org="qualquer", caminho_cli=os.environ.get("REGENTE_GH", "gh"))
    with pytest.raises(SomenteLeitura):
        p._cli(["repo", "delete", "qualquer/coisa"])
