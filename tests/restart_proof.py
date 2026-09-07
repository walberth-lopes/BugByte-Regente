# -*- coding: utf-8 -*-
"""The completion criterion, demonstrated by execution rather than argued.

    a process dies -> the state persists -> a new process starts
    -> it finds what was left behind -> it recovers or escalates
    -> it keeps operating

Repeated, with real processes and a real `SIGKILL`. The in-process soak drops
its objects and rebuilds, which proves that objects can be dropped; this proves
that the ROWS are enough. A second interpreter, sharing nothing with the first
but a file on disk, has to work out what happened.

Run it:

    python tests/restart_proof.py --rounds 6

It prints a ledger and exits non-zero if anything was lost.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

#: The child. It ticks forever against a shared database, printing one line per
#: tick so the parent knows it is alive and can kill it mid-flight.
CHILD = '''
import json, sys, time
sys.path.insert(0, {here!r})
sys.path.insert(0, {root!r})
from datetime import datetime, timedelta, timezone
from pathlib import Path
from soak import Soak, Clock

start = datetime.fromisoformat({start!r})
soak = Soak(root=Path({root_dir!r}), clock=Clock(at=start), lease_seconds=120)
soak.open()
for i in range(1, {tasks} + 1):
    soak.write_task(f"RP-{{i}}", resources=(f"repo:r{{i % 2}}",))

n = 0
while True:
    n += 1
    m = soak.tick(n)
    soak.decide_everything()
    # Four hours a tick, so rounds cross day boundaries and the run shows
    # the engine still WORKING after each kill -- not merely surviving.
    soak.clock.advance(seconds=14400)
    print(json.dumps({{"tick": n, "at": soak.clock().isoformat(),
                       "dispatched": m.dispatched, "recovered": m.recovered,
                       "health": m.health}}), flush=True)
    time.sleep(0.05)
'''


def one_round(root: Path, start_iso: str, tasks: int,
              ticks_before_kill: int) -> dict:
    """Start a child, let it work, kill it without warning, report what it did."""
    program = CHILD.format(here=str(HERE), root=str(ROOT), root_dir=str(root),
                           start=start_iso, tasks=tasks)
    script = root.parent / "child.py"
    script.write_text(program, encoding="utf-8")

    child = subprocess.Popen([sys.executable, str(script)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             encoding="utf-8", errors="replace")
    seen: list[dict] = []
    try:
        while len(seen) < ticks_before_kill:
            line = child.stdout.readline()
            if not line:
                break
            line = line.strip()
            if line.startswith("{"):
                seen.append(json.loads(line))
    finally:
        # SIGKILL. No cleanup, no flush, no goodbye, no chance to release a
        # lease or close the database. Exactly what a machine losing power does.
        child.kill()
        child.wait(timeout=30)

    last = seen[-1] if seen else {}
    return {"ticks": len(seen), "last_at": last.get("at", start_iso),
            "dispatched": sum(s["dispatched"] for s in seen),
            "recovered": sum(s["recovered"] for s in seen),
            "exit": child.returncode}


def inspect(root: Path) -> dict:
    """What a fresh process can tell from the disk alone."""
    from regente.engine import health as health_module
    from regente.engine.store_sqlite import SqliteStore
    from soak import check_invariants
    from datetime import datetime

    store = SqliteStore(root / "regente.db")
    store.migrate()
    try:
        at = datetime.now().astimezone()
        rows = store.tasks("wks_soak")
        latest = max((t.updated_at for t in rows), default=None)
        when = latest + __import__("datetime").timedelta(seconds=1) if latest else at
        report = health_module.inspect(store, "wks_soak", "restart-proof",
                                       areas_root=root / "areas",
                                       max_dispatches=8, when=when)
        return {
            "tasks": len(rows),
            "runs_running": len(store.active_runs("wks_soak")),
            "leases": len(store.leases("wks_soak")),
            "events": store.table_counts("wks_soak").get("events", 0),
            "health": report.level.value,
            "violations": check_invariants(store, "wks_soak", when,
                                           root / "areas"),
        }
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="kill and restart, repeatedly")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--tasks", type=int, default=4)
    parser.add_argument("--ticks-before-kill", type=int, default=4)
    parser.add_argument("--root", default="")
    args = parser.parse_args()

    root = Path(args.root) if args.root else HERE / ".restart"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)

    print(f"{'round':>5} {'ticks':>6} {'disp':>5} {'recov':>6} {'tasks':>6} "
          f"{'runs':>5} {'leases':>7} {'events':>7} {'health':>9} violations")
    start = "2026-03-01T09:00:00+00:00"
    lost: list[str] = []
    keys_before: set[str] | None = None

    for round_number in range(1, args.rounds + 1):
        ran = one_round(root, start, args.tasks, args.ticks_before_kill)
        start = ran["last_at"]
        state = inspect(root)

        print(f"{round_number:5} {ran['ticks']:6} {ran['dispatched']:5} "
              f"{ran['recovered']:6} {state['tasks']:6} {state['runs_running']:5} "
              f"{state['leases']:7} {state['events']:7} {state['health']:>9} "
              f"{len(state['violations'])}")

        for problem in state["violations"]:
            lost.append(f"round {round_number}: {problem}")

        keys = {t for t in range(state["tasks"])}
        if keys_before is not None and state["tasks"] < len(keys_before):
            lost.append(f"round {round_number}: tasks disappeared "
                        f"({len(keys_before)} -> {state['tasks']})")
        keys_before = keys

    print()
    if lost:
        print("LOST OR BROKEN")
        for problem in lost:
            print("  -", problem)
        return 1
    print(f"{args.rounds} kill/restart rounds, nothing lost, no invariant broken")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
