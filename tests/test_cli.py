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
from pathlib import Path

from regente.app.container import Engine

CLI = Path(__file__).resolve().parents[1] / "regente" / "cli.py"

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
