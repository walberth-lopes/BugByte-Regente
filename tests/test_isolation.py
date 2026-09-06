# -*- coding: utf-8 -*-
"""Isolamento entre clientes. A fronteira que nao pode vazar nunca.

Um vazamento aqui nao aparece como error: aparece como o motor do cliente A
trabalhando com dado do cliente B, em silencio, ate o dia em que sai num log ou
num PR. Por isso cada propriedade e testada explicitamente, e nao presumida da
existencia da coluna `workspace_id`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from regente.adapters.notify.console import Console
from regente.adapters.runner.scripted import ScriptedRunner
from regente.adapters.secrets import ScopedSecrets, SecretMissing, SecretOutOfScope
from regente.adapters.tasks.filesystem import FilesystemTasks
from regente.adapters.tasks.jira import JiraTasks
from regente.adapters.tasks.transport import SnapshotTransport
from regente.adapters.workspace.local import IsolatedDirectory
from regente.core.model import Workspace
from regente.core.policy import AutonomyLevel, PolicyEngine
from regente.core.risk import RiskEngine
from regente.core.scheduling import Limits
from regente.engine.gate import Gate
from regente.engine.orchestrator import Orchestrator
from regente.engine.store_sqlite import SqliteStore

SNAPSHOTS = Path(__file__).parent / "snapshots" / "jira"


def _yaml_tasks(folder: Path, keys: list[str]) -> FilesystemTasks:
    folder.mkdir(parents=True, exist_ok=True)
    for k in keys:
        (folder / f"{k}.yaml").write_text(
            yaml.safe_dump({"key": k, "title": f"work {k}", "status": "TO DO",
                            "resources": [f"repo:{k}"]},
                           allow_unicode=True, sort_keys=False), encoding="utf-8")
    return FilesystemTasks(folder)


def _engine(store: SqliteStore, ws_id: str, name: str, provider, tmp_path: Path):
    ws = Workspace(id=ws_id, client_id=f"cli_{name}", name=name,
                   max_autonomy=AutonomyLevel.L0)
    store.save_workspace(ws)
    risk = RiskEngine()
    return Orchestrator(
        store=store, workspace=ws, tasks_provider=provider,
        area_provider=IsolatedDirectory(tmp_path / f"areas-{name}"),
        runner=ScriptedRunner(),
        gate=Gate(store=store, policy=PolicyEngine.from_config([]), risk=risk),
        risk=risk, limits=Limits(max_workers=2),
        notificador=Console(journal=tmp_path / f"{name}.log"))


# ---------------------------------------------------------------------------
# Tenancy no estado
# ---------------------------------------------------------------------------

def test_two_clientes_in_same_database_not_if_see(tmp_path):
    """O caso barato de errar: um banco compartilhado e uma consulta sem escopo."""
    store = SqliteStore(tmp_path / "compartilhado.db")
    store.migrate()

    a = _engine(store, "wks_a", "clienteA", _yaml_tasks(tmp_path / "a", ["A-1", "A-2"]), tmp_path)
    b = _engine(store, "wks_b", "clienteB", _yaml_tasks(tmp_path / "b", ["B-1"]), tmp_path)
    a.tick(); a.tick()
    b.tick(); b.tick()

    chaves_a = {t.key for t in store.tasks("wks_a")}
    chaves_b = {t.key for t in store.tasks("wks_b")}
    assert chaves_a == {"A-1", "A-2"}
    assert chaves_b == {"B-1"}
    assert not (chaves_a & chaves_b)


def test_same_key_externa_in_two_clientes_sao_tasks_distinct(tmp_path):
    """SG-1 do cliente A e SG-1 do cliente B nao podem colidir no indice."""
    store = SqliteStore(tmp_path / "c.db")
    store.migrate()
    a = _engine(store, "wks_a", "A", _yaml_tasks(tmp_path / "a", ["SG-1"]), tmp_path)
    b = _engine(store, "wks_b", "B", _yaml_tasks(tmp_path / "b", ["SG-1"]), tmp_path)
    a.tick(); b.tick()

    ta = store.task_by_key("wks_a", "filesystem", "SG-1")
    tb = store.task_by_key("wks_b", "filesystem", "SG-1")
    assert ta and tb
    assert ta.id != tb.id, "a mesma chave externa virou uma task so"


def test_events_is_actions_sao_scoped(tmp_path):
    store = SqliteStore(tmp_path / "c.db")
    store.migrate()
    a = _engine(store, "wks_a", "A", _yaml_tasks(tmp_path / "a", ["A-1"]), tmp_path)
    b = _engine(store, "wks_b", "B", _yaml_tasks(tmp_path / "b", ["B-1"]), tmp_path)
    a.tick(); a.tick()
    b.tick()

    de_a = store.events("wks_a", limit=200)
    ids_de_b = {t.id for t in store.tasks("wks_b")}
    assert de_a, "o cliente A produziu eventos"
    assert not any(e.task_id in ids_de_b for e in de_a)


def test_plan_of_a_client_ignores_work_of_other(tmp_path):
    store = SqliteStore(tmp_path / "c.db")
    store.migrate()
    a = _engine(store, "wks_a", "A", _yaml_tasks(tmp_path / "a", ["A-1", "A-2"]), tmp_path)
    b = _engine(store, "wks_b", "B", _yaml_tasks(tmp_path / "b", ["B-1", "B-2", "B-3"]), tmp_path)
    a.tick(); b.tick()
    a._analyze(type("R", (), {"analyzed": 0})())
    b._analyze(type("R", (), {"analyzed": 0})())

    chaves = {store.task(i).key for i in a.plan().dispatch}
    assert chaves <= {"A-1", "A-2"}


def test_lease_of_a_client_not_blocks_the_other(tmp_path):
    """Recurso homonimo em dois clientes sao dois recursos DIFERENTES.

    Dois clientes podem ter um repositorio de mesmo nome -- e no board real isso
    ja acontece com `scamchecker`, cujo diretorio local sequer bate com o nome
    remoto. Com a trava por chave nua, um cliente atrasaria o outro: conservador
    o bastante para nunca corromper nada, e errado o bastante para ninguem
    descobrir por que o motor do cliente B fica parado.
    """
    store = SqliteStore(tmp_path / "c.db")
    store.migrate()
    store.save_workspace(Workspace(id="wks_a", client_id="a", name="A"))
    store.save_workspace(Workspace(id="wks_b", client_id="b", name="B"))

    assert store.acquire_lease("repo:api", "run_a", "wks_a", 60) is not None
    assert store.acquire_lease("repo:api", "run_b", "wks_b", 60) is not None, (
        "clientes diferentes competindo pela mesma trava")
    # E dentro do MESMO cliente a exclusao continua valendo.
    assert store.acquire_lease("repo:api", "run_a2", "wks_a", 60) is None


def test_releasing_lease_of_a_client_not_releases_the_of_other(tmp_path):
    store = SqliteStore(tmp_path / "c.db")
    store.migrate()
    store.save_workspace(Workspace(id="wks_a", client_id="a", name="A"))
    store.save_workspace(Workspace(id="wks_b", client_id="b", name="B"))
    store.acquire_lease("repo:api", "run_x", "wks_a", 60)
    store.acquire_lease("repo:api", "run_x", "wks_b", 60)

    store.release_lease("repo:api", "run_x", workspace_id="wks_a")
    assert store.acquire_lease("repo:api", "outro", "wks_a", 60) is not None
    assert store.acquire_lease("repo:api", "outro", "wks_b", 60) is None, (
        "soltar a trava de um cliente soltou a do outro")


def test_lease_expired_is_listed_only_to_the_owner_of_scope(tmp_path):
    store = SqliteStore(tmp_path / "c.db")
    store.migrate()
    store.save_workspace(Workspace(id="wks_a", client_id="a", name="A"))
    store.save_workspace(Workspace(id="wks_b", client_id="b", name="B"))
    store.acquire_lease("repo:api", "run_a", "wks_a", 60)
    store.acquire_lease("repo:api", "run_b", "wks_b", 60)
    store._con.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00.000000Z'")

    assert [l.owner for l in store.expired_leases("wks_a")] == ["run_a"]
    assert [l.owner for l in store.expired_leases("wks_b")] == ["run_b"]


# ---------------------------------------------------------------------------
# Tenancy nos segredos
# ---------------------------------------------------------------------------

def test_workspace_not_reaches_secret_that_not_declared():
    """A fronteira mais cara de furar: credencial de um cliente noutro adapter."""
    a = ScopedSecrets(allowed_from=frozenset({"env:A_TOKEN"}), workspace="A")
    with pytest.raises(SecretOutOfScope):
        a.resolve("env:B_TOKEN")


def test_reference_declared_resolves(monkeypatch):
    monkeypatch.setenv("A_TOKEN", "valor-de-teste")
    a = ScopedSecrets(allowed_from=frozenset({"env:A_TOKEN"}), workspace="A")
    assert a.resolve("env:A_TOKEN") == "valor-de-teste"


def test_reference_declared_but_missing_is_error_clear():
    a = ScopedSecrets(allowed_from=frozenset({"env:NAO_DEFINIDA_XYZ"}), workspace="A")
    os.environ.pop("NAO_DEFINIDA_XYZ", None)
    with pytest.raises(SecretMissing):
        a.resolve("env:NAO_DEFINIDA_XYZ")


def test_not_exists_secret_literal():
    """Se `literal:` existisse, o primeiro token de producao entraria num YAML."""
    a = ScopedSecrets(allowed_from=frozenset({"literal:abc123"}), workspace="A")
    with pytest.raises(SecretMissing, match="scheme"):
        a.resolve("literal:abc123")


def test_workspace_without_secrets_not_reaches_nothing():
    a = ScopedSecrets(allowed_from=frozenset(), workspace="A")
    with pytest.raises(SecretOutOfScope):
        a.resolve("env:QUALQUER")


# ---------------------------------------------------------------------------
# Dois provedores DIFERENTES no mesmo motor
# ---------------------------------------------------------------------------

def test_clientes_with_providers_different_coexist(tmp_path):
    """Cliente A em YAML, cliente B em Jira -- mesmo Core, mesmo banco."""
    store = SqliteStore(tmp_path / "c.db")
    store.migrate()
    a = _engine(store, "wks_a", "A", _yaml_tasks(tmp_path / "a", ["A-1"]), tmp_path)
    b = _engine(store, "wks_b", "B",
               JiraTasks(transport=SnapshotTransport(directory=SNAPSHOTS),
                         site="https://exemplo.atlassian.net"), tmp_path)
    a.tick(); b.tick()

    assert {t.key for t in store.tasks("wks_a")} == {"A-1"}
    do_b = store.tasks("wks_b")
    assert do_b and all(t.key.startswith("SG-") for t in do_b)
    # E a identidade guarda de QUAL provedor cada task veio.
    assert {t.externo.provider for t in store.tasks("wks_a")} == {"filesystem"}
    assert {t.externo.provider for t in do_b} == {"jira"}


# ---------------------------------------------------------------------------
# Identidade de repositorio dentro da tenancy
# ---------------------------------------------------------------------------

def test_repos_same_named_in_clientes_different_sao_resources_different():
    """Dois clientes podem ter um repositorio chamado `api`. Sao dois."""
    from regente.ports.repository import RepoRef
    a = RepoRef(provider="github", key="clienteA/api")
    b = RepoRef(provider="github", key="clienteB/api")

    # Chaves diferentes: recursos diferentes, obviamente.
    assert a.resource("wks_1") != b.resource("wks_1")
    # MESMA chave em workspaces diferentes: tambem recursos diferentes.
    assert a.resource("wks_1") != a.resource("wks_2")
    # E o mesmo repositorio visto por dois provedores nao colide.
    assert (RepoRef(provider="git-local", key="clienteA/api").resource("wks_1")
            != a.resource("wks_1"))


def test_two_clientes_with_repo_of_same_name_not_compete_lock(tmp_path):
    """O cenario completo: identidade -> recurso -> lease, entre clientes."""
    from regente.ports.repository import RepoRef
    store = SqliteStore(tmp_path / "c.db")
    store.migrate()
    store.save_workspace(Workspace(id="wks_a", client_id="a", name="A"))
    store.save_workspace(Workspace(id="wks_b", client_id="b", name="B"))

    # Coincidencia total: mesmo provider, mesma chave, clientes diferentes.
    ref = RepoRef(provider="github", key="acme/api")
    ra, rb = ref.resource("wks_a"), ref.resource("wks_b")

    assert store.acquire_lease(ra, "run_a", "wks_a", 60) is not None
    assert store.acquire_lease(rb, "run_b", "wks_b", 60) is not None, (
        "o cliente B ficou esperando a trava do cliente A")
    # Dentro do mesmo cliente, a exclusao continua valendo.
    assert store.acquire_lease(ra, "run_a2", "wks_a", 60) is None


def test_providers_of_repo_different_coexist(tmp_path):
    """Cliente A le clones locais, cliente B le a hospedagem -- mesmo Core."""
    import subprocess
    from regente.adapters.repos.git_local import GitLocal

    def repo(root, name, remoto):
        p = root / name
        p.mkdir(parents=True)
        for args in (["init", "-q", "-b", "main"],
                     ["config", "user.email", "t@e.invalido"],
                     ["config", "user.name", "T"]):
            subprocess.run(["git", *args], cwd=str(p), check=True, capture_output=True)
        (p / "a.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(p), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=str(p), check=True,
                       capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", remoto], cwd=str(p),
                       check=True, capture_output=True)
        return p

    a = tmp_path / "a"; b = tmp_path / "b"
    repo(a, "api", "https://github.com/clienteA/api.git")
    repo(b, "api", "https://github.com/clienteB/api.git")

    pa, pb = GitLocal(root=a), GitLocal(root=b)
    ka = pa.list_repositories()[0].ref
    kb = pb.list_repositories()[0].ref
    assert ka.key == "clienteA/api" and kb.key == "clienteB/api"
    assert ka.resource("wks_a") != kb.resource("wks_b")
