# -*- coding: utf-8 -*-
"""The NEEDS ME queue.

The entry criterion is deliberately narrow. An item only appears here when the
answer **does not exist inside the system**:

- the policy requires human authority (a merge into production, for example);
- the work contract is ambiguous and no extra reading resolves it;
- the recovery ladder ran out;
- the backlog blocks itself (a dependency cycle).

These do not belong here: high risk (that buys a second pass, not waiting), a red
test (that is work), a transient error (that is a retry). Filling this queue with
what the engine could resolve itself is the one guaranteed way to make the owner
stop reading it.

The card carries a decision, not a diagnosis. Logs stay in the event, on demand.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core import ids
from ..core.model import Approval, Option, Task
from ..core.risk import RiskLevel

#: Default options. Every escalation offers at least: follow the recommendation,
#: ask for more investigation, or stop. "Stop" has to be available always --
#: without it, the owner's only way out would be to edit the database by hand.
#:
FOLLOW = Option("follow", "Approve the recommendation", "the engine runs the recommended path")
INVESTIGATE = Option("investigate", "Ask for more investigation", "sends the task back to analysis")
BLOCK = Option("block", "Block the task", "leaves the work queue until somebody unblocks it")
CANCEL = Option("cancel", "Cancel the task", "closes the work out")


@dataclass(frozen=True, slots=True)
class Briefing:
    """The Approval's projection to the surface. No internal vocabulary."""
    id: str
    key: str
    title: str
    what_happened: str
    why_it_matters: str
    what_was_tried: tuple[str, ...]
    options: tuple[dict[str, str], ...]
    recommendation: str | None
    risk: str


def build(
    task: Task,
    what_happened: str,
    why_it_matters: str,
    attempts: tuple[str, ...] = (),
    options: tuple[Option, ...] = (),
    recommendation: str | None = FOLLOW.id,
    risk: RiskLevel = RiskLevel.MEDIUM,
    run_id: str | None = None,
) -> Approval:
    """Creates the queue item. `why_it_matters` is mandatory and cannot be empty.

    A card describing what happened without saying why it matters hands the owner
    the job of working out whether it deserves attention -- which is exactly the
    job the queue was supposed to have spared them.
    """
    if not why_it_matters.strip():
        raise ValueError("an escalation without 'why it matters' does not enter the queue")
    return Approval(
        id=ids.new_id(ids.APPROVAL),
        workspace_id=task.workspace_id,
        task_id=task.id,
        run_id=run_id,
        what_happened=what_happened,
        why_it_matters=why_it_matters,
        what_was_tried=attempts,
        options=options or (FOLLOW, INVESTIGATE, BLOCK, CANCEL),
        recommendation=recommendation,
        risk=risk,
    )


def briefing(a: Approval, task: Task) -> Briefing:
    return Briefing(
        id=a.id,
        key=task.key,
        title=task.title,
        what_happened=a.what_happened,
        why_it_matters=a.why_it_matters,
        what_was_tried=a.what_was_tried,
        options=tuple({"id": o.id, "label": o.label, "effect": o.effect} for o in a.options),
        recommendation=a.recommendation,
        risk=a.risk.name,
    )


def render(b: Briefing) -> str:
    """Terminal rendering. The same content the UI shows."""
    lines = [
        f"{b.key}  [{b.risk}]",
        f"  {b.title}",
        "",
        f"  WHAT HAPPENED     {b.what_happened}",
        f"  WHY IT MATTERS    {b.why_it_matters}",
    ]
    if b.what_was_tried:
        lines.append("  WHAT I TRIED      " + b.what_was_tried[0])
        lines += ["                    " + t for t in b.what_was_tried[1:]]
    lines.append("")
    for o in b.options:
        mark = "->" if o["id"] == b.recommendation else "  "
        lines.append(f"  {mark} [{o['id']}] {o['label']}")
    return "\n".join(lines)
