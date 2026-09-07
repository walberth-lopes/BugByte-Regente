# -*- coding: utf-8 -*-
"""Authentication is the adapter's business, and the diagnosis says so.

The failure this file guards against is subtle and expensive: an engine that
works only where an API key exists. Clients do not all have one. A corporate
coding-agent subscription, a CLI sign-in, SSO, a workspace credential, an
internal gateway and an API key are all ordinary ways to authenticate an agent,
and an engine that recognises one of them is an engine for one client.

So the diagnosis is six independent axes and never a sentence about a variable:

    executable -> protocol -> authentication -> agent -> policy -> budget

`BLOCKED_AUTHENTICATION` tells its reader to go and authenticate, whatever that
means where they work. "ANTHROPIC_API_KEY missing" would tell a client whose
agent signs itself in to set a variable that agent never reads.

The last test is the architectural one: `core/`, `ports/` and `engine/` must not
name any vendor, so that swapping the agent is a file in `adapters/`.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from regente.adapters.runner.vendors.claude_code import ClaudeCodeAgent
from regente.adapters.runner.vendors.codex_cli import CodexCliAgent
from regente.adapters.runner.external import DeterministicAgent
from regente.adapters.runner.headless import HeadlessAgent, SandboxProfile
from regente.core.policy import AutonomyLevel, PolicyEngine
from regente.engine import readiness
from regente.ports.agent import (AgentAvailability, AgentRunner, AuthMode,
                                 Check, Readiness)

ALLOW = [{"name": "run", "effect": "ALLOW", "match": {"action": "agent.run"}}]
DENY = [{"name": "no", "effect": "DENY", "match": {"action": "agent.run"}}]


def fake_cli(tmp_path: Path, name: str, version: str = "1.2.3",
             auth: str | None = None, auth_exit: int = 0) -> str:
    """A real executable that answers the two probes. Not a mock of a probe.

    The probes run subprocesses, so a double that intercepted them would test
    the double. This is a script on disk that the adapter really executes.
    """
    script = tmp_path / f"{name}.py"
    script.write_text(textwrap.dedent(f"""
        import sys
        args = sys.argv[1:]
        if args[:1] == ["--version"]:
            print({version!r})
            sys.exit(0)
        if args[:2] in (["auth", "status"], ["login", "status"]):
            print({(auth or "")!r})
            sys.exit({auth_exit})
        sys.exit(0)
    """), encoding="utf-8")
    launcher = tmp_path / f"{name}.cmd"
    # The adapter builds `[cli_path, ...]`, so the "cli" must be one argv entry.
    # A tiny launcher keeps that true without the test reaching inside.
    launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
                        encoding="utf-8")
    return str(launcher if sys.platform == "win32" else script)


def signed_in(tmp_path: Path) -> str:
    return fake_cli(tmp_path, "signed-in",
                    auth=json.dumps({"loggedIn": True,
                                     "authMethod": "corporate-account"}))


def signed_out(tmp_path: Path) -> str:
    return fake_cli(tmp_path, "signed-out",
                    auth=json.dumps({"loggedIn": False, "authMethod": "none"}))


# ---------------------------------------------------------------------------
# 1 & 2 -- a CLI that holds its own session
# ---------------------------------------------------------------------------

def test_1_a_cli_authenticated_by_its_own_session_is_ready(tmp_path):
    """No key, no secret, no resolution. The tool signed itself in."""
    agent = ClaudeCodeAgent(cli_path=signed_in(tmp_path))
    state = agent.availability()

    assert state.auth_mode is AuthMode.SESSION
    assert state.authentication.ok is True
    assert "signed in" in state.authentication.detail
    assert "corporate-account" in state.authentication.detail
    assert state.readiness is Readiness.BLOCKED_POLICY, (
        "an adapter alone is never READY -- policy and budget are the engine's")


def test_2_a_cli_that_is_not_signed_in_blocks_on_authentication(tmp_path):
    agent = ClaudeCodeAgent(cli_path=signed_out(tmp_path))
    state = agent.availability()

    assert state.readiness is Readiness.BLOCKED_AUTHENTICATION
    assert "not signed in" in state.authentication.detail
    # The advice must fit any mechanism, and must not name a variable.
    assert "ANTHROPIC" not in state.blocking_reason().upper()
    assert "API KEY" not in state.blocking_reason().upper()
    assert state.agent.ok is None, "unreachable until authentication holds"


def test_the_engine_never_looks_in_the_environment_for_a_session_credential(
        tmp_path, monkeypatch):
    """A key in the environment must not make a signed-out tool look ready."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-something")
    agent = ClaudeCodeAgent(cli_path=signed_out(tmp_path))
    assert agent.availability().readiness is Readiness.BLOCKED_AUTHENTICATION


# ---------------------------------------------------------------------------
# 3 & 4 -- resolved credentials and gateways
# ---------------------------------------------------------------------------

class Secrets:
    """Stands in for the workspace's scoped SecretProvider."""

    def __init__(self, **values):
        self.values = values
        self.asked: list[str] = []

    def resolve(self, reference: str) -> str:
        self.asked.append(reference)
        if reference not in self.values:
            raise KeyError(f"'{reference}' is not resolvable in this workspace")
        return self.values[reference]


