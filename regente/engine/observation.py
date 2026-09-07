# -*- coding: utf-8 -*-
"""What the engine found in the workspace, independent of what the agent said.

This module is the answer to a single question: *how does the engine know
anything happened?* Before it existed, the loop read `outcome.claimed_files` and
believed it. That is a verdict resting on the subject's self-report, and it fails
in the direction that costs most -- an agent that reports three edited files and
edited none produces a green mission and an empty branch.

So nothing here asks the agent. It reads the filesystem and git:

    diff -> changed files -> cleanliness -> HEAD -> branch ownership
         -> escape detection -> forbidden artifacts

The agent's claims still arrive, and they are used for exactly one thing:
comparison. A mismatch between claim and observation is itself evidence, and it
is recorded as a `Discrepancy` rather than resolved. The engine does not need to
decide whether the agent lied or was confused; it needs to not be fooled either
way.

**Escape detection is not a diff.** A file written outside the work area leaves no
trace in `git status`, because it is not in the repository. It is caught by
comparing a fingerprint of the neighbourhood taken before the agent ran against
one taken after -- which is why `Sentinel` exists and why it must be created
before dispatch, not after.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..ports.agent import Mission, Outcome

#: Paths whose modification means the agent reached for authority rather than
#: for the code. Matched against what `git status` reports, relative to the
#: work area.
#:
#: **`.git/` is deliberately NOT in this list, and that is the interesting part.**
#: An earlier version named `.git/config`, `.git/hooks` and `.git/credentials`
#: here, which read like the strongest entries and were in fact unreachable:
#: `git status` never reports anything inside `.git/`, because it is not part of
#: the working tree. Verified rather than assumed -- rewrite `.git/config`, add a
#: hook, and `git status --untracked-files=all` prints nothing at all.
#:
#: A mutation test found it: emptying this list of its git entries broke no test,
#: because the tests were passing on the sentinel's fingerprint the whole time.
#: Dead code shaped like a guard is worse than no code -- the next reader deletes
#: the sentinel believing this covers it.
#:
#: So `.git/` is covered by `Sentinel.guarded`, which compares fingerprints and
#: can see what a diff cannot. What stays here is what a diff CAN see.
#:
#: The equivalent files for a CI provider are a property of the repository, not
#: knowledge the engine may hold, so they arrive as an argument. The boundary
#: test caught the first version of this constant naming three CI vendors, and it
#: was right to.
AUTHORITY_PATHS: tuple[str, ...] = (
    ".gitmodules",
)

#: Directories skipped when fingerprinting AND when listing changed files.
#:
#: Not cosmetic. The engine runs the test suite itself, and running it writes
#: `__pycache__` into the work area -- so without this filter the engine's own
#: verification appears in the next observation as work the agent did, and the
#: baseline run's artifacts get attributed to an agent that had not started yet.
#: Found by running against a real repository; a fixture would never have shown
#: it.
#:
#: The trade is stated rather than hidden: a deliberate edit inside one of these
#: directories is invisible here. That is acceptable because they hold build
#: output rather than source, and because an escape OUT of the work area -- the
#: case that matters -- is caught by the sentinel, which walks a different tree.
NOISE = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
         ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "target",
         ".gradle", ".next", "coverage", ".idea", ".vscode"}


@dataclass(frozen=True, slots=True)
class ObservedFile:
    path: str
    status: str        # 'modified' | 'added' | 'deleted' | 'renamed'
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """A gap between what the agent claimed and what the engine found.

    Not an accusation. An agent can honestly believe it wrote a file that an
    editor tool silently refused. The engine records the gap and lets the
    verdict rest on the observation.
    """
    kind: str          # 'claimed_not_found' | 'found_not_claimed' | 'count'
    detail: str


@dataclass(frozen=True, slots=True)
class Violation:
    """An attempt to reach outside the authority the mission granted.

    Recorded whether or not it succeeded. A failed escape attempt is a fact
    about the agent worth keeping: the sandbox may hold today and be
    misconfigured tomorrow, and the timeline is what makes that visible.
    """
    kind: str          # 'escape' | 'authority_path' | 'source_mutated' | 'foreign_area'
    detail: str
    path: str = ""

    @property
    def condemns(self) -> bool:
        """Every violation condemns. There is no benign category here."""
        return True


@dataclass(frozen=True, slots=True)
class Observation:
    """The engine's own account of what happened in the work area."""
    files: tuple[ObservedFile, ...] = ()
    head: str = ""
    branch: str = ""
    dirty: bool = False
    violations: tuple[Violation, ...] = ()
    discrepancies: tuple[Discrepancy, ...] = ()
    #: Set when the engine could not look. Never confused with "nothing changed":
    #: one is an absence of change, the other an absence of knowledge.
    unavailable: str = ""

    @property
    def changed_anything(self) -> bool:
        return bool(self.files)

    @property
    def changed_lines(self) -> int:
        return sum(f.additions + f.deletions for f in self.files)

    @property
    def safe(self) -> bool:
        return not self.violations

    @property
    def knows(self) -> bool:
        return not self.unavailable

    def paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files)


