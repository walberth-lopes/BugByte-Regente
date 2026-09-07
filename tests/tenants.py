# -*- coding: utf-8 -*-
"""Two complete client contexts, one engine, deliberately identical names.

The weak version of a tenancy test gives each client different names and proves
`A != B`. That proves the names differ. This gives both clients the SAME local
identifiers -- same task key, same repository name, same resource, same branch,
even the same workspace name -- and proves they remain different entities.

    Organization
    |-- Client-A / Workspace-A: board A, repo "worker", resource "database",
    |                           policy A (push DENIED), small budget, SECRET_A
    `-- Client-B / Workspace-B: board B, repo "worker", resource "database",
                                policy B (push ALLOWED), large budget, SECRET_B

Both share one SQLite file, because separate databases would prove nothing: the
interesting question is whether tenancy holds when the rows sit side by side.

If global identity is ever derived from a local name -- a repository key, a task
key, a resource -- these two collide, and every test here fails at once.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from soak import Clock

from regente.adapters.notify.console import Console
from regente.adapters.runner.scripted import ScriptedAgent
from regente.adapters.secrets import ScopedSecrets
from regente.adapters.tasks.filesystem import FilesystemTasks
from regente.adapters.workspace.local import IsolatedDirectory
from regente.core import ids
from regente.core.model import Workspace
from regente.core.policy import AutonomyLevel, PolicyEngine
from regente.core.risk import RiskEngine
from regente.core.scheduling import Limits
from regente.engine.gate import Gate
from regente.engine.orchestrator import Orchestrator
from regente.engine.store_sqlite import SqliteStore

#: The identifiers both clients use. Every one of them is shared on purpose.
SHARED_TASK = "TASK-1"
SHARED_REPO = "worker"
SHARED_RESOURCE = "repo:database"
SHARED_BRANCH = "feature/test"
SHARED_WORKSPACE_NAME = "main"

ORGANIZATION = "acme"


def stable_id(prefix: str, *parts: str) -> str:
    """The same derivation the composition root uses.

    Imported rather than reimplemented would be better, and it lives in
    `app/container.py` behind an underscore; copying the call here would risk
    the two drifting, so the test imports it.
    """
    from regente.app.container import _stable_id
    return _stable_id(prefix, *parts)


@dataclass
class Tenant:
    """One client's complete world, sharing a database with the other."""
    client: str
    root: Path
    database: Path
    policy_rules: list[dict]
    max_dispatches: int
    secret_reference: str
    secret_value: str
    clock: Clock = field(default_factory=Clock)
    lease_seconds: int = 120
    store: SqliteStore | None = None
    orchestrator: Orchestrator | None = None
    workspace: Workspace | None = None

    @property
    def workspace_id(self) -> str:
        return stable_id(ids.WORKSPACE, ORGANIZATION, self.client,
                         SHARED_WORKSPACE_NAME)

    @property
    def client_id(self) -> str:
        return stable_id(ids.CLIENT, ORGANIZATION, self.client)

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def areas(self) -> Path:
        return self.root / "areas"

    # ------------------------------------------------------------------
    def write_task(self, key: str = SHARED_TASK,
                   resources: tuple[str, ...] = (SHARED_RESOURCE,),
                   status: str = "TO DO") -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        body = [f"key: {key}", f"title: {self.client} work {key}",
                f"status: {status}", "resources:"]
        body += [f"  - {r}" for r in resources]
        (self.tasks_dir / f"{key}.yaml").write_text("\n".join(body) + "\n",
                                                    encoding="utf-8")

    def open(self) -> Tenant:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.areas.mkdir(parents=True, exist_ok=True)

        # One database, two tenants. Separate files would prove nothing.
        self.store = SqliteStore(self.database, clock=self.clock)
        self.store.migrate()
        self.workspace = Workspace(
            id=self.workspace_id, client_id=self.client_id,
            name=SHARED_WORKSPACE_NAME, max_autonomy=AutonomyLevel.L3,
            root=str(self.root))
        self.store.save_workspace(self.workspace)

        risk = RiskEngine()
        self.orchestrator = Orchestrator(
            store=self.store, workspace=self.workspace,
            tasks_provider=FilesystemTasks(self.tasks_dir),
            area_provider=IsolatedDirectory(self.areas),
            runner=ScriptedAgent(default_value={
                "status": "FINISHED", "claim": "COMPLETE",
                "summary": f"done for {self.client}"}),
            gate=Gate(store=self.store,
                      policy=PolicyEngine.from_config(self.policy_rules),
                      risk=risk),
            risk=risk, clock=self.clock,
            limits=Limits(max_workers=2,
                          max_dispatches_per_day=self.max_dispatches),
            notificador=Console(journal=self.root / "journal.log"),
            lease_seconds=self.lease_seconds)
        return self

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
        self.store = None
        self.orchestrator = None

    def crash(self) -> None:
        """Drop everything in memory without closing. What a kill leaves."""
        self.store = None
        self.orchestrator = None
        self.open()

    # ------------------------------------------------------------------
    def secrets(self) -> ScopedSecrets:
        """This tenant's secret resolver, scoped to its own references."""
        return ScopedSecrets(allowed_from=frozenset({self.secret_reference}),
                             workspace=SHARED_WORKSPACE_NAME)

    def policy(self) -> PolicyEngine:
        return PolicyEngine.from_config(self.policy_rules)

    def tick(self, n: int = 1):
        return self.orchestrator.tick()

    def tasks(self):
        return self.store.tasks(self.workspace_id)

    def leases(self):
        return self.store.leases(self.workspace_id)

    def approvals(self):
        return self.store.open_approvals(self.workspace_id)

    def dispatches_today(self) -> int:
        return self.store.dispatch_count(self.workspace_id,
                                         self.clock().strftime("%Y-%m-%d"))


#: Client A: push is refused, and it may dispatch twice a day.
POLICY_A = [
    {"name": "work", "effect": "ALLOW",
     "match": {"action": ["workspace.write", "agent.run", "repo.commit"]}},
    {"name": "no-push", "effect": "DENY", "match": {"action": "repo.push*"}},
]

#: Client B: push is allowed, and it may dispatch far more.
POLICY_B = [
    {"name": "work", "effect": "ALLOW",
     "match": {"action": ["workspace.write", "agent.run", "repo.commit",
                          "repo.push", "repo.push.force"]}},
]


def two_tenants(root: Path, clock_a: Clock | None = None,
                clock_b: Clock | None = None) -> tuple[Tenant, Tenant]:
    """Client-A and Client-B, one database, identical local names."""
    database = root / "regente.db"
    a = Tenant(client="client-a", root=root / "a", database=database,
               policy_rules=POLICY_A, max_dispatches=2,
               secret_reference="env:SECRET_A",
               secret_value="value-belonging-only-to-a",
               clock=clock_a or Clock())
    b = Tenant(client="client-b", root=root / "b", database=database,
               policy_rules=POLICY_B, max_dispatches=50,
               secret_reference="env:SECRET_B",
               secret_value="value-belonging-only-to-b",
               clock=clock_b or Clock())
    return a, b