def test_3_an_agent_authenticated_by_a_resolved_credential_is_ready(tmp_path):
    agent = HeadlessAgent(
        command=[sys.executable, "-c", "pass"],
        auth_mode=AuthMode.RESOLVED_SECRET,
        sandbox=SandboxProfile(env={"SOME_VENDOR_KEY": "resolved-value"}))
    state = agent.availability()

    assert state.auth_mode is AuthMode.RESOLVED_SECRET
    assert state.authentication.ok is True
    assert "resolved 1 credential" in state.authentication.detail
    # The value never appears in the diagnosis. A diagnosis is read aloud and
    # pasted into tickets, and a credential in one is a credential leaked.
    assert "resolved-value" not in state.render()


def test_4_an_agent_behind_a_corporate_gateway_is_ready(tmp_path):
    agent = HeadlessAgent(
        command=[sys.executable, "-c", "pass"],
        auth_mode=AuthMode.GATEWAY,
        sandbox=SandboxProfile(env={"LLM_GATEWAY_URL": "https://gw.internal",
                                    "LLM_GATEWAY_TOKEN": "t"}))
    state = agent.availability()

    assert state.auth_mode is AuthMode.GATEWAY
    assert state.authentication.ok is True
    assert "https://gw.internal" not in state.render()


def test_5_a_missing_credential_blocks_on_authentication_not_on_the_executable():
    agent = HeadlessAgent(command=[sys.executable, "-c", "pass"],
                          auth_mode=AuthMode.RESOLVED_SECRET,
                          sandbox=SandboxProfile(env={}))
    state = agent.availability()

    assert state.readiness is Readiness.BLOCKED_AUTHENTICATION
    assert state.executable.ok is True, "the binary is fine; the credential is not"
    assert "none was resolved" in state.authentication.detail


def test_6_an_invalid_credential_surfaces_at_run_time_not_as_readiness(tmp_path):
    """A credential the engine holds cannot be validated without spending money.

    So `availability()` says yes -- it has one -- and the first real run reports
    the rejection. Pretending to know earlier would mean either a free oracle
    that does not exist, or a guess.
    """
    script = tmp_path / "rejecting.py"
    script.write_text(textwrap.dedent("""
        import json
        print(json.dumps({"status": "ERROR",
                          "summary": "the provider rejected the credential"}))
    """), encoding="utf-8")
    agent = HeadlessAgent(command=[sys.executable, str(script)],
                          auth_mode=AuthMode.RESOLVED_SECRET,
                          sandbox=SandboxProfile(env={"K": "wrong"}))

    assert agent.availability().authentication.ok is True

    from regente.ports.agent import ContextItem, ContextPackage, Mission
    outcome = agent.run(Mission(
        workspace_id="w", workspace_name="ws", task_key="K-1", run_id="r",
        allowed_root=str(tmp_path), goal="g",
        context=ContextPackage(goal="g", items=(
            ContextItem(kind="task", ref="K-1", reason="the work itself"),))))
    assert outcome.status.value == "ERROR"
    assert "rejected the credential" in outcome.summary


# ---------------------------------------------------------------------------
# 7 & 8 -- the executable, and an adapter that is present but not authenticated
# ---------------------------------------------------------------------------

def test_7_a_missing_executable_blocks_before_anything_else():
    agent = ClaudeCodeAgent(cli_path="definitely-not-installed-anywhere")
    state = agent.availability()

    assert state.readiness is Readiness.BLOCKED_EXECUTABLE
    assert state.authentication.ok is None, (
        "authentication was never probed, and UNKNOWN is the honest record")


def test_8_an_adapter_that_is_present_but_unauthenticated_says_exactly_that(tmp_path):
    """The distinction between 'not installed' and 'installed, not signed in'."""
    absent = ClaudeCodeAgent(cli_path="not-installed").availability()
    present = ClaudeCodeAgent(cli_path=signed_out(tmp_path)).availability()

    assert absent.readiness is Readiness.BLOCKED_EXECUTABLE
    assert present.readiness is Readiness.BLOCKED_AUTHENTICATION
    assert present.executable.ok is True
    assert present.protocol.ok is True


def test_an_axis_nobody_could_determine_never_counts_as_passing():
    """UNKNOWN blocks. 'We could not check' must not become 'it is fine'."""
    state = AgentAvailability(
        adapter="x", executable=Check.yes(), protocol=Check.yes(),
        authentication=Check.unknown("the tool offers no way to ask"),
        agent=Check.yes(), policy=Check.yes(), budget=Check.yes())
    assert state.readiness is Readiness.BLOCKED_AUTHENTICATION


def test_a_delegated_login_is_never_assumed_to_work():
    agent = HeadlessAgent(command=[sys.executable, "-c", "pass"],
                          auth_mode=AuthMode.DELEGATED)
    assert agent.availability().readiness is Readiness.BLOCKED_AUTHENTICATION


# ---------------------------------------------------------------------------
# 9 -- two workspaces, two mechanisms, at the same time
# ---------------------------------------------------------------------------