# ---------------------------------------------------------------------------
# Sentinel: the fingerprint that makes an escape visible
# ---------------------------------------------------------------------------

def is_noise(relative: str) -> bool:
    """Is this path build output rather than work?"""
    parts = relative.replace("\\", "/").split("/")
    return bool(NOISE & set(parts)) or relative.endswith((".pyc", ".pyo"))


def _fingerprint(root: Path, limit: int = 4000,
                 skip_noise: bool = True) -> dict[str, tuple[int, int]]:
    """Cheap per-file marks: (size, mtime_ns). Not a hash of the content.

    Hashing every neighbouring file would cost more than the mission. Size and
    mtime catch a write; they can miss a same-size same-timestamp rewrite, which
    is a deliberate trade and is stated in `describe()` rather than hidden.
    """
    marks: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return marks
    for current, dirs, files in os.walk(root):
        if skip_noise:
            dirs[:] = [d for d in dirs if d not in NOISE]
        for name in files:
            p = Path(current) / name
            try:
                st = p.stat()
            except OSError:
                continue
            marks[str(p)] = (st.st_size, st.st_mtime_ns)
            if len(marks) >= limit:
                return marks
    return marks


@dataclass(slots=True)
class Sentinel:
    """Watches the neighbourhood the agent is NOT allowed to touch.

    Created before the agent runs. Compares afterwards. It watches the sibling
    work areas and the source clones -- the places an escape would land -- and
    deliberately not the whole filesystem, which would cost more than it proves.

    A sentinel taken after the agent ran proves nothing at all, so `capture()`
    stamps the moment and `compare()` refuses to answer without it.
    """
    watched: tuple[Path, ...] = ()
    #: Trees INSIDE the work area that must not change. Separate because
    #: `git status` cannot see them: `.git/` is not part of the working tree, so
    #: rewriting `.git/config` to point at another remote -- which converts a
    #: refused push into an allowed one -- produces no diff at all. A
    #: fingerprint is the only thing that sees it.
    guarded: tuple[Path, ...] = ()
    _before: dict[str, tuple[int, int]] = field(default_factory=dict)
    _guarded_before: dict[str, tuple[int, int]] = field(default_factory=dict)
    _captured: bool = False

    def capture(self) -> Sentinel:
        self._before = {}
        for root in self.watched:
            self._before.update(_fingerprint(root))
        self._guarded_before = {}
        for root in self.guarded:
            self._guarded_before.update(_fingerprint(root, skip_noise=False))
        self._captured = True
        return self

    def compare(self) -> tuple[Violation, ...]:
        if not self._captured:
            raise RuntimeError(
                "the sentinel was never captured; comparing now would report "
                "'nothing moved' about a world nobody looked at")
        after: dict[str, tuple[int, int]] = {}
        for root in self.watched:
            after.update(_fingerprint(root))

        found: list[Violation] = []
        guarded_after: dict[str, tuple[int, int]] = {}
        for root in self.guarded:
            guarded_after.update(_fingerprint(root, skip_noise=False))
        for path, mark in guarded_after.items():
            if self._guarded_before.get(path) != mark:
                found.append(Violation(
                    "authority_path",
                    "a file that decides where code is pushed or what runs on "
                    "commit was written", path))
        for path in self._guarded_before:
            if path not in guarded_after:
                found.append(Violation(
                    "authority_path", "a repository control file was deleted",
                    path))
        for path, mark in after.items():
            if path not in self._before:
                found.append(Violation("escape", "a file appeared outside the "
                                       "work area", path))
            elif self._before[path] != mark:
                found.append(Violation("escape", "a file outside the work area "
                                       "was modified", path))
        for path in self._before:
            if path not in after:
                found.append(Violation("escape", "a file outside the work area "
                                       "was deleted", path))
        return tuple(found[:50])


# ---------------------------------------------------------------------------
# Observing the work area itself
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: str, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, f"{type(e).__name__}: {e}"
    return p.returncode, (p.stdout or "")


def _parse_numstat(text: str) -> dict[str, tuple[int, int]]:
    counts: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        counts[path.strip()] = (
            int(added) if added.isdigit() else 0,
            int(removed) if removed.isdigit() else 0)
    return counts


_STATUS = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed",
           "C": "copied", "?": "added", "U": "conflicted"}


