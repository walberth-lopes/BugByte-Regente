# -*- coding: utf-8 -*-
"""Run tests and classify the result.

**A red test does not mean a bad agent.** Five causes produce the same colour,
and confusing them costs in both directions: punishing the agent for a missing
`pip install`, or approving a regression believing it was the environment.

The only classification that condemns the change is `REGRESSION` -- and to claim
it you must know what passed BEFORE. So the engine measures a baseline before
letting the agent touch anything: without that snapshot, "broke now" is
indistinguishable from "was already broken", and the engine would escalate
inherited debt as if it were a freshly created defect.

The agent does NOT classify its own test. It runs and reports; the engine judges,
with the baseline in hand.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..core import childenv
from ..ports.agent import TestResult

#: Marks that the problem is the ENVIRONMENT, not the code. Deliberately
#: conservative: between environment and regression, the engine only picks
#: environment when the mark is unambiguous -- calling a regression an
#: environment problem would approve broken code.
ENVIRONMENT_MARKS = (
    "modulenotfounderror",
    # `python -m pytest` with pytest absent prints neither of the two forms
    # above: the launcher says "<interpreter>: No module named pytest". Missing
    # it cost a real misclassification -- the run came back
    # PREEXISTING_FAILURE, which reads as "the change is fine, the red is
    # inherited", when in truth nothing had been verified at all. The bare form
    # is matched deliberately: erring toward ENVIRONMENT blocks the mission,
    # and erring the other way approves unverified work.
    "no module named",
    "connection refused", "could not connect", "no such host",
    "address already in use", "authentication failed",
    "database .* does not exist", "could not translate host name",
    "certificate verify failed", "no module named ['\"]psycopg",
    "docker.*not running", "unable to open database file",
)

#: Marks that the runner itself never ran.
INFRASTRUCTURE_MARKS = (
    "command not found", "is not recognized as an internal or external command",
    "no such file or directory", "cannot find the path",
    "error: unrecognized arguments", "no tests ran",
    "file or directory not found", "executable not found",
)


@dataclass(frozen=True, slots=True)
class TestRun:
    """One raw test round. Classifying is a separate step."""
    command: str
    exit_code: int
    output: str
    duration_s: float
    ran: bool = True

    @property
    def green(self) -> bool:
        return self.ran and self.exit_code == 0


@dataclass(frozen=True, slots=True)
class TestVerdict:
    result: TestResult
    reason: str
    #: Tests that were failing BEFORE and still fail.
    preexisting: tuple[str, ...] = ()
    #: Tests that passed before and broke now. The only ones that condemn.
    regressions: tuple[str, ...] = ()
    duration_s: float = 0.0
    command: str = ""

    @property
    def allows_delivery(self) -> bool:
        return self.result in (TestResult.PASSED, TestResult.PREEXISTING_FAILURE)


def _fresh_environment() -> dict[str, str]:
    """Force the verification run to read the files, not a cached compilation.

    Found by running this engine against a real repository, and it failed in the
    worst possible direction. The sequence:

      1. the engine measures the baseline -- the suite compiles `app.py` and
         writes `__pycache__/app.cpython-313.pyc`, stamped with the source's
         modification time;
      2. the agent edits `app.py` a fraction of a second later;
      3. the engine runs the suite again to check the change.

    If both writes land inside the filesystem's timestamp granularity -- routine
    on Windows, and the whole sequence takes well under a second -- the stamp
    matches and Python imports the STALE bytecode. The engine then verifies the
    code as it was BEFORE the agent touched it, sees green, and reports
    `changes exist and the tests are green` about a change that breaks the
    build. It reproduced roughly half the time.

    A verifier that can silently test the previous revision is not a verifier.
    Sending the cache to a fresh directory per run costs nothing and removes the
    class: there is no prior compilation to find.

    The environment is composed rather than inherited, and that is a second,
    unrelated defect this function now closes. The agent is given no tool that
    runs commands -- but it can write files, tests are files, and THIS is where
    the engine runs them. An agent that cannot execute anything could still write
    a test and have the engine execute it holding every credential in the
    engine's environment. Running agent-authored tests is work we want; handing
    them a wallet is not.
    """
    return childenv.compose(
        os.environ,
        childenv.BASE_ALLOWLIST + childenv.TEST_RUNNER_ALLOWLIST,
        extra={"PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPYCACHEPREFIX": tempfile.mkdtemp(prefix="regente-pyc-")})


def run(command: list[str], cwd: str | Path, timeout: int = 900) -> TestRun:
    """Execute the repository's test command. Never raises."""
    started = time.monotonic()
    try:
        p = subprocess.run(command, cwd=str(cwd), capture_output=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env=_fresh_environment())
    except subprocess.TimeoutExpired:
        return TestRun(" ".join(command), -1, f"[timed out after {timeout}s]",
                       time.monotonic() - started)
    except (FileNotFoundError, NotADirectoryError) as e:
        # A missing runner is infrastructure, and `ran=False` stops this from
        # being read as "the tests failed".
        return TestRun(" ".join(command), -1, f"command not found: {e}",
                       time.monotonic() - started, ran=False)
    return TestRun(" ".join(command), p.returncode,
                   ((p.stdout or "") + "\n" + (p.stderr or "")).strip(),
                   time.monotonic() - started)


#: Pull test identifiers out of the output. Covers the most common formats
#: without pretending to be universal: when nothing matches, the engine falls
#: back to exit codes, which is less precise and is declared as such.
_FAILURE_PATTERNS = (
    re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M),           # pytest
    re.compile(r"^\s*\d+\)\s+(\S+::\S+)", re.M),              # phpunit
    re.compile(r"^\s*(?:FAIL|not ok)\s+[\d\s]*(\S+)", re.M),  # go / tap
)


