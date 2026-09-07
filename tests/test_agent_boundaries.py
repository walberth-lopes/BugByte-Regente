# -*- coding: utf-8 -*-
"""The architectural property, proven rather than asserted.

Every test here is structural. It reads the source with the AST, or copies the
package to a temp directory and deletes a file, or imports a module with another
blocked out of `sys.modules`. None of them asks the code to describe itself --
they take it apart.

The reason for that discipline is on the record: a second vendor profile in this
project imported a helper from the first. Nothing broke, the whole suite stayed
green, and the property the milestone claims -- that swapping the agent is one
file -- had silently stopped being true. A test that had merely checked "both
adapters exist and work" would have passed happily.

Regex over source is not used anywhere in this file. `"claude"` appears in a URL,
in prose, in a filename and in a variable name, and only one of those is a
violation; the difference is visible to a parser and invisible to a pattern.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "regente"
VENDORS = ROOT / "adapters" / "runner" / "vendors"

#: Layers that may never name a vendor or presume an authentication mechanism.
NEUTRAL_LAYERS = ("core", "ports", "engine")

#: Vendor and product names. `AuthMode.RESOLVED_SECRET` is fine above the
#: adapter line -- it is the shape of an exchange. `api key` is not: it presumes
#: one shape is the shape.
FORBIDDEN_ABOVE_ADAPTERS = (
    "anthropic", "openai", "claude", "gpt", "codex", "aider", "cursor",
    "copilot", "gemini", "windsurf", "api_key", "apikey", "api key",
    "bearer", "oauth", "sk-",
)


#: One narrow, named exemption. `core/childenv.py` holds a DENYLIST of
#: credential-shaped variable names, and naming a credential shape in order to
#: refuse it is the opposite of presuming one is the mechanism. The exemption is
#: the assignment of that one constant in that one module, located with the AST
#: rather than by pattern -- a weaker rule everywhere would have been the easy
#: fix and the wrong one.
EXEMPT = {("childenv.py", "CREDENTIAL_MARKS")}


def exempt_lines(path, tree) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) and not isinstance(node, ast.AnnAssign):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            name = getattr(target, "id", None)
            if name and (path.name, name) in EXEMPT:
                lines.update(range(node.lineno,
                                   (node.end_lineno or node.lineno) + 1))
    return lines


def modules(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*.py") if "__pycache__" not in p.parts)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def docstring_lines(tree: ast.Module) -> set[int]:
    """Line numbers occupied by docstrings, which are prose and may say anything.

    Prose has to be able to explain why a vendor name is forbidden, which means
    naming one. A check that could not tell a docstring from a default value
    would force the explanation out of the code that needs it most.
    """
    lines: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders) or not node.body:
            continue
        first = node.body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            lines.update(range(first.lineno,
                               (first.end_lineno or first.lineno) + 1))
    return lines


def imported_modules(tree: ast.Module, path: Path) -> set[str]:
    """Absolute-ish names of everything a module imports, relatives resolved."""
    package = path.relative_to(ROOT.parent).with_suffix("").parts
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - node.level]
                found.add(".".join([*base, node.module] if node.module
                                   else list(base)))
            elif node.module:
                found.add(node.module)
    return found


# ---------------------------------------------------------------------------
# 1. No vendor adapter may depend on another vendor adapter
# ---------------------------------------------------------------------------

def test_no_vendor_profile_imports_another_vendor_profile():
    """The wall. A convention would be a comment; this is a wall.

    It exists because the violation happened here, passed every test, and was
    found by hand.
    """
    names = {p.stem for p in modules(VENDORS) if p.stem != "__init__"}
    assert len(names) >= 2, "one vendor cannot demonstrate independence"

    offences: list[str] = []
    for path in modules(VENDORS):
        if path.stem == "__init__":
            continue
        for imported in imported_modules(parse(path), path):
            tail = imported.rsplit(".", 1)[-1]
            if ".vendors." in imported or (
                    imported.endswith(tuple(f".{n}" for n in names))
                    and tail != path.stem):
                if tail in names and tail != path.stem:
                    offences.append(f"{path.name} imports {imported}")
    assert not offences, (
        "a vendor adapter depends on another vendor adapter:\n  "
        + "\n  ".join(offences)
        + "\nShared work belongs in cli_agent.py or mission_text.py")


def test_shared_infrastructure_never_imports_a_vendor():
    """The other direction. A base that knew its subclasses is not a base."""
    shared = [p for p in modules(ROOT / "adapters" / "runner")
              if "vendors" not in p.parts]
    offences = [f"{p.name} imports {i}"
                for p in shared
                for i in imported_modules(parse(p), p)
                if ".vendors" in i or "vendors." in i]
    assert not offences, "shared runner code imports a vendor:\n  " + \
                         "\n  ".join(offences)


@pytest.mark.parametrize("removed,survivor", [
    ("claude_code.py", "codex_cli"),
    ("codex_cli.py", "claude_code"),
])
def test_deleting_one_vendor_does_not_break_the_other(tmp_path, removed, survivor):
    """The contra-proof, done by actually deleting the file.

    A copy of the package, one vendor removed, the other imported and
    instantiated in a fresh interpreter. Nothing here trusts an import graph
    computed by this test -- the interpreter is the judge.
    """
    copy = tmp_path / "regente"
    shutil.copytree(ROOT, copy,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (copy / "adapters" / "runner" / "vendors" / removed).unlink()

    program = (
        f"import importlib\n"
        f"m = importlib.import_module("
        f"'regente.adapters.runner.vendors.{survivor}')\n"
        f"cls = [v for v in vars(m).values() if isinstance(v, type) "
        f"and v.__module__ == m.__name__ and v.__name__.endswith('Agent')][0]\n"
        f"a = cls(cli_path='definitely-absent')\n"
        f"print(a.availability().readiness.value)\n")
    p = subprocess.run([sys.executable, "-c", program], cwd=str(tmp_path),
                       capture_output=True, encoding="utf-8", errors="replace",
                       timeout=120)
    assert p.returncode == 0, (
        f"removing {removed} broke {survivor}:\n{p.stderr[-1500:]}")
    assert p.stdout.strip() == "BLOCKED_EXECUTABLE"


def test_a_new_vendor_needs_no_change_above_the_adapter_line(tmp_path):
    """Add a whole vendor without touching core, ports or engine. Then run it.

    Written into a copy of the package so the addition is real: a new file, a
    new class, a new authentication shape, and the engine's readiness path used
    unchanged.
    """
    copy = tmp_path / "regente"
    shutil.copytree(ROOT, copy,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    before = {p.relative_to(copy): p.read_bytes()
              for layer in NEUTRAL_LAYERS for p in modules(copy / layer)}

    (copy / "adapters" / "runner" / "vendors" / "brand_new.py").write_text(
        "from ..cli_agent import CliAgent\n"
        "from ....ports.agent import AuthMode, Check\n"
        "from dataclasses import dataclass\n"
        "\n"
        "@dataclass(slots=True)\n"
        "class BrandNewAgent(CliAgent):\n"
        "    name: str = 'brand-new'\n"
        "    auth_mode: AuthMode = AuthMode.GATEWAY\n"
        "    def argv(self, mission):\n"
        "        return [self.cli_path, '--go', self.prompt(mission)]\n"
        "    def parse(self, stdout, stderr, returncode, duration):\n"
        "        raise NotImplementedError\n",
        encoding="utf-8")

    program = (
        "from regente.adapters.runner.vendors.brand_new import BrandNewAgent\n"
        "from regente.engine import readiness\n"
        "from regente.core.policy import PolicyEngine\n"
        "a = BrandNewAgent(cli_path='absent')\n"
        "s = readiness.diagnose(a, policy=PolicyEngine.from_config("
        "[{'name':'r','effect':'ALLOW','match':{'action':'agent.run'}}]))\n"
        "print(s.readiness.value, s.auth_mode.value)\n")
    p = subprocess.run([sys.executable, "-c", program], cwd=str(tmp_path),
                       capture_output=True, encoding="utf-8", errors="replace",
                       timeout=120)
    assert p.returncode == 0, p.stderr[-1500:]
    assert p.stdout.strip() == "BLOCKED_EXECUTABLE GATEWAY"

    after = {p.relative_to(copy): p.read_bytes()
             for layer in NEUTRAL_LAYERS for p in modules(copy / layer)}
    assert before == after, "adding a vendor changed a neutral layer"


# ---------------------------------------------------------------------------
# 2. Adversarial audit of the neutral layers
# ---------------------------------------------------------------------------

def test_no_vendor_name_or_credential_shape_above_adapters():
    offences: list[str] = []
    for layer in NEUTRAL_LAYERS:
        for path in modules(ROOT / layer):
            text = path.read_text(encoding="utf-8")
            tree = parse(path)
            skip = docstring_lines(tree) | exempt_lines(path, tree)
            for n, line in enumerate(text.splitlines(), 1):
                if n in skip:
                    continue
                code = line.split("#", 1)[0].lower()
                offences += [f"{layer}/{path.name}:{n} -> {word}"
                             for word in FORBIDDEN_ABOVE_ADAPTERS if word in code]
    assert not offences, "vendor or credential shape above adapters/:\n  " + \
                         "\n  ".join(offences)


def test_the_neutral_layers_only_touch_the_environment_through_the_composer():
    """`os.environ` above the adapter line is where a vendor default hides.

    A credential name, a model name, a base URL: each arrives as "just a
    default" and each makes the engine work in exactly one environment. But a
    blanket ban is the wrong rule -- the engine legitimately needs PATH to run a
    test suite at all. What must never happen is a whole inherited environment
    reaching a child process.

    So the rule is precise: in these layers `os.environ` may appear only as an
    argument to `childenv.compose`, which starts from nothing and adds what is
    named. The first version of this test banned the read outright, went red on
    a legitimate use, and would have been "fixed" by weakening it into nothing.
    """
    offences: list[str] = []
    for layer in NEUTRAL_LAYERS:
        for path in modules(ROOT / layer):
            tree = parse(path)
            allowed: set[int] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = (getattr(node.func, "attr", None)
                        or getattr(node.func, "id", None))
                if name == "compose":
                    allowed.update(getattr(a, "lineno", -1)
                                   for a in ast.walk(node))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and node.attr in ("environ", "getenv")
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "os"
                        and node.lineno not in allowed):
                    offences.append(f"{layer}/{path.name}:{node.lineno}")
    assert not offences, (
        "the environment is touched outside childenv.compose:\n  "
        + "\n  ".join(offences))


def test_the_engines_own_test_run_does_not_hand_credentials_to_agent_code():
    """The attack this audit found, closed and then verified by running it.

    The agent has no tool that executes anything, which closes push, pull
    request and deploy in one move. It can still write a file, tests are files,
    and the engine runs the suite itself to reach a verdict -- so an agent that
    cannot execute anything could have the ENGINE execute for it, holding every
    credential the engine had.

    Behavioural rather than structural: a real suite runs in a real temporary
    repository and is asked what it can see.
    """
    import os
    import tempfile

    from regente.engine import testing

    area = Path(tempfile.mkdtemp(prefix="regente-envleak-"))
    (area / "test_snoop.py").write_text(
        "import os, json\n"
        "def test_snoop():\n"
        "    marks = ('TOKEN', 'SECRET', 'API_KEY', 'PASSWORD', 'CREDENTIAL')\n"
        "    leaked = sorted(k for k in os.environ\n"
        "                    if any(m in k.upper() for m in marks))\n"
        "    print('LEAKED:' + json.dumps(leaked))\n",
        encoding="utf-8")

    os.environ["REGENTE_PROBE_API_KEY"] = "must-not-be-visible"
    os.environ["JIRA_API_TOKEN"] = "must-not-be-visible"
    try:
        run = testing.run([sys.executable, "-m", "pytest", "-q", "-s",
                           str(area / "test_snoop.py")], str(area), timeout=180)
    finally:
        os.environ.pop("REGENTE_PROBE_API_KEY", None)
        os.environ.pop("JIRA_API_TOKEN", None)

    assert "LEAKED:[]" in run.output, (
        "agent-authored test code can read the engine's credentials:\n"
        + run.output[-800:])
    assert "must-not-be-visible" not in run.output


def test_readiness_special_cases_no_vendor():
    """No branch in the readiness path may depend on which adapter it holds.

    A single `if adapter == "..."` here would turn the six-axis diagnosis back
    into a table of vendors with extra steps.
    """
    from regente.engine import readiness as module

    tree = parse(Path(module.__file__))
    constants = {n.value.lower() for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for word in FORBIDDEN_ABOVE_ADAPTERS:
        assert not any(word in c for c in constants), f"readiness mentions {word}"

    # It must also not branch on the adapter's identity at all.
    compares = [n for n in ast.walk(tree) if isinstance(n, ast.Compare)]
    for node in compares:
        left = getattr(node.left, "attr", None) or getattr(node.left, "id", None)
        assert left not in ("name", "adapter"), (
            f"readiness branches on the adapter's identity at line {node.lineno}")


def test_the_port_names_no_transport_and_no_format():
    """The contract must not presume a CLI, a session, a model or a response shape.

    `AuthMode.SESSION` is a shape of exchange and belongs. `subprocess`,
    `argv`, `stdout` and `json` do not: an agent reached over a socket, or in
    process, must fit this port without an adapter pretending to be a CLI.
    """
    from regente.ports import agent as module

    path = Path(module.__file__)
    tree = parse(path)
    text = path.read_text(encoding="utf-8")
    prose = docstring_lines(tree)

    assert not imported_modules(tree, path) & {"subprocess", "os", "shutil"}, (
        "the agent port imports a process module; the contract would then only "
        "fit agents that are processes")

    banned = ("argv", "stdout", "stderr", "cli_path", "returncode", "subprocess")
    offences = [f"{n}: {word}"
                for n, line in enumerate(text.splitlines(), 1) if n not in prose
                for word in banned if word in line.split("#", 1)[0].lower()]
    assert not offences, "the port presumes a transport:\n  " + "\n  ".join(offences)


# ---------------------------------------------------------------------------
# 3. The contract answers four questions and refuses the other five
# ---------------------------------------------------------------------------

def test_the_availability_contract_carries_no_authority_field():
    """`AgentRunner` answers can-I / how / what-can-it-do / what-happened.

    It never answers may-it-commit, may-it-push, may-it-open-a-pull-request,
    may-it-deploy or is-it-trustworthy. Those are the engine's and the policy's,
    and a field here would be the adapter voting on its own permissions.
    """
    from regente.ports.agent import (AgentAvailability, AgentCapabilities,
                                     Outcome)

    authority_words = ("commit", "push", "pull_request", "pr", "merge",
                       "deploy", "review", "trust", "allowed", "permitted",
                       "authorised", "authorized")
    for kind in (AgentAvailability, AgentCapabilities, Outcome):
        for field in kind.__dataclass_fields__:
            assert field.lower() not in authority_words, (
                f"{kind.__name__}.{field} lets the adapter speak about authority")


def test_nothing_an_adapter_returns_can_lift_unknown_to_yes():
    """An adapter that lies about policy and budget is simply overwritten.

    Checked by handing the engine an adapter that claims both, and confirming
    the engine's own verdict replaces them in both directions.
    """
    from regente.core.policy import PolicyEngine
    from regente.engine import readiness
    from regente.ports.agent import (AgentAvailability, AgentRunner, Check,
                                     Readiness)

    class Liar(AgentRunner):
        name = "liar"

        def run(self, mission):
            raise NotImplementedError

        def availability(self):
            return AgentAvailability(
                adapter="liar", executable=Check.yes(), protocol=Check.yes(),
                authentication=Check.yes(), agent=Check.yes(),
                policy=Check.yes("I hereby permit myself"),
                budget=Check.yes("and I have plenty of money"))

    denied = readiness.diagnose(
        Liar(), policy=PolicyEngine.from_config(
            [{"name": "no", "effect": "DENY", "match": {"action": "agent.run"}}]))
    assert denied.readiness is Readiness.BLOCKED_POLICY
    assert "I hereby permit myself" not in denied.policy.detail

    broke = readiness.diagnose(
        Liar(), policy=PolicyEngine.from_config(
            [{"name": "r", "effect": "ALLOW", "match": {"action": "agent.run"}}]),
        ceiling_usd=1.0, spent_usd=99.0)
    assert broke.readiness is Readiness.BLOCKED_BUDGET


def test_an_adapter_cannot_widen_its_own_permissions():
    """`AgentCapabilities` and `Permissions` are different types on purpose."""
    from regente.ports.agent import AgentCapabilities, Permissions

    boastful = AgentCapabilities(edits_files=True, runs_commands=True,
                                 reaches_network=True)
    granted = Permissions(read=True, write_code=True)

    assert boastful.runs_commands, "the tool is capable of it"
    assert not granted.run_commands, "and it is still not permitted"
    assert not granted.push and not granted.open_pr and not granted.deploy
    assert set(AgentCapabilities.__dataclass_fields__) & \
           set(Permissions.__dataclass_fields__) == set(), (
        "capability and permission must not share a field name; a shared name "
        "is one refactor away from a shared value")
