# -*- coding: utf-8 -*-
"""Assembling the context handed to an agent: the minimum that suffices.

Two failure modes sit on either side of this module, and the expensive one is
not the obvious one.

The obvious one is starving the agent. The expensive one is **feeding it the
whole project**: it makes the agent slower, costlier and less accurate at the
same time, and it hides the behaviour we are trying to measure. If the
Orchestrator pre-chews the analysis, the run proves that the Orchestrator can
find files -- not that the agent can do the work.

So this gathers what is cheap and certain (the task's own text, the resolved
target, the base branch, how the repository is tested, why we believe the target)
and stops. Anything else the agent finds with its own tools, which is exactly the
capability we want to observe.
"""

from __future__ import annotations

from pathlib import Path

from ..ports.agent import Context
from ..ports.repository import RepoInfo
from ..ports.tasks import ExternalTask
from .discovery import Discovery

#: Directory names that never help and always cost. Skipped when listing tests.
NOISE = {".git", ".venv", "node_modules", "__pycache__", "dist", "build",
         ".pytest_cache", ".mypy_cache", "vendor", ".idea", ".vscode"}

#: How many existing test files to name. A list, not the content: the agent can
#: open what it needs, and naming twenty paths costs almost nothing while
#: pasting twenty files costs everything.
MAX_TEST_FILES = 20


def _acceptance_criteria(description: str) -> tuple[str, ...]:
    """Pull acceptance criteria out of the description, when they are marked.

    Deliberately literal: it recognises common list markers and nothing else. A
    parser that infers criteria from prose would invent requirements, and an
    invented acceptance criterion is worse than none -- the agent would satisfy
    something nobody asked for and report it as done.
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


def existing_tests(root: str | Path, limit: int = MAX_TEST_FILES) -> tuple[str, ...]:
    """Test files already in the working area, by path."""
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
                                                      "_test.go")):
            found.append(path.relative_to(base).as_posix())
    return tuple(found)


def architecture_notes(root: str | Path) -> str:
    """Whatever the repository already says about itself, first hit wins.

    Reading a file the team wrote beats any description the engine could
    assemble, and it costs one open. Truncated hard: this is orientation, not
    the documentation itself.
    """
    base = Path(root)
    for name in ("AGENTS.md", "CLAUDE.md", "ARCHITECTURE.md", "CONTRIBUTING.md",
                 "README.md"):
        candidate = base / name
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace")
            return f"[{name}]\n{text[:2000]}"
    return ""


def build(
    task: ExternalTask,
    repo: RepoInfo,
    discovery: Discovery,
    workspace_root: str | Path,
    constraints: tuple[str, ...] = (),
) -> Context:
    return Context(
        goal=task.title,
        acceptance_criteria=_acceptance_criteria(task.description),
        repository=repo.ref.key,
        base_branch=repo.base_branch,
        # Empty on purpose in this milestone: the engine does not know which
        # files matter, and guessing would hand the agent a wrong lead dressed
        # as a fact. It has read access; let it look.
        relevant_files=(),
        architecture=architecture_notes(workspace_root),
        related_tasks=tuple(v.key for v in task.links),
        dependencies=tuple(v.key for v in task.links if v.blocking),
        existing_tests=existing_tests(workspace_root),
        constraints=constraints,
        target_evidence=tuple(f"{e.source}: {e.detail}" for e in discovery.evidence),
    )