def _failed_ids(output: str) -> set[str]:
    found: set[str] = set()
    for pattern in _FAILURE_PATTERNS:
        found |= {m.strip() for m in pattern.findall(output)}
    return found


def _match_mark(output: str, marks: tuple[str, ...]) -> str:
    low = output.lower()
    for m in marks:
        if re.search(m, low):
            return m
    return ""


def classify(after: TestRun, baseline: TestRun | None = None) -> TestVerdict:
    """`baseline` is the snapshot of the repository BEFORE the change.

    Without a baseline the engine cannot claim a regression -- and does not. A
    red run with no baseline becomes `UNKNOWN`, which is honest and approves
    nothing. Calling it a regression would escalate inherited debt; calling it
    preexisting would approve broken code.
    """
    if not after.ran:
        return TestVerdict(TestResult.INFRASTRUCTURE_FAILURE,
                           f"the test runner never ran: {after.output[:160]}",
                           duration_s=after.duration_s, command=after.command)

    if after.green:
        return TestVerdict(TestResult.PASSED, "all tests passed",
                           duration_s=after.duration_s, command=after.command)

    infra = _match_mark(after.output, INFRASTRUCTURE_MARKS)
    if infra:
        return TestVerdict(TestResult.INFRASTRUCTURE_FAILURE,
                           f"the runner did not really run: '{infra}'",
                           duration_s=after.duration_s, command=after.command)

    environment = _match_mark(after.output, ENVIRONMENT_MARKS)
    if environment:
        return TestVerdict(TestResult.ENVIRONMENT_FAILURE,
                           f"missing environment, not a code defect: '{environment}'",
                           duration_s=after.duration_s, command=after.command)

    if baseline is None:
        return TestVerdict(TestResult.UNKNOWN,
                           "red with no baseline: a regression cannot be claimed",
                           duration_s=after.duration_s, command=after.command)

    if not baseline.ran:
        return TestVerdict(TestResult.UNKNOWN,
                           "the baseline never ran; comparison is impossible",
                           duration_s=after.duration_s, command=after.command)

    before_ids, now_ids = _failed_ids(baseline.output), _failed_ids(after.output)
    if before_ids or now_ids:
        regressions = tuple(sorted(now_ids - before_ids))
        preexisting = tuple(sorted(now_ids & before_ids))
        if regressions:
            return TestVerdict(TestResult.REGRESSION,
                               f"{len(regressions)} test(s) passed before and broke",
                               preexisting=preexisting, regressions=regressions,
                               duration_s=after.duration_s, command=after.command)
        return TestVerdict(TestResult.PREEXISTING_FAILURE,
                           f"{len(preexisting)} failure(s) already existed before",
                           preexisting=preexisting,
                           duration_s=after.duration_s, command=after.command)

    # No identifiers extracted: only the exit code is left to compare, and that
    # is less precise. Saying so beats pretending precision.
    if baseline.exit_code != 0:
        return TestVerdict(TestResult.PREEXISTING_FAILURE,
                           "was already failing (compared by exit code)",
                           duration_s=after.duration_s, command=after.command)
    return TestVerdict(TestResult.REGRESSION,
                       "passed before and fails now (compared by exit code)",
                       duration_s=after.duration_s, command=after.command)


#: How to discover a repository's test command, from evidence on disk. Order
#: matters: most specific first. Nothing is assumed -- when no marker file
#: exists, the engine says it does not know how to test, instead of guessing
#: `pytest` and reporting an infrastructure failure it caused itself.
TEST_DETECTION = (
    ("pytest.ini", ["python", "-m", "pytest", "-q"]),
    ("tox.ini", ["python", "-m", "pytest", "-q"]),
    ("pyproject.toml", ["python", "-m", "pytest", "-q"]),
    ("phpunit.xml", ["vendor/bin/phpunit"]),
    ("phpunit.xml.dist", ["vendor/bin/phpunit"]),
    ("go.mod", ["go", "test", "./..."]),
    ("Cargo.toml", ["cargo", "test"]),
    ("package.json", ["npm", "test", "--silent"]),
)


def python_for(root: str | Path) -> str:
    """Which interpreter can actually run this repository's tests.

    `python` from PATH is the wrong default and fails quietly: it resolves to
    whatever interpreter happens to be first, which usually lacks the test
    dependencies. Order: the repository's own virtualenv, then the interpreter
    running the engine (which at least has pytest), then PATH as a last resort.
    """
    base = Path(root)
    for candidate in (base / ".venv" / "Scripts" / "python.exe",
                      base / ".venv" / "bin" / "python",
                      base / "venv" / "Scripts" / "python.exe",
                      base / "venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable or "python"


def detect_command(root: str | Path) -> list[str] | None:
    """Discover how to test, from evidence. `None` when it cannot be known."""
    r = Path(root)
    for marker, command in TEST_DETECTION:
        if not (r / marker).is_file():
            continue
        if marker == "pyproject.toml":
            text = (r / marker).read_text(encoding="utf-8", errors="replace")
            # `pyproject.toml` exists in projects with no tests at all. Requiring
            # the section (or a tests dir) avoids calling pytest where it has
            # nothing to run -- which would look like infrastructure failure.
            if "pytest" not in text and not (r / "tests").is_dir():
                continue
        resolved = list(command)
        if resolved[0] == "python":
            resolved[0] = python_for(r)
        return resolved
    return None
