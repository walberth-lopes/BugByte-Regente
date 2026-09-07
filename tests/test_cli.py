# -*- coding: utf-8 -*-
"""The CLI wiring, checked in the source.

The defect this file exists for: argparse declared `--minhas` while the handler
read `args.mine`. Every such pair is an `AttributeError` on the command's first
line, and `decide`, `sombra` and `cadeia` were all dead in the tree at once.

Nothing caught it because no test invoked the CLI, and the mismatch is invisible
to a reader -- the declaration and the read sit two hundred lines apart. So the
check is mechanical, like the boundary test: read the source and compare the two
sides.
"""

from __future__ import annotations

import ast
import dataclasses
import os
import re
from pathlib import Path

from regente.app.container import Engine
from regente.cli import REPORTS_DIR, _report_path, _write_report

CLI = Path(__file__).resolve().parents[1] / "regente" / "cli.py"
README = Path(__file__).resolve().parents[1] / "README.md"

#: A row of the README command table: `| `name` | what it does |`.
#: Whitespace-tolerant so reformatting the table does not break the check.
TABLE_ROW = re.compile(r"^\|\s*`([a-z][a-z-]*)`\s*\|", re.M)

#: Defined on the top-level parser, so available to every subcommand.
GLOBAL_ARGS = frozenset({"config", "cmd", "fn"})


def _tree() -> ast.Module:
    return ast.parse(CLI.read_text(encoding="utf-8"))


def _declared() -> tuple[dict[str, set[str]], dict[str, str]]:
    """(subcommand -> argparse dests, subcommand -> handler function name)."""
    dests: dict[str, set[str]] = {}
    handlers: dict[str, str] = {}
    current: str | None = None

    for node in ast.walk(_tree()):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr == "add_parser" and node.args:
            current = node.args[0].value
            dests.setdefault(current, set())
        elif node.func.attr == "add_argument" and current is not None:
            dests[current].add(_dest_of(node))
        elif node.func.attr == "set_defaults" and current is not None:
            for kw in node.keywords:
                if kw.arg == "fn" and isinstance(kw.value, ast.Name):
                    handlers[current] = kw.value.id
    return dests, handlers


def _dest_of(call: ast.Call) -> str:
    """The attribute name argparse will put on the Namespace."""
    for kw in call.keywords:
        if kw.arg == "dest" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    # Without an explicit dest, argparse uses the longest option string.
    flags = [a.value for a in call.args if isinstance(a, ast.Constant)]
    return max(flags, key=len).lstrip("-").replace("-", "_")


def _subcommands_in_order() -> list[str]:
    """Subcommand names, in the order `main` declares them.

    Sorted by line number rather than trusting `ast.walk`, whose breadth-first
    order happens to match the source here but is not promised to.
    """
    found = [
        (n.lineno, n.args[0].value)
        for n in ast.walk(_tree())
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "add_parser" and n.args
    ]
    return [name for _, name in sorted(found)]


def _documented_in_order() -> list[str]:
    return TABLE_ROW.findall(README.read_text(encoding="utf-8"))


def _read_by_handlers() -> dict[str, set[str]]:
    """cmd_* function name -> the `args.X` attributes it reads."""
    out: dict[str, set[str]] = {}
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("cmd_"):
            out[node.name] = {
                n.attr for n in ast.walk(node)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "args"
            }
    return out


def test_every_argument_a_handler_reads_is_declared():
    """`args.X` with no `add_argument` for X kills the command on its first line."""
    dests, handlers = _declared()
    read = _read_by_handlers()

    broken: list[str] = []
    for subcommand, fn in sorted(handlers.items()):
        missing = read.get(fn, set()) - dests[subcommand] - GLOBAL_ARGS
        for name in sorted(missing):
            broken.append(f"{subcommand}: {fn} reads args.{name}, never declared")
    assert not broken, "\n".join(broken)


