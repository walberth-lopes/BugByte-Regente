# -*- coding: utf-8 -*-
"""A worker killed between mutating the remote and recording that it did.

The window this proves is not hypothetical and cannot be reached from inside one
process: the push lands, the row that would record it is never written, and the
worker stops existing. Afterwards the local state and the remote disagree, and
the disagreement is silent -- the local side simply says nothing happened.

    push lands on the remote  ->  [KILLED]  ->  row never written

Assuming the push succeeded loses work. Assuming it failed pushes again. The
engine does neither: it asks the remote. This harness exists to make a second
process face that question for real, with a real corpse behind it.

    python tests/delivery_kill.py push  <root>     # dies after pushing
    python tests/delivery_kill.py resume <root>    # picks up the wreckage

The "remote" is a JSON file. It is not pretending to be GitHub -- it is a place
where a mutation from a process that no longer exists survives, which is the one
property the proof needs.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from regente.core.model import Workspace                       # noqa: E402
from regente.core.policy import AutonomyLevel, PolicyEngine    # noqa: E402
from regente.core.states import TaskState                      # noqa: E402
from regente.engine.pipeline import DeliveryStage              # noqa: E402
from regente.engine.remote import RemoteDelivery, RemoteIdentity  # noqa: E402
from regente.engine.store_sqlite import SqliteStore            # noqa: E402
from regente.ports.agent import Verdict                        # noqa: E402
from regente.ports.repository import PullRequest, RepoRef, build_marker  # noqa: E402
from regente.ports.workspace import WorkArea                   # noqa: E402

WORKSPACE = "wks_kill"
TASK_KEY = "KILL-1"
RUN_ID = "run_kill"
SHA = "c" * 40
BRANCH = "regente/kill-1"
REPO = RepoRef(provider="github", key="acme/worker")
RULES = [{"name": "deliver", "effect": "ALLOW",
          "match": {"action": ["repo.push*", "repo.pr.create"]}}]


# ---------------------------------------------------------------------------
# The remote, as a file that outlives a process
# ---------------------------------------------------------------------------

class FileRemote:
    """Branch heads and pull requests, on disk, shared between processes."""

    def __init__(self, path: Path, sentinel: Path | None = None):
        self.path, self.sentinel = path, sentinel
        if not path.exists():
            self._write({"branch_sha": None, "pulls": [], "pushes": 0,
                         "creations": 0})

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, state: dict) -> None:
        self.path.write_text(json.dumps(state), encoding="utf-8")

    # ---- the read that makes a push idempotent --------------------------
    def remote_branch_sha(self, repo: str, branch: str) -> str | None:
        return self._read().get("branch_sha")

    def find_pull_request_for_branch(self, repo: str, branch: str):
        pulls = self._read()["pulls"]
        if not pulls:
            return None
        p = pulls[-1]
        return PullRequest(number=p["number"], repo=REPO, title="t",
                           url=p["url"], state="OPEN", head_sha=p["head"],
                           branch=branch, base="main", author="regente",
                           data={"body": p["body"]})

    def create_pull_request(self, repo, branch, base, title, body, marker):
        state = self._read()
        number = len(state["pulls"]) + 1
        state["pulls"].append({
            "number": number, "head": state["branch_sha"] or SHA,
            "body": f"{body}\n{marker}",
            "url": f"https://example.invalid/pull/{number}"})
        state["creations"] += 1
        self._write(state)
        return self.find_pull_request_for_branch(repo, branch)


class Areas:
    """A work area whose push writes to the file remote -- and then dies."""

    def __init__(self, remote: FileRemote, die_after_push: bool = False):
        self.remote, self.die_after_push = remote, die_after_push

    def head(self, area):
        return SHA

    def push_target(self, area):
        return "https://example.invalid/acme/worker.git"

    def push(self, area, expected_sha, branch=None):
        state = self.remote._read()
        state["branch_sha"] = expected_sha
        state["pushes"] += 1
        self.remote._write(state)

        if self.die_after_push:
            # The remote has changed and nothing local knows it. The sentinel
            # tells the parent this is the moment; then this process waits to
            # be killed from outside. It must never reach the code that would
            # record the push -- that is the whole point.
            if self.remote.sentinel:
                self.remote.sentinel.write_text("pushed", encoding="utf-8")
            while True:
                time.sleep(0.05)
        return expected_sha


# ---------------------------------------------------------------------------

def build(root: Path, die: bool) -> tuple[SqliteStore, DeliveryStage]:
    store = SqliteStore(root / "regente.db")
    store.migrate()
    remote = FileRemote(root / "remote.json", root / "pushed.flag")
    delivery = RemoteDelivery(
        store=store, areas=Areas(remote, die_after_push=die),
        repos_write=remote, cicd=None,
        policy=PolicyEngine.from_config(RULES), autonomy=AutonomyLevel.L2)
    return store, DeliveryStage(store=store, delivery=delivery,
                                workspace_id=WORKSPACE)


def seed(root: Path) -> str:
    """A workspace and a task sitting at TESTING, ready to be delivered."""
    from regente.core import ids
    from regente.core.model import ExternalRef, Task

    store = SqliteStore(root / "regente.db")
    store.migrate()
    store.save_workspace(Workspace(id=WORKSPACE, client_id="c", name="ws",
                                   max_autonomy=AutonomyLevel.L3))
    task = Task(id=ids.new_id(ids.TASK), workspace_id=WORKSPACE,
                project_id="prj", title="deliver me", state=TaskState.READY,
                externo=ExternalRef(provider="filesystem", key=TASK_KEY))
    store.save_task(task)
    for step in (TaskState.ASSIGNED, TaskState.IMPLEMENTING, TaskState.TESTING):
        store.transition(task.id, step, actor="seed", reason="setup",
                         workspace_id=WORKSPACE)
    store.close()
    return task.id


def identity() -> RemoteIdentity:
    return RemoteIdentity(
        workspace_id=WORKSPACE, workspace_name="ws", organization="org",
        client="c", task_key=TASK_KEY, run_id=RUN_ID, repo=REPO,
        branch=BRANCH, commit_sha=SHA)


def area() -> WorkArea:
    return WorkArea(id=TASK_KEY, path=str(Path.cwd()), branch=BRANCH,
                    repo=REPO.key)


def main() -> int:
    role, root = sys.argv[1], Path(sys.argv[2])
    task_id = (root / "task.id").read_text(encoding="utf-8").strip()
    store, stage = build(root, die=(role == "push"))
    try:
        outcome = stage.advance(identity(), area(), Verdict.READY_FOR_REVIEW,
                                task_id, title=f"{TASK_KEY}: deliver me")
        print(json.dumps({
            "step": outcome.step.value, "reason": outcome.reason,
            "pull_request": outcome.pull_request,
            "resumed": list(outcome.resumed),
            "task_state": outcome.task_state.value if outcome.task_state else None,
        }), flush=True)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
