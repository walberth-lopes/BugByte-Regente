# -*- coding: utf-8 -*-
"""Rendering a mission as text, for agents that read text.

This was extracted from one vendor's adapter, and the extraction was NOT
mechanical -- the function it came from was three different things wearing one
name, and pulling it out whole would have moved a vendor's protocol into shared
code where the next vendor would inherit it silently.

The three parts, and where each belongs:

  **The mission, rendered.** Task, identity, acceptance criteria, repository
  instructions, target evidence, nearby tests, validation commands, constraints,
  withheld authority, what failed last time. Nothing here is vendor-shaped: any
  text-driven coding agent wants exactly this, and wants it worded the same.
  That is this module.

  **How to answer.** "Reply with JSON matching the required schema" reads
  agnostic and is not: it presumes the schema arrives out of band, which is true
  of a tool that takes a schema flag and false of one that does not. So it is a
  hook on the base class, and each profile supplies its own -- one pointing at a
  schema it passed as a flag, another inlining the schema because it has no
  flag. See `CliAgent.answer_instruction`.

  **The invocation.** Flags, sandbox switches, output format. Entirely the
  profile's, and it never lived here.

Why this sits in `adapters/` rather than `engine/`: rendering a mission as prose
is an assumption about the agent's *input format*. An agent that accepted a
structured mission directly would want none of this. The engine owns the
mission; how a particular kind of agent is spoken to is adapter work.
"""

from __future__ import annotations

from ...ports.agent import Mission


def render(mission: Mission, answer_instruction: str = "") -> str:
    """The mission as markdown. `answer_instruction` comes from the profile.

    Everything stated here is also enforced elsewhere: the forbidden actions are
    forbidden because the tools are absent, the working directory is confined by
    the process, the budget is capped outside the model. Saying it too saves the
    agent a wasted turn. It is never the mechanism, and a reader who mistakes it
    for one has found the most dangerous possible misreading of this file.
    """
    ctx = mission.context
    lines = [
        f"# Task {mission.task_key}",
        "",
        mission.goal or "(no title)",
        "",
        "## Where you are",
        f"- repository: {mission.repo.key if mission.repo else '(unknown)'}",
        f"- branch: {mission.branch}",
        f"- working directory: {mission.allowed_root}",
        "  This directory is the whole world. Paths outside it are not yours,",
        "  and the tools you have cannot reach them.",
        "",
    ]

    if ctx:
        if ctx.acceptance_criteria:
            lines.append("## Acceptance criteria")
            lines += [f"- {c}" for c in ctx.acceptance_criteria]
            lines.append("")
        for kind, heading in (("instructions", "Repository instructions"),
                              ("evidence", "Why the engine believes the work "
                                           "belongs in this repository"),
                              ("task", "Task detail")):
            chosen = ctx.of_kind(kind)
            if chosen:
                lines.append(f"## {heading}")
                for item in chosen:
                    lines.append(f"<!-- included because: {item.reason} -->")
                    lines.append(item.content or item.ref)
                lines.append("")
        tests = ctx.of_kind("test")
        if tests:
            lines.append("## Existing tests")
            lines += [f"- {t.ref}" for t in tests]
            lines.append("")

    if mission.validation_commands:
        lines.append("## How your work will be checked")
        lines.append("The engine runs these itself afterwards. Your own claims")
        lines.append("about tests are recorded but decide nothing.")
        lines += [f"- {' '.join(c)}" for c in mission.validation_commands]
        lines.append("")

    if mission.constraints:
        lines.append("## Constraints")
        lines += [f"- {c}" for c in mission.constraints]
        lines.append("")

    if mission.forbidden_actions:
        lines.append("## Not available to you")
        lines.append("These are refused outside this conversation; you have no")
        lines.append("tool that performs them. Listed so you do not spend a turn")
        lines.append("trying:")
        lines += [f"- {a}" for a in mission.forbidden_actions]
        lines.append("")

    if mission.previous_failures:
        lines.append(f"## Attempt {mission.iteration}: what failed last time")
        lines += [f"- {f}" for f in mission.previous_failures]
        lines.append("")

    if answer_instruction:
        lines += ["## Answer with", answer_instruction]

    return "\n".join(lines)


#: The part of the answer instruction that is genuinely shared: what a claim
#: means, and that an honest partial beats an optimistic complete. A profile
#: appends its own account of the format.
HONESTY_NOTE = (
    "`claim` is what you believe about the work; the engine will check the "
    "working directory itself and may reach a different conclusion. Report "
    "honestly -- a claim that does not survive observation is recorded as a "
    "discrepancy, and an accurate `PARTIAL` is worth more than an optimistic "
    "`COMPLETE`."
)