def test_every_subcommand_has_a_handler():
    """A subcommand without `fn` fails with AttributeError inside `main`."""
    dests, handlers = _declared()
    assert set(dests) == set(handlers), sorted(set(dests) ^ set(handlers))


def test_readme_documents_every_subcommand():
    """`mission` shipped with a milestone and stayed out of the table for all of it.

    A command nobody wrote down is a command nobody runs. This is the cheap half
    of the check -- membership, not order -- so deleting the order test below
    leaves the drift itself still guarded.
    """
    declared = _subcommands_in_order()
    documented = _documented_in_order()

    assert documented, (
        "no rows matched in the README command table. Either the table is gone "
        f"or its format changed and {TABLE_ROW.pattern!r} no longer matches it."
    )

    undocumented = [c for c in declared if c not in documented]
    stale = [c for c in documented if c not in declared]
    assert not undocumented, f"subcommands with no README row: {undocumented}"
    assert not stale, f"README rows for commands that do not exist: {stale}"


def test_readme_table_follows_the_parser_order():
    """A convention, not a defect -- delete this test if you reorder on purpose.

    Kept separate from the coverage check above so that dropping the convention
    does not also drop the guard that matters.
    """
    assert _documented_in_order() == _subcommands_in_order()


def test_bare_output_name_goes_to_reports():
    """`--output r.txt` is a NAME, not a location: it must not land in the root.

    This is the case that actually happened -- a report written beside the
    source, untracked, one `git add -A` from being committed.
    """
    assert _report_path("r.txt") == Path(REPORTS_DIR) / "r.txt"
    assert _report_path("SHADOW-REPORT.txt") == Path(REPORTS_DIR) / "SHADOW-REPORT.txt"


def test_output_with_a_directory_is_respected():
    """A caller who typed a place gets that place, including `.`."""
    assert _report_path("docs/r.txt") == Path("docs/r.txt")
    assert _report_path(os.path.join("docs", "r.txt")) == Path("docs") / "r.txt"

    # `./r.txt` means HERE. pathlib normalises the `./` away, so the rule has to
    # look at the raw string -- otherwise "here" is indistinguishable from a
    # bare name and this would be silently relocated.
    assert _report_path("./r.txt") == Path("./r.txt")
    assert _report_path("./r.txt").parent != Path(REPORTS_DIR)


def test_absolute_output_is_never_relocated(tmp_path):
    """An absolute path is unambiguous; relocating it would be user-hostile."""
    target = tmp_path / "r.txt"
    assert _report_path(str(target)) == target


def test_write_report_creates_the_directory_and_reports_where(tmp_path, monkeypatch):
    """The path printed back has to be where the bytes went, not what was typed."""
    monkeypatch.chdir(tmp_path)

    written = _write_report("r.txt", "hello")
    assert written == Path(REPORTS_DIR) / "r.txt"
    assert (tmp_path / REPORTS_DIR / "r.txt").read_text(encoding="utf-8") == "hello"
    assert not (tmp_path / "r.txt").exists()

    # A directory the caller named is created too, rather than raising.
    written = _write_report("deep/nested/r.txt", "hello")
    assert written == Path("deep/nested/r.txt")
    assert (tmp_path / "deep" / "nested" / "r.txt").read_text(encoding="utf-8") == "hello"


def test_engine_attributes_the_cli_uses_exist():
    """The other half of the same defect: `motor.resolvedor` on a slots dataclass.

    `Engine` uses `slots=True`, so a stale attribute name is an AttributeError at
    runtime and nothing else -- no import error, no type error, no test failure.
    """
    known = {f.name for f in dataclasses.fields(Engine)}
    known |= {m for m in dir(Engine) if not m.startswith("__")}

    broken: list[str] = []
    for node in ast.walk(_tree()):
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("cmd_")):
            continue
        for n in ast.walk(node):
            if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                    and n.value.id in ("motor", "engine") and n.attr not in known):
                broken.append(f"cli.py:{n.lineno} {node.name}: {n.value.id}.{n.attr}")
    assert not broken, "\n".join(broken)
