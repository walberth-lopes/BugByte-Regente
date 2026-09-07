# -*- coding: utf-8 -*-
"""The RepositoryProvider contract. Holds for EVERY adapter, present and future.

It runs against two providers deliberately of the most different kind possible: a
local process over real git repositories, and a CLI against the remote hosting.
If the contract holds for both, it is not written around either one.

The local provider runs over REAL git repositories created on the spot -- there
is no simulation of git anywhere. The remote one requires a network and a
credential, so it runs under a marker: `pytest -m rede`. The default suite stays
fast and offline, and the contract stays the same code in both cases.

"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from regente.adapters.repos.git_local import GitLocal, _org_repo
from regente.adapters.repos.github import GitHubRepos
from regente.adapters.repos.readonly import cli_is_read, git_is_read
from regente.ports import AdapterError, ReadOnlyRefused
from regente.ports.repository import RepoCapability, RepoRef, RepositoryProvider


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, encoding="utf-8", errors="replace")


def _make_repo(root: Path, diretorio: str, remoto: str | None,
               base: str = "main", branches: tuple[str, ...] = ()) -> Path:
    """Creates a REAL git repository. Nothing here is simulated."""
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
        # `origin/HEAD` without a fetch: the adapter has to find the base anyway,
        # and this is the state of a freshly configured clone.
        _git(p, "update-ref", f"refs/remotes/origin/{base}", "HEAD")
        _git(p, "symbolic-ref", "refs/remotes/origin/HEAD",
             f"refs/remotes/origin/{base}")
    return p


@pytest.fixture
def clones(tmp_path) -> Path:
    root = tmp_path / "clones"
    root.mkdir()
    _make_repo(root, "api", "https://github.com/acme/servico-api.git",
               branches=("feat/K-1-coisa", "fix/K-2-outra"))
    _make_repo(root, "web", "git@github.com:acme/web.git", base="master")
    _make_repo(root, "no-remote", None)
    return root


@pytest.fixture
def provider(clones) -> RepositoryProvider:
    return GitLocal(root=clones)


NETWORK = pytest.mark.skipif(
    os.environ.get("REGENTE_TESTE_REDE") != "1",
    reason="needs a network and a credential; enable with REGENTE_TESTE_REDE=1")


# ---------------------------------------------------------------------------
# Identity -- the core of the contract
# ---------------------------------------------------------------------------

def test_declares_the_that_is(provider):
    d = provider.describe()
    assert d["capability"] == "repository"
    assert d["adapter"] and d["adapter"] != "unknown"


def test_list_returns_repositorios(provider):
    repos = provider.list_repositories()
    assert repos
    assert all(r.ref.key for r in repos)


def test_identity_comes_from_the_remote_not_the_directory(provider):
    """The defect this test prevents was measured on the real disk: the directory
    `scamchecker-legado` points at the repository `scamchecker`.

    """
    por_dir = {r.data["directory"]: r for r in provider.list_repositories()}
    assert por_dir["api"].ref.key == "acme/servico-api"
    assert por_dir["api"].data.get("directory_differs_from_repo") is True
    assert por_dir["web"].ref.key == "acme/web"


def test_identity_is_unique(provider):
    keys = [r.ref.key for r in provider.list_repositories()]
    assert len(keys) == len(set(keys))


def test_identity_carries_the_provider(provider):
    for r in provider.list_repositories():
        assert r.ref.provider == provider.name
        assert str(r.ref) == f"{r.ref.provider}:{r.ref.key}"


def test_resource_is_scoped_by_workspace(provider):
    """A repository of the same name in two clients must give DIFFERENT resources."""
    r = provider.list_repositories()[0]
    a = r.ref.resource("wks_a")
    b = r.ref.resource("wks_b")
    assert a != b
    assert "wks_a" in a and "wks_b" in b


def test_without_a_remote_it_still_has_an_identity(provider):
    """The absence of a remote must not become the absence of a repository."""
    keys = {r.ref.key for r in provider.list_repositories()}
    assert any(k.startswith("local/") for k in keys)


def test_get_returns_the_same_that_the_list(provider):
    from_list = provider.list_repositories()[0]
    um = provider.get_repository(from_list.ref.key)
    assert um.ref == from_list.ref
    assert um.base_branch == from_list.base_branch


def test_missing_repository_raises(provider):
    with pytest.raises(AdapterError):
        provider.get_repository("does/not-exist-999")


# ---------------------------------------------------------------------------
# Base branch -- assuming 'main' costs the whole job
# ---------------------------------------------------------------------------

def test_branch_base_is_read_never_assumed(provider):
    por_dir = {r.data["directory"]: r for r in provider.list_repositories()}
    assert por_dir["api"].base_branch == "main"
    assert por_dir["web"].base_branch == "master", "presumiu 'main'"


def test_branch_current_not_is_confused_with_the_base(provider, clones):
    """Of the 12 real clones examined, 11 were on a work branch."""
    _git(clones / "api", "checkout", "-q", "feat/K-1-coisa")
    r = provider.get_repository("acme/servico-api")
    assert r.data["current_branch"] == "feat/K-1-coisa"
    assert r.base_branch == "main"


def test_list_branches_without_duplicating_local_is_remote(provider):
    names = [b.name for b in provider.list_branches("acme/servico-api")]
    assert len(names) == len(set(names))
    assert "feat/K-1-coisa" in names
    assert sum(1 for b in provider.list_branches("acme/servico-api") if b.is_base) == 1


def test_filter_of_branch(provider):
    found = provider.list_branches("acme/servico-api", {"pattern": "K-1"})
    assert [b.name for b in found] == ["feat/K-1-coisa"]


# ---------------------------------------------------------------------------
# Capabilities: power and permission are different things
# ---------------------------------------------------------------------------

def test_declares_capabilities(provider):
    for r in provider.list_repositories():
        assert r.capabilities
        assert r.can(RepoCapability.READ_METADATA)


def test_declares_no_write_capability_in_this_milestone(provider):
    from regente.ports.repository import WRITE_CAPS
    assert not (provider.capabilities & WRITE_CAPS), (
        "a read adapter declaring write power")


def test_missing_capability_is_declared_not_simulated(provider):
    """Plain git has no pull requests -- and the adapter has to SAY so."""
    assert not provider.list_repositories()[0].can(RepoCapability.READ_PULL_REQUESTS)


# ---------------------------------------------------------------------------
# Shadow mode: per INVOCATION, never per verb
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
    """The contract exists; the implementation does not. Both are visible."""
    with pytest.raises(NotImplementedError):
        getattr(provider, operation)(*args)


@pytest.mark.parametrize("invocation", [
    ["commit", "-m", "x"], ["push"], ["remote", "set-url", "origin", "x"],
    ["remote", "add", "x", "y"], ["branch", "-D", "x"],
    ["symbolic-ref", "HEAD", "refs/heads/x"], ["config", "user.email", "x"],
    ["checkout", "x"], ["reset", "--hard"], ["clean", "-fd"],
])
def test_git_refuses_invocation_that_writes(provider, clones, invocation):
    """Each of these starts with a verb that has a read form -- which is why an
    allowlist by verb would let them through.

    """
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
    """`repo delete` crossed a verb-level allowlist on 06/09/2026. Never again."""
    ok, reason = cli_is_read(invocation)
    assert not ok, f"'{' '.join(invocation)}' got through the gate"
    assert reason


@pytest.mark.parametrize("invocation", [
    ["repo", "list", "org"], ["repo", "view", "x"], ["pr", "view", "1"],
    ["api", "repos/x/y"], ["api", "--method", "GET", "repos/x"], ["auth", "status"],
])
def test_cli_allows_read(invocation):
    ok, reason = cli_is_read(invocation)
    assert ok, f"'{' '.join(invocation)}' recusado: {reason}"


@pytest.mark.parametrize("invocation", [
    ["rev-parse", "HEAD"], ["remote", "get-url", "origin"],
    ["symbolic-ref", "--short", "HEAD"], ["for-each-ref", "refs/heads"],
    ["show", "HEAD:README.md"], ["config", "--get", "x"],
])
def test_git_allows_read(invocation):
    ok, reason = git_is_read(invocation)
    assert ok, f"'git {' '.join(invocation)}' recusado: {reason}"


# ---------------------------------------------------------------------------
# Missing data, malformed data and unavailability
# ---------------------------------------------------------------------------

def test_root_missing_is_error_not_list_empty(tmp_path):
    p = GitLocal(root=tmp_path / "does-not-exist")
    with pytest.raises(AdapterError):
        p.list_repositories()


def test_directory_without_git_is_ignored_without_breaking(provider, clones):
    (clones / "not-a-repo").mkdir()
    (clones / "not-a-repo" / "arquivo.txt").write_text("x", encoding="utf-8")
    assert len(provider.list_repositories()) == 3


def test_timeout_becomes_error_of_adapter(provider, clones, monkeypatch):
    """Tests the TRANSLATION of the timeout, not the race.

    A test that relies on `timeout=0` actually firing depends on the process being
    slower than the clock's granularity -- and therefore fails every so often, on
    the wrong machine, with nobody understanding why.

    """
    def blow_up(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=0.1)
    monkeypatch.setattr(subprocess, "run", blow_up)
    with pytest.raises(AdapterError, match="timed out"):
        provider._git(clones / "api", "rev-parse", "HEAD")


def test_file_missing_is_error_clear(provider):
    with pytest.raises(AdapterError):
        provider.read_file("acme/servico-api", "does/not-exist.txt")


def test_reads_a_file_from_the_base_by_default(provider):
    assert provider.read_file("acme/servico-api", "README.md").strip() == "# api"


@pytest.mark.parametrize("url,esperado", [
    ("https://github.com/acme/repo.git", "acme/repo"),
    ("git@github.com:acme/repo.git", "acme/repo"),
    ("https://gitlab.com/grupo/sub/repo", "sub/repo"),
    ("ssh://git@bitbucket.org/time/repo.git", "time/repo"),
    ("/caminho/local/repo", "local/repo"),
])
def test_identity_survives_any_url_form(url, esperado):
    """`git@host:org/repo.git` is not a valid URL and escapes every parser."""
    assert _org_repo(url) == esperado


# ---------------------------------------------------------------------------
# The other provider -- same contract, a real network
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