def test_9_two_workspaces_may_authenticate_by_different_mechanisms(tmp_path):
    """The point of the whole redesign, asserted in one place.

    One client's agent signs itself in; the other's is reached through a
    gateway whose credential the engine resolves. Both run through the same
    port, the same loop and the same policy, and neither knows the other exists.
    """
    from regente.adapters import registry
    from regente.ports import Capability

    subscription = registry.create(Capability.RUNNER, "claude-code", {
        "cli": signed_in(tmp_path), "auth": "session"})
    gateway_secrets = Secrets(**{"env:CORP_GATEWAY_TOKEN": "gw-token"})
    gateway = registry.create(Capability.RUNNER, "headless-agent", {
        "command": [sys.executable, "-c", "pass"],
        "auth": "gateway",
        "credentials": {"LLM_GATEWAY_TOKEN": "env:CORP_GATEWAY_TOKEN"},
        "secrets": gateway_secrets})

    a, b = subscription.availability(), gateway.availability()
    assert a.auth_mode is AuthMode.SESSION
    assert b.auth_mode is AuthMode.GATEWAY
    assert a.authentication.ok is True and b.authentication.ok is True
    assert gateway_secrets.asked == ["env:CORP_GATEWAY_TOKEN"]
    # Neither resolved anything for the other.
    assert subscription.sandbox.env == {}


def test_a_workspace_cannot_resolve_a_credential_it_does_not_own():
    """Tenancy: the scoped SecretProvider refuses, and the factory surfaces it."""
    from regente.adapters import registry
    from regente.ports import Capability

    with pytest.raises(KeyError, match="not resolvable"):
        registry.create(Capability.RUNNER, "headless-agent", {
            "command": ["x"], "auth": "resolved_secret",
            "credentials": {"K": "env:ANOTHER_CLIENTS_SECRET"},
            "secrets": Secrets()})


def test_a_credential_shaped_auth_mode_without_credentials_is_refused():
    from regente.adapters import registry
    from regente.ports import Capability

    with pytest.raises(KeyError, match="needs a `credentials` mapping"):
        registry.create(Capability.RUNNER, "headless-agent", {
            "command": ["x"], "auth": "resolved_secret"})


# ---------------------------------------------------------------------------
# 10 -- the Core knows no vendor, and swapping one is a file
# ---------------------------------------------------------------------------

# The scan for vendor names and credential shapes above `adapters/` lives in
# `tests/test_agent_boundaries.py`, together with the rest of the structural
# audit and its one narrow exemption. It was duplicated here first; two copies
# of a rule drift, and the copy that drifts is the one nobody is reading.


def test_10b_a_second_vendor_needed_no_change_above_the_adapter_line(tmp_path):
    """Two vendors, one port, one loop. Neither is special anywhere above."""
    first = ClaudeCodeAgent(cli_path=signed_in(tmp_path))
    second = CodexCliAgent(cli_path=fake_cli(tmp_path, "codex",
                                             auth="Logged in as someone"))

    for agent in (first, second):
        assert isinstance(agent, AgentRunner)
        state = agent.availability()
        assert state.executable.ok is True
        assert state.authentication.ok is True, agent.name
        assert not state.capabilities.runs_commands, (
            f"{agent.name} must not be handed command execution")


def test_10c_the_engine_composes_the_two_axes_the_adapter_may_not_claim(tmp_path):
    agent = ClaudeCodeAgent(cli_path=signed_in(tmp_path))

    ready = readiness.diagnose(
        agent, policy=PolicyEngine.from_config(ALLOW),
        autonomy=AutonomyLevel.L2, ceiling_usd=10.0, spent_usd=1.0,
        max_dispatches=20, dispatches_today=3)
    assert ready.readiness is Readiness.READY

    refused = readiness.diagnose(agent, policy=PolicyEngine.from_config(DENY))
    assert refused.readiness is Readiness.BLOCKED_POLICY

    broke = readiness.diagnose(
        agent, policy=PolicyEngine.from_config(ALLOW),
        ceiling_usd=5.0, spent_usd=5.0)
    assert broke.readiness is Readiness.BLOCKED_BUDGET


def test_no_policy_configured_is_not_permission(tmp_path):
    agent = ClaudeCodeAgent(cli_path=signed_in(tmp_path))
    state = readiness.diagnose(agent, policy=None)
    assert state.readiness is Readiness.BLOCKED_POLICY
    assert "absence of a rule is not permission" in state.policy.detail


def test_an_adapter_that_breaks_while_diagnosing_itself_is_a_diagnosis():
    class Broken(AgentRunner):
        name = "broken"

        def run(self, mission):
            raise NotImplementedError

        def availability(self):
            raise RuntimeError("the vendor SDK blew up on import")

    state = readiness.diagnose(Broken())
    assert state.readiness is Readiness.BLOCKED_EXECUTABLE
    assert "blew up on import" in state.executable.detail


def test_the_deterministic_agent_needs_nobody():
    state = DeterministicAgent().availability()
    assert state.auth_mode is AuthMode.NONE
    assert state.authentication.ok is True
    assert "no credential" in state.authentication.detail
