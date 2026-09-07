# -*- coding: utf-8 -*-
"""The Context Engine: choosing what the agent sees, and recording why.

Not prompt concatenation. The difference is the word *why*: every item carries
the reason it earned its place, and every deliberate omission is written down
too. That turns the context into something reviewable -- when an agent does the
wrong thing, the first question is what it was told, and the second is what it
was not told. A package that cannot answer both is a black box.

Two failure modes sit on either side, and the expensive one is not the obvious
one. The obvious one is starving the agent. The expensive one is **feeding it the
whole repository**: slower, costlier and less accurate at once, and it hides the
behaviour we want to measure. If the engine pre-chews the analysis, a successful
run proves the engine can find files, not that the agent can do the work.

So the rule for inclusion is:

    include what is cheap, certain, and hard for the agent to discover on its own

The task text, the evidence that chose the target, the repository's own
instructions, the shape of the test suite. Names of things, not the things
themselves: naming twenty test files costs almost nothing, pasting twenty test
files costs everything.
"""

from __future__ import annotations

from pathlib import Path

from ..ports.agent import ContextItem, ContextPackage
from ..ports.repository import RepoInfo
from ..ports.tasks import ExternalTask
from .discovery import Discovery

#: Directory names that never help and always cost.
NOISE = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
         ".pytest_cache", ".mypy_cache", "vendor", ".idea", ".vscode", ".tox"}

#: How many existing test files to name. A list, not the content.
MAX_TEST_FILES = 20

#: How much of an instruction file to inline. Orientation, not documentation.
INSTRUCTION_BUDGET = 2000


def acceptance_criteria(description: str) -> tuple[str, ...]:
    """Pull acceptance criteria out of the description, when they are marked.

    Deliberately literal: it recognises list markers and nothing else. A parser
    that inferred criteria from prose would invent requirements, and an invented
    acceptance criterion is worse than none -- the agent would satisfy something
    nobody asked for and report it as done.
    """
    found: list[str] = []
    for raw in description.splitlines():
        line = raw.strip()
        if not line:
            continue
        marker = line[:2]
        if marker in ("- ", "* ") or (line[:1].isdigit() and line[1:3] in (". ", ") ")):
            text = line.lstrip("-*0123456789.) ").strip()
            if len(text) > 8:
                found.append(text)
    return tuple(found[:12])


def test_files(root: str | Path, limit: int = MAX_TEST_FILES) -> tuple[str, ...]:
    base = Path(root)
    if not base.is_dir():
        return ()
    found: list[str] = []
    for path in sorted(base.rglob("*")):
        if len(found) >= limit:
            break
        if not path.is_file():
            continue
        if NOISE & set(path.relative_to(base).parts):
            continue
        name = path.name
        if name.startswith("test_") or name.endswith(("_test.py", "Test.php",
                                                      ".test.ts", ".test.js",
                                                      "_test.go", "_spec.rb")):
            found.append(path.relative_to(base).as_posix())
    return tuple(found)


def instructions(root: str | Path,
                 candidates: tuple[str, ...] = ()) -> tuple[str, str]:
    """Whatever the repository already says about itself. First hit wins.

    Reading a file the team wrote beats any description the engine could
    assemble, and it costs one open.

    The filenames arrive as an argument: several of them are one coding agent's
    convention, and the engine holding a coding agent's filename is the same
    coupling as the engine holding a CI provider's. Composition supplies them
    from `adapters/conventions.py`.
    """
    base = Path(root)
    for name in candidates:
        candidate = base / name
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace")
            return name, text[:INSTRUCTION_BUDGET]
    return "", ""


def build(
    task: ExternalTask,
    repo: RepoInfo,
    discovery: Discovery,
    workspace_root: str | Path,
    constraints: tuple[str, ...] = (),
    validation_commands: tuple[tuple[str, ...], ...] = (),
    instruction_files: tuple[str, ...] = (),
) -> ContextPackage:
    """Assemble the package. Every item justified, every omission named."""
    items: list[ContextItem] = []
    excluded: list[tuple[str, str]] = []

    items.append(ContextItem(
        kind="task", ref=task.key,
        reason="the work itself; without it there is no mission",
        content=f"{task.title}\n\n{task.description}".strip()[:4000],
        source="task provider"))

    for e in discovery.evidence:
        items.append(ContextItem(
            kind="evidence", ref=repo.ref.key,
            reason=f"this is why the engine believes the work belongs in "
                   f"{repo.ref.key}; the agent may disagree with it",
            content=e.detail[:400], source=e.source))

    for rejected in discovery.alternatives_considered:
        excluded.append((
            rejected.repo,
            f"considered as the target and rejected: {rejected.reason}"))

    name, text = instructions(workspace_root, instruction_files)
    if name:
        items.append(ContextItem(
            kind="instructions", ref=name,
            reason="the team's own account of this repository beats anything "
                   "the engine could assemble about it",
            content=text, source="repository"))
    else:
        excluded.append((
            "repository instructions",
            f"none of {', '.join(instruction_files) or '(none configured)'} "
            f"exists here"))

    tests = test_files(workspace_root)
    for path in tests:
        items.append(ContextItem(
            kind="test", ref=path,
            reason="an existing test near the work: it shows the conventions "
                   "and is the cheapest place to add coverage",
            source="work area"))
    if not tests:
        excluded.append(("tests", "no test file was found in the work area"))
    elif len(tests) == MAX_TEST_FILES:
        excluded.append(("further tests",
                         f"only the first {MAX_TEST_FILES} are named; the agent "
                         f"can list the rest itself"))

    for command in validation_commands:
        items.append(ContextItem(
            kind="validation", ref=" ".join(command),
            reason="the engine will run exactly this afterwards; the agent is "
                   "told so it can aim at the same target",
            source="engine"))

    for c in constraints:
        items.append(ContextItem(
            kind="constraint", ref=c[:60],
            reason="a boundary of this mission, enforced outside the model",
            content=c, source="engine"))

    for link in task.links:
        items.append(ContextItem(
            kind="related", ref=link.key,
            reason=("a blocking dependency of this task" if link.blocking
                    else "a related task; context, not a dependency"),
            source="task provider"))

    # Stated rather than silently true. "The agent never saw the file" is the
    # most common explanation for a wrong change, and an omission nobody wrote
    # down is an omission nobody can review.
    excluded.append((
        "repository source files",
        "not inlined by design: the agent has read access and finding the "
        "right file is the work being measured, not a cost to be avoided"))

    return ContextPackage(
        goal=task.title,
        items=tuple(items),
        excluded=tuple(excluded),
        acceptance_criteria=acceptance_criteria(task.description))
