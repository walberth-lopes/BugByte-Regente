# -*- coding: utf-8 -*-
"""Two clients as real processes, interleaved, with kills on either side.

Running A and then B proves sequencing. This runs them at the same time, in
separate interpreters, against one SQLite file, and kills processes on one side
while the other is working -- because the question is not whether the queries
carry a workspace but whether a failure in one tenant can freeze, corrupt or
leak into the other.

The order of events is deliberately nondeterministic. The invariants are not.

    python tests/multitenant.py --seconds 6 --kill a --repeat 5
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))


def run_worker(root: Path, which: str, seconds: float) -> int:
    """Tick one tenant until told to stop. Nothing knows about the other."""
    import random

    from tenants import two_tenants

    a, b = two_tenants(root)
    tenant = a if which == "a" else b
    tenant.open()
    tenant.write_task()
    tenant.write_task("SHARED-2", resources=("repo:database", "repo:extra"))

    deadline = time.monotonic() + seconds
    ticks = 0
    while time.monotonic() < deadline:
        ticks += 1
        try:
            tenant.tick()
        except Exception as e:                       # noqa: BLE001
            print(json.dumps({"tenant": which, "tick_failed":
                              f"{type(e).__name__}: {e}"[:200]}), flush=True)
        for approval in tenant.approvals():
            try:
                tenant.store.decide_approval(
                    approval.id, "investigar", per=f"operator-{which}",
                    workspace_id=tenant.workspace_id)
            except Exception:                        # noqa: BLE001
                pass
        tenant.clock.advance(minutes=7)
        time.sleep(random.uniform(0.01, 0.04))

    print(json.dumps({"tenant": which, "ticks": ticks}), flush=True)
    tenant.close()
    return 0


def inspect(root: Path) -> dict:
    """What the shared database says about both tenants, separately."""
    from tenants import two_tenants
    from soak import check_invariants
    from regente.engine.store_sqlite import SqliteStore

    a, b = two_tenants(root)
    store = SqliteStore(root / "regente.db")
    store.migrate()
    found: dict[str, int] = defaultdict(int)
    broken: list[str] = []
    try:
        at = a.clock()
        for tenant in (a, b):
            tasks = store.tasks(tenant.workspace_id)
            found[f"{tenant.client}.tasks"] = len(tasks)
            found[f"{tenant.client}.runs"] = len(
                store.task_runs(tasks[0].id, tenant.workspace_id)) if tasks else 0
            found[f"{tenant.client}.leases"] = len(store.leases(tenant.workspace_id))
            found[f"{tenant.client}.approvals"] = len(
                store.open_approvals(tenant.workspace_id))
            found[f"{tenant.client}.events"] = len(
                store.events(tenant.workspace_id, limit=100_000))

            # Every row this tenant can see must belong to it.
            for task in tasks:
                if task.workspace_id != tenant.workspace_id:
                    broken.append(f"{tenant.client} sees task of "
                                  f"{task.workspace_id}")
            for lease in store.leases(tenant.workspace_id):
                if lease.workspace_id != tenant.workspace_id:
                    broken.append(f"{tenant.client} sees lease of "
                                  f"{lease.workspace_id}")
            for event in store.events(tenant.workspace_id, limit=100_000):
                if event.workspace_id != tenant.workspace_id:
                    broken.append(f"{tenant.client} sees event of "
                                  f"{event.workspace_id}")

            for problem in check_invariants(store, tenant.workspace_id, at,
                                            tenant.areas):
                broken.append(f"{tenant.client}: {problem}")

        # The same local names must have produced different rows.
        a_tasks = {t.id for t in store.tasks(a.workspace_id)}
        b_tasks = {t.id for t in store.tasks(b.workspace_id)}
        if a_tasks & b_tasks:
            broken.append(f"the two clients share task rows: {a_tasks & b_tasks}")
        found["shared_task_rows"] = len(a_tasks & b_tasks)

        # Both must have worked. A tenancy proof where one side did nothing
        # proves only that nothing leaked from an idle client.
        if not a_tasks or not b_tasks:
            broken.append("one client did no work at all")
    finally:
        store.close()
    return {"counters": dict(found), "violations": broken}


def main() -> int:
    parser = argparse.ArgumentParser(description="two tenants, real processes")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--workers-each", type=int, default=2)
    parser.add_argument("--kill", default="", choices=["", "a", "b", "both"])
    parser.add_argument("--kill-at", type=float, default=1.5)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--root", default="")
    args = parser.parse_args()

    base = Path(args.root) if args.root else HERE / ".tenants"
    totals: dict[str, int] = defaultdict(int)
    violations: list[str] = []

    for round_number in range(1, args.repeat + 1):
        root = base.parent / f"{base.name}-{round_number}"
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)

        children: dict[str, list] = {"a": [], "b": []}
        for which in ("a", "b"):
            for _ in range(args.workers_each):
                children[which].append(subprocess.Popen(
                    [sys.executable, str(Path(__file__)), "worker",
                     "--root", str(root), "--which", which,
                     "--seconds", str(args.seconds)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    encoding="utf-8", errors="replace"))

        if args.kill:
            time.sleep(args.kill_at)
            victims = (["a", "b"] if args.kill == "both" else [args.kill])
            for which in victims:
                children[which][0].kill()
                totals[f"killed.{which}"] += 1

        for group in children.values():
            for child in group:
                try:
                    child.wait(timeout=args.seconds + 60)
                except subprocess.TimeoutExpired:
                    child.kill()

        crashed = [c.stderr.read() for group in children.values()
                   for c in group if c.stderr]
        for text in crashed:
            if text and "Traceback" in text:
                violations.append(f"round {round_number}: a worker crashed:\\n"
                                  + text[-600:])

        found = inspect(root)
        for key, value in found["counters"].items():
            totals[key] += value
        violations += [f"round {round_number}: {v}" for v in found["violations"]]
        print(f"  round {round_number}/{args.repeat}: "
              f"{len(found['violations'])} violation(s)", flush=True)
        shutil.rmtree(root, ignore_errors=True)

    print()
    print("aggregate")
    for key, value in sorted(totals.items()):
        print(f"  {key:<28} {value}")
    for name in ("cross_client_task", "cross_client_secret",
                 "cross_client_lease", "shared_task_rows"):
        print(f"  {name:<28} {totals.get(name, 0)}")

    if violations:
        print()
        print(f"{len(violations)} VIOLATION(S)")
        for v in violations[:20]:
            print("  -", v)
        return 1
    print()
    print("no tenancy boundary crossed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        p = argparse.ArgumentParser()
        p.add_argument("worker")
        p.add_argument("--root", required=True)
        p.add_argument("--which", required=True)
        p.add_argument("--seconds", type=float, default=5.0)
        a = p.parse_args()
        raise SystemExit(run_worker(Path(a.root), a.which, a.seconds))
    raise SystemExit(main())