def observe(path: str, expected_branch: str = "",
            sentinel: Sentinel | None = None,
            authority_paths: tuple[str, ...] = ()) -> Observation:
    """Look at the work area. Ask nobody.

    Uses `git status --porcelain` rather than `git diff` so that untracked files
    count: an agent that creates a new module has changed the repository, and a
    diff against HEAD would not see it. That distinction was worth a whole class
    of missed work.
    """
    root = Path(path)
    if not root.is_dir():
        return Observation(unavailable=f"the work area '{path}' does not exist")

    rc, status = _git(["status", "--porcelain=v1", "--untracked-files=all"], path)
    if rc != 0:
        return Observation(unavailable=f"git status failed: {status.strip()[:200]}")

    rc_head, head = _git(["rev-parse", "HEAD"], path)
    rc_branch, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], path)
    _, numstat = _git(["diff", "--numstat", "HEAD"], path)
    counts = _parse_numstat(numstat)

    files: list[ObservedFile] = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        code, name = line[:2].strip(), line[3:].strip()
        # A rename prints "old -> new"; the new path is the one that exists.
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        name = name.strip('"')
        if is_noise(name):
            continue
        added, removed = counts.get(name, (0, 0))
        if not added and not removed and code in ("??", "A"):
            added = _count_lines(root / name)
        files.append(ObservedFile(
            path=name, status=_STATUS.get(code[:1], "modified"),
            additions=added, deletions=removed))

    violations: list[Violation] = []
    for f in files:
        for guarded in AUTHORITY_PATHS + authority_paths:
            if f.path == guarded or f.path.startswith(guarded.rstrip("/") + "/"):
                violations.append(Violation(
                    "authority_path",
                    f"'{f.path}' controls where code is pushed or what runs on "
                    f"the server; changing it is reaching for authority, not "
                    f"doing the task", f.path))
    if sentinel is not None:
        violations.extend(sentinel.compare())

    if expected_branch and rc_branch == 0:
        actual = branch.strip()
        if actual and actual != expected_branch:
            violations.append(Violation(
                "foreign_area",
                f"the area is on '{actual}' and this run owns "
                f"'{expected_branch}'"))

    return Observation(
        files=tuple(files),
        head=head.strip() if rc_head == 0 else "",
        branch=branch.strip() if rc_branch == 0 else "",
        dirty=bool(files),
        violations=tuple(violations))


def _count_lines(path: Path) -> int:
    try:
        if not path.is_file() or path.stat().st_size > 2_000_000:
            return 0
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Comparing claim against observation
# ---------------------------------------------------------------------------

def reconcile(observation: Observation, outcome: Outcome) -> Observation:
    """Attach the gaps between what was claimed and what was found.

    Returns a new observation. The file list is NOT adjusted to match the
    claims -- that is the whole point. The claims never edit the record; they
    are only measured against it.
    """
    if not observation.knows:
        return observation

    observed = set(observation.paths())
    claimed = {f.path.replace("\\", "/").lstrip("./") for f in outcome.claimed_files}
    normalised = {p.replace("\\", "/").lstrip("./") for p in observed}

    gaps: list[Discrepancy] = []
    for path in sorted(claimed - normalised):
        gaps.append(Discrepancy(
            "claimed_not_found",
            f"the agent reported changing '{path}' and the work area shows no "
            f"such change"))
    for path in sorted(normalised - claimed):
        gaps.append(Discrepancy(
            "found_not_claimed",
            f"'{path}' changed and the agent did not report it"))

    if outcome.claims_a_change and not observation.changed_anything:
        gaps.append(Discrepancy(
            "count",
            f"the agent reported {len(outcome.claimed_files)} changed file(s) "
            f"and the work area is unchanged"))

    return Observation(
        files=observation.files, head=observation.head, branch=observation.branch,
        dirty=observation.dirty, violations=observation.violations,
        discrepancies=tuple(gaps[:50]), unavailable=observation.unavailable)


def sentinel_for(mission: Mission, area_root: str | Path,
                 sources: tuple[str, ...] = (),
                 authority_paths: tuple[str, ...] = ()) -> Sentinel:
    """Watch every sibling work area and every source clone, never the area itself.

    The area root holds all workspaces' areas; watching the whole root would
    flag the agent's own legitimate edits as escapes. So the agent's own
    directory is excluded and everything beside it is watched -- which is
    exactly the boundary that matters.
    """
    root = Path(area_root)
    own = Path(mission.allowed_root).resolve() if mission.allowed_root else None
    watched: list[Path] = []
    if root.is_dir():
        for child in root.iterdir():
            if not child.is_dir():
                continue
            if own is not None and child.resolve() == own:
                continue
            watched.append(child)
    watched.extend(Path(s) for s in sources if Path(s).is_dir())

    # The area's own git metadata is guarded rather than watched: it lives
    # inside the area the agent may edit, and it is exactly the lever that turns
    # an editor into a push.
    guarded: list[Path] = []
    if own is not None:
        for name in (".git", *authority_paths):
            candidate = own / name
            if candidate.exists():
                guarded.append(candidate)
    return Sentinel(watched=tuple(watched), guarded=tuple(guarded)).capture()
