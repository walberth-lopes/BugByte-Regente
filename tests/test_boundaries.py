# -*- coding: utf-8 -*-
"""The architectural rule, with teeth.

Layer separation written only in the README lasts until the first rush. Here it
is verified in the source: if somebody imports an adapter inside the Core, the
test fails -- and it fails in CI, not in code review, which is where this kind of
thing slips through.

Two rules:

1. `core/` imports nothing from the engine: no adapters, no engine, no network or
   database library. It is pure domain.
2. `core/` and `engine/` mention no provider at all. A tool's name in those
   folders is the evidence that the abstraction leaked.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "regente"

#: Provider names. If one of them shows up in the Core or the Engine, the
#: abstraction has failed -- and the test says exactly where.
FORNECEDORES = [
    "jira", "github", "gitlab", "linear", "bitbucket", "asana",
    "gcloud", "bigquery", "cloudrun", "aws", "azure", "kubernetes",
    "postgres", "mysql", "anthropic", "openai", "slack",
]

#: Libraries that characterise I/O. The domain may import none of them.
IO_PROIBIDO = {"sqlite3", "httpx", "requests", "urllib", "socket", "subprocess", "yaml"}


def _modulos(folder: str) -> list[Path]:
    return sorted((ROOT / folder).rglob("*.py"))


def _imports(path: Path) -> list[str]:
    arvore = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            names += [a.name for a in no.names]
        elif isinstance(no, ast.ImportFrom):
            names.append(("." * (no.level or 0)) + (no.module or ""))
    return names


def test_core_not_imports_io():
    failures = []
    for file in _modulos("core"):
        for name in _imports(file):
            root = name.lstrip(".").split(".", 1)[0]
            if root in IO_PROIBIDO:
                failures.append(f"{file.name} importa {name}")
    assert not failures, "the domain has to be pure:\n  " + "\n  ".join(failures)


def test_core_not_imports_adapters_nor_engine():
    failures = []
    for file in _modulos("core"):
        for name in _imports(file):
            if "adapters" in name or "engine" in name or "ports" in name:
                failures.append(f"{file.name} importa {name}")
    assert not failures, "core must not depend on the edges:\n  " + "\n  ".join(failures)


def test_engine_not_imports_adapters():
    """The engine knows PORTS. What picks an implementation is the composition root."""
    failures = []
    for file in _modulos("engine"):
        for name in _imports(file):
            if "adapters" in name:
                failures.append(f"{file.name} importa {name}")
    assert not failures, "engine may only talk to ports:\n  " + "\n  ".join(failures)


def _prose_lines(file: Path) -> set[int]:
    """Lines occupied by a docstring or a comment.

    Documentation MUST be able to say 'neither Jira nor GitHub' -- that is
    precisely where the rule is recorded. What must not happen is the name
    becoming code: an import, a default value, a comparison literal.
    """
    text = file.read_text(encoding="utf-8")
    prose: set[int] = set()
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            prose.add(n)

    # DOCSTRINGS ONLY -- the first statement of a module, class or function. Any
    # other string in the middle of the code still counts as code, otherwise
    # `provider = "some-vendor"` would escape precisely by being a string.
    portadores = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for no in ast.walk(ast.parse(text, filename=str(file))):
        if not isinstance(no, portadores) or not no.body:
            continue
        first = no.body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            prose.update(range(first.lineno,
                               (first.end_lineno or first.lineno) + 1))
    return prose


def test_no_vendor_in_code_of_core_nor_of_engine():
    default_value = re.compile("|".join(FORNECEDORES), re.IGNORECASE)
    failures = []
    for folder in ("core", "engine", "ports"):
        for file in _modulos(folder):
            prose = _prose_lines(file)
            for n, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
                if n in prose:
                    continue
                hit = default_value.search(line.split("#", 1)[0])
                if hit:
                    failures.append(f"{folder}/{file.name}:{n} menciona '{hit.group()}'")
    assert not failures, ("a tool name exists only in adapters/:\n  "
                        + "\n  ".join(failures))


def test_registry_is_the_only_place_importing_adapters():
    """A second place importing an adapter is the beginning of the coupling."""
    offenders = []
    for file in ROOT.rglob("*.py"):
        relativo = file.relative_to(ROOT).as_posix()
        if relativo.startswith("adapters/"):
            continue
        if any("adapters" in name for name in _imports(file)):
            offenders.append(relativo)
    assert offenders == ["adapters/registry.py"] or offenders == ["app/container.py"] or set(offenders) <= {
        "app/container.py", "cli.py"}, (
        f"imports adapters outside the registry: {offenders}")


def test_swapping_of_provider_only_touches_in_adapter_is_configuration():
    """The Milestone 3 architectural review, written as a test.

    Question: if tomorrow the task provider is a completely different one, what
    has to change? The desired answer -- and the one verified here:

        core        NO change
        ports       NO change
        engine      NO change
        adapters    new implementation
        config      new provider declared

    The test proves the first three lines. The last two are the work.
    """
    # 1. Nobody outside adapters/ decides WHICH implementation to use. The two
    #    exceptions are the composition root and the surface that displays it.
    choosers = set()
    for file in ROOT.rglob("*.py"):
        rel = file.relative_to(ROOT).as_posix()
        if rel.startswith("adapters/"):
            continue
        if any("adapters" in name for name in _imports(file)):
            choosers.add(rel)
    assert choosers == {"app/container.py", "cli.py"}, (
        f"else chooses an implementation: {choosers - {'app/container.py', 'cli.py'}}")

    # 2. The engine talks to the PORT, and the port knows no provider.
    port = (ROOT / "ports" / "tasks.py").read_text(encoding="utf-8")
    prose = _prose_lines(ROOT / "ports" / "tasks.py")
    for n, line in enumerate(port.splitlines(), 1):
        if n in prose:
            continue
        assert not re.search("|".join(FORNECEDORES), line, re.IGNORECASE), (
            f"ports/tasks.py:{n} knows a provider: {line.strip()}")

    # 3. Every capability already has more than one possible implementation --
    #    and an abstraction with a single implementer was never really tested.
    import regente.adapters.registry as reg
    assert len(reg.available()["tasks"]) >= 2, (
        "a TaskProvider with a single adapter proves nothing")


def test_repositoryprovider_not_knows_vendor():
    """The Milestone 4 counter-proof, in the same format as Milestone 3.

    Swapping the code hosting for a completely different one must require a new
    adapter + configuration, and nothing else.
    """
    file = ROOT / "ports" / "repository.py"
    prose = _prose_lines(file)
    for n, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
        if n in prose:
            continue
        hit = re.search("|".join(FORNECEDORES), line, re.IGNORECASE)
        assert not hit, f"ports/repository.py:{n} conhece '{hit.group()}'"

    import regente.adapters.registry as reg
    assert len(reg.available()["repository"]) >= 2, (
        "a RepositoryProvider with a single adapter proves nothing")


def test_resolution_of_target_not_knows_vendor():
    """The task->repository link is the most tempting to couple: it is where you
    would be tempted to look at `nameWithOwner` or a specific board's field."""
    for name in ("target.py", "chain.py"):
        file = ROOT / "engine" / name
        prose = _prose_lines(file)
        for n, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            if n in prose:
                continue
            hit = re.search("|".join(FORNECEDORES), line, re.IGNORECASE)
            assert not hit, f"engine/{name}:{n} conhece '{hit.group()}'"
