# -*- coding: utf-8 -*-
"""Isolation between clients. The boundary that must never leak.

A leak here does not show up as an error: it shows up as client A's engine
working with client B's data, silently, until the day it comes out in a log or a
PR. That is why every property is tested explicitly, and not assumed from the
existence of the `workspace_id` column.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from regente.adapters.notify.console import Console
from regente.adapters.runner.scripted import ScriptedAgent
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
        runner=ScriptedAgent(),
        gate=Gate(store=store, policy=PolicyEngine.from_config([]), risk=risk),
        risk=risk, limits=Limits(max_workers=2),
        notifier=Console(journal=tmp_path / f"{name}.log"))


# ---------------------------------------------------------------------------
# Tenancy in the state
# ---------------------------------------------------------------------------

def test_two_clients_in_one_database_cannot_see_each_other(tmp_path):
    """The cheap way to get it wrong: a shared database and an unscoped query."""
    store = SqliteStore(tmp_path / "compartilhado.db")
    store.migrate()

    a = _engine(store, "wks_a", "clienteA", _yaml_tasks(tmp_path / "a", ["A-1", "A-2"]), tmp_path)
    b = _engine(store, "wks_b", "clienteB", _yaml_tasks(tmp_path / "b", ["B-1"]), tmp_path)
    a.tick(); a.tick()
    b.tick(); b.tick()

    keys_a = {t.key for t in store.tasks("wks_a")}
    keys_b = {t.key for t in store.tasks("wks_b")}
    assert keys_a == {"A-1", "A-2"}
    assert keys_b == {"B-1"}
    assert not (keys_a & keys_b)


def test_the_same_external_key_in_two_clients_is_two_tasks(tmp_path):
    """Client A's SG-1 and client B's SG-1 must not collide in the index."""
    store = SqliteStore(tmp_path / "c.db")
    store.migrate()
    a = _engine(store, "wks_a", "A", _yaml_tasks(tmp_path / "a", ["SG-1"]), tmp_path)
    b = _engine(store, "wks_b", "B", _yaml_tasks(tmp_path / "b", ["SG-1"]), tmp_path)
    a.tick(); b.tick()

    ta = store.task_by_key("wks_a", "filesystem", "SG-1")
    tb = store.task_by_key("wks_b", "filesystem", "SG-1")
    assert ta and tb
    assert ta.id != tb.id, "the same external key became a single task"


def test_events_and_actions_are_scoped(tmp_path):
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

    keys = {store.task(i).key for i in a.plan().dispatch}
    assert keys <= {"A-1", "A-2"}


def test_lease_of_a_client_not_blocks_the_other(tmp_path):
    """A resource of the same name in two clients is two DIFFERENT resources.

    Two clients can have a repository of the same name -- and on the real board
    that already happens with `scamchecker`, whose local directory does not even
    match the remote name. With a lock on the bare key, one client would hold up
    the other: conservative enough never to corrupt anything, and wrong enough
    that nobody would work out why client B's engine is stopped.
    """
    store = SqliteStore(tmp_path / "c.db")
    store.migrate()
    store.save_workspace(Workspace(id="wks_a", client_id="a", name="A"))
    store.save_workspace(Workspace(id="wks_b", client_id="b", name="B"))

    assert store.acquire_lease("repo:api", "run_a", "wks_a", 60) is not None
    assert store.acquire_lease("repo:api", "run_b", "wks_b", 60) is not None, (
        "different clients competing for the same lock")
    # And within the SAME client the exclusion still holds.
    assert store.acquire_lease("repo:api", "run_a2", "wks_a", 60) is None


def test_releasing_lease_of_a_client_not_releases_the_of_other(tmp_path):
    store = SqliteStore(tmp_path / "c.db")
    store.migrate()
    store.save_workspace(Workspace(id="wks_a", client_id="a", name="A"))
    store.save_workspace(Workspace(id="wks_b", client_id="b", name="B"))
    store.acquire_lease("repo:api", "run_x", "wks_a", 60)
    store.acquire_lease("repo:api", "run_x", "wks_b", 60)

    store.release_lease("repo:api", "run_x", workspace_id="wks_a")
    assert store.acquire_lease("repo:api", "another", "wks_a", 60) is not None
    assert store.acquire_lease("repo:api", "another", "wks_b", 60) is None, (
        "releasing one client's lock released the other's")


def test_lease_expired_is_listed_only_to_the_owner_of_scope(tmp_path):
    store = SqliteStore(tmp_path / "c.db")
    store.migrate()
    store.save_workspace(Workspace(id="wks_a", client_id="a", name="A"))
    store.save_workspace(Workspace(id="wks_b", client_id="b", name="B"))
    store.acquire_lease("repo:api", "run_a", "wks_a", 60)
    store.acquire_lease("repo:api", "run_b", "wks_b", 60)
    store._conn.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00.000000Z'")

    assert [l.owner for l in store.expired_leases("wks_a")] == ["run_a"]
    assert [l.owner for l in store.expired_leases("wks_b")] == ["run_b"]


# ---------------------------------------------------------------------------
# Tenancy in the secrets
# ---------------------------------------------------------------------------

def test_workspace_not_reaches_secret_that_not_declared():
    """The most expensive boundary to breach: one client's credential in another adapter."""
    a = ScopedSecrets(allowed_from=frozenset({"env:A_TOKEN"}), workspace="A")
    with pytest.raises(SecretOutOfScope):
        a.resolve("env:B_TOKEN")


def test_reference_declared_resolves(monkeypatch):
    monkeypatch.setenv("A_TOKEN", "test-value")
    a = ScopedSecrets(allowed_from=frozenset({"env:A_TOKEN"}), workspace="A")
    assert a.resolve("env:A_TOKEN") == "test-value"


def test_reference_declared_but_missing_is_error_clear():
    a = ScopedSecrets(allowed_from=frozenset({"env:NAO_DEFINIDA_XYZ"}), workspace="A")
    os.environ.pop("NAO_DEFINIDA_XYZ", None)
    with pytest.raises(SecretMissing):
        a.resolve("env:NAO_DEFINIDA_XYZ")


def test_not_exists_secret_literal():
    """If `literal:` existed, the first production token would end up in a YAML."""
    a = ScopedSecrets(allowed_from=frozenset({"literal:abc123"}), workspace="A")
    with pytest.raises(SecretMissing, match="scheme"):
        a.resolve("literal:abc123")


def test_workspace_without_secrets_not_reaches_nothing():
    a = ScopedSecrets(allowed_from=frozenset(), workspace="A")
    with pytest.raises(SecretOutOfScope):
        a.resolve("env:QUALQUER")


# ---------------------------------------------------------------------------
# Two DIFFERENT providers in the same engine
# ---------------------------------------------------------------------------

def test_clients_with_different_providers_coexist(tmp_path):
    """Client A on YAML, client B on Jira -- same Core, same database."""
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
    # And the identity records WHICH provider each task came from.
    assert {t.external.provider for t in store.tasks("wks_a")} == {"filesystem"}
    assert {t.external.provider for t in do_b} == {"jira"}


# ---------------------------------------------------------------------------
# Repository identity within the tenancy
# ---------------------------------------------------------------------------

def test_same_named_repos_in_different_clients_are_different_resources():
    """Two clients can have a repository called `api`. They are two."""
    from regente.ports.repository import RepoRef
    a = RepoRef(provider="github", key="clienteA/api")
    b = RepoRef(provider="github", key="clienteB/api")

    # Different keys: different resources, obviously.
    assert a.resource("wks_1") != b.resource("wks_1")
    # The SAME key in different workspaces: also different resources.
    assert a.resource("wks_1") != a.resource("wks_2")
    # And the same repository seen by two providers does not collide.
    assert (RepoRef(provider="git-local", key="clienteA/api").resource("wks_1")
            != a.resource("wks_1"))


def test_two_clients_with_a_same_named_repo_do_not_contend_for_a_lock(tmp_path):
    """The full scenario: identity -> resource -> lease, across clients."""
    from regente.ports.repository import RepoRef
    store = SqliteStore(tmp_path / "c.db")
    store.migrate()
    store.save_workspace(Workspace(id="wks_a", client_id="a", name="A"))
    store.save_workspace(Workspace(id="wks_b", client_id="b", name="B"))

    # Total coincidence: same provider, same key, different clients.
    ref = RepoRef(provider="github", key="acme/api")
    ra, rb = ref.resource("wks_a"), ref.resource("wks_b")

    assert store.acquire_lease(ra, "run_a", "wks_a", 60) is not None
    assert store.acquire_lease(rb, "run_b", "wks_b", 60) is not None, (
        "client B ended up waiting for client A's lock")
    # Within the same client, the exclusion still holds.
    assert store.acquire_lease(ra, "run_a2", "wks_a", 60) is None


def test_providers_of_repo_different_coexist(tmp_path):
    """Client A reads local clones, client B reads the hosting -- same Core."""
    import subprocess
    from regente.adapters.repos.git_local import GitLocal

    def repo(root, name, remote):
        p = root / name
        p.mkdir(parents=True)
        for args in (["init", "-q", "-b", "main"],
                     ["config", "user.email", "t@e.invalid"],
                     ["config", "user.name", "T"]):
            subprocess.run(["git", *args], cwd=str(p), check=True, capture_output=True)
        (p / "a.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(p), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=str(p), check=True,
                       capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=str(p),
                       check=True, capture_output=True)
        return p

    a = tmp_path / "a"; b = tmp_path / "b"
    repo(a, "api", "https://github.com/clientA/api.git")
    repo(b, "api", "https://github.com/clientB/api.git")

    pa, pb = GitLocal(root=a), GitLocal(root=b)
    ka = pa.list_repositories()[0].ref
    kb = pb.list_repositories()[0].ref
    assert ka.key == "clientA/api" and kb.key == "clientB/api"
    assert ka.resource("wks_a") != kb.resource("wks_b")
