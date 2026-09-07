# -*- coding: utf-8 -*-
"""Three questions that must never be answered by each other.

    Authority : what may the agent do?
    Budget    : how much may it consume?
    Readiness : is it usable right now?

They are easy to conflate and expensive to conflate. Paying more for an agent
must not buy it a wider licence, and a licence must not imply a wallet. An
agent at `L1` with a large budget is still an agent at `L1`; an agent at `L1`
with zero budget is still permitted the same things and simply cannot afford
them today.

The fake vendors at the bottom exist for a narrower reason: to prove `AuthMode`
is a protocol rather than a lookup table with vendor names filed off. They carry
invented names on purpose. If `SESSION` quietly meant "the one CLI we tested"
and `RESOLVED_SECRET` meant "the one API we tested", these two would not work,
and the generality claimed by this milestone would be decoration.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import pytest

from regente.adapters.runner.cli_agent import CliAgent
from regente.adapters.runner.headless import HeadlessAgent, SandboxProfile
from regente.core.policy import (AutonomyLevel, Effect, PolicyEngine,
                                 required_level)
from regente.engine import readiness
from regente.engine.readiness import RUN_ACTION
from regente.ports.agent import (AgentCapabilities, AuthMode, Budget, Check,
                                 Mission, Outcome, Permissions, ProcessStatus,
                                 Readiness)

ALLOW_EVERYTHING = [{"name": "all", "effect": "ALLOW", "match": {"action": "*"}}]


# ---------------------------------------------------------------------------
# The autonomy ladder, and what `agent.run = L1` does NOT grant
# ---------------------------------------------------------------------------

#: The ladder as this engine uses it. Documented in a test rather than only in
#: prose so that a later edit to `REQUIRED_LEVEL` has to come past a statement
#: of what the levels mean.
#:
#:   L0  observation only
#:   L1  isolated workspace mutation
#:   L2  external repository mutation
#:   L3  production-impacting mutation
#:   L4  human-equivalent authority
LADDER = {
    AutonomyLevel.L0: ("repo.read", "task.read", "ci.read", "cloud.read"),
    AutonomyLevel.L1: ("workspace.write", "agent.run", "repo.branch",
                       "repo.commit"),
    AutonomyLevel.L2: ("repo.push", "repo.pr.create", "task.write"),
    AutonomyLevel.L3: ("repo.pr.close", "repo.review"),
}


@pytest.mark.parametrize("level,actions", list(LADDER.items()))
def test_each_action_sits_where_the_ladder_says(level, actions):
    for action in actions:
        assert required_level(action) is level, (
            f"'{action}' is at {required_level(action).name}, not {level.name}")


def test_running_an_agent_grants_nothing_beyond_the_isolated_area():
    """The counter-proof for `agent.run = L1`.

    Every action that leaves the isolated area must demand a level ABOVE the
    one that starting an agent demands. Written as a comparison rather than as
    a list of expected values, so adding a new escaping action and forgetting
    to rank it fails here instead of shipping.
    """
    run_level = required_level(RUN_ACTION)
    assert run_level is AutonomyLevel.L1

    escapes_the_area = ("repo.push", "repo.pr.create", "repo.pr.close",
                        "repo.review", "task.write")
    for action in escapes_the_area:
        assert required_level(action).value > run_level.value, (
            f"'{action}' leaves the isolated area and must outrank agent.run")


def test_a_workspace_that_may_run_an_agent_may_not_thereby_push_or_merge():
    """Behavioural: an L1 ceiling permits the run and stops everything else.

    The ceiling turns ALLOW into HUMAN_APPROVAL rather than into DENY, which is
    the existing rule and the right one -- a person may still authorise it. What
    matters is that it never stays a plain ALLOW.
    """
    from regente.core.policy import Action, PolicyContext

    policy = PolicyEngine.from_config(ALLOW_EVERYTHING)

    def decide(kind: str):
        return policy.decide(PolicyContext(
            action=Action(kind=kind, resource="acme/api", environment="staging"),
            organization="o", client="c", workspace="w", project="p",
            agent="coder", risk="LOW", autonomy=AutonomyLevel.L1))

    assert decide(RUN_ACTION).effect == Effect.ALLOW
    assert decide("repo.commit").effect == Effect.ALLOW

    for escaping in ("repo.push", "repo.pr.create", "repo.review",
                     "repo.merge", "deploy.production", "task.write"):
        assert decide(escaping).effect != Effect.ALLOW, (
            f"an L1 workspace silently gained '{escaping}'")


def test_the_permissions_handed_to_an_agent_never_include_the_escaping_verbs():
    """The other half: even at a high ceiling, the agent is not given them."""
    default = Permissions()
    for verb in ("push", "open_pr", "deploy", "run_commands", "write_tasks",
                 "commit"):
        assert not getattr(default, verb)


# ---------------------------------------------------------------------------
# Budget is not authority, in either direction
# ---------------------------------------------------------------------------

def unlimited_agent(**kw) -> HeadlessAgent:
    return HeadlessAgent(command=[sys.executable, "-c", "pass"],
                         auth_mode=AuthMode.NONE, **kw)


@pytest.mark.parametrize("ceiling", [0.0, 0.01, 1000.0])
def test_budget_does_not_change_what_an_agent_is_allowed_to_do(ceiling):
    """A bigger wallet is still the same licence.

    Asserted on the policy decision, which is the thing that would have to
    change for a budget to have bought authority.
    """
    from regente.core.policy import Action, PolicyContext

    policy = PolicyEngine.from_config(ALLOW_EVERYTHING)
    decisions = {}
    for action in (RUN_ACTION, "repo.push", "repo.review"):
        decisions[action] = policy.decide(PolicyContext(
            action=Action(kind=action, resource="r", environment="staging"),
            organization="o", client="c", workspace="w", project="p",
            agent="coder", risk="LOW", autonomy=AutonomyLevel.L1)).effect

    assert decisions[RUN_ACTION] == Effect.ALLOW
    assert decisions["repo.push"] != Effect.ALLOW
    assert decisions["repo.review"] != Effect.ALLOW


def test_zero_budget_blocks_readiness_without_touching_authority():
    agent = unlimited_agent()
    broke = readiness.diagnose(agent, policy=PolicyEngine.from_config(
        ALLOW_EVERYTHING), autonomy=AutonomyLevel.L1,
        ceiling_usd=0.0, spent_usd=0.0)

    assert broke.readiness is Readiness.BLOCKED_BUDGET
    assert broke.policy.ok is True, (
        "the workspace is still permitted; it simply cannot afford it today")


def test_a_large_budget_does_not_rescue_a_denied_policy():
    agent = unlimited_agent()
    denied = readiness.diagnose(
        agent,
        policy=PolicyEngine.from_config(
            [{"name": "no", "effect": "DENY", "match": {"action": "agent.run"}}]),
        ceiling_usd=10_000.0, spent_usd=0.0)

    assert denied.readiness is Readiness.BLOCKED_POLICY
    assert denied.budget.ok is True


def test_the_two_axes_are_reported_separately_even_when_both_fail():
    agent = unlimited_agent()
    both = readiness.diagnose(
        agent,
        policy=PolicyEngine.from_config(
            [{"name": "no", "effect": "DENY", "match": {"action": "agent.run"}}]),
        ceiling_usd=1.0, spent_usd=5.0)

    assert both.policy.ok is False
    assert both.budget.ok is False
    # Policy is reported first: money is irrelevant where the work is forbidden.
    assert both.readiness is Readiness.BLOCKED_POLICY


def test_the_budget_type_carries_no_authority_field():
    """`Budget` counts consumption. A permission field here would merge them."""
    authority_words = ("push", "commit", "merge", "deploy", "review",
                       "allowed", "permitted", "level", "autonomy")
    for field in Budget.__dataclass_fields__:
        assert not any(word in field.lower() for word in authority_words), field


# ---------------------------------------------------------------------------
# AuthMode is a protocol, not a vendor table
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class MeadowlarkAgent(CliAgent):
    """An invented vendor whose tool signs itself in.

    No relation to anything real, which is the point: if `SESSION` had quietly
    become "the one CLI we happened to test", this would not work.
    """

    name: str = "meadowlark"
    auth_mode: AuthMode = AuthMode.SESSION

    def auth_argv(self) -> list[str] | None:
        return [self.cli_path, "whoami"]

    def read_auth(self, stdout: str, stderr: str, returncode: int) -> Check:
        if returncode == 0 and stdout.strip():
            return Check.yes(f"the tool reports the account {stdout.strip()}")
        return Check.no("the tool reports no account")

    def argv(self, mission: Mission) -> list[str]:
        return [self.cli_path, "--task", self.prompt(mission)]

    def parse(self, stdout, stderr, returncode, duration) -> Outcome:
        return Outcome(status=ProcessStatus.NO_PROGRESS, summary="stub")


@dataclass(slots=True)
class TidewaterAgent(CliAgent):
    """An invented vendor reached with a credential the engine resolves."""

    name: str = "tidewater"
    auth_mode: AuthMode = AuthMode.RESOLVED_SECRET

    def argv(self, mission: Mission) -> list[str]:
        return [self.cli_path, "run", self.prompt(mission)]

    def parse(self, stdout, stderr, returncode, duration) -> Outcome:
        return Outcome(status=ProcessStatus.NO_PROGRESS, summary="stub")


def whoami_cli(tmp_path, answer: str = "someone@example.invalid") -> str:
    import textwrap

    script = tmp_path / "meadowlark.py"
    script.write_text(textwrap.dedent(f"""
        import sys
        if sys.argv[1:2] == ["--version"]:
            print("meadowlark 9.9")
        elif sys.argv[1:2] == ["whoami"]:
            print({answer!r})
        sys.exit(0)
    """), encoding="utf-8")
    launcher = tmp_path / "meadowlark.cmd"
    launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
                        encoding="utf-8")
    return str(launcher if sys.platform == "win32" else script)


def version_only_cli(tmp_path) -> str:
    import textwrap

    script = tmp_path / "tidewater.py"
    script.write_text(textwrap.dedent("""
        import sys
        if sys.argv[1:2] == ["--version"]:
            print("tidewater 0.4")
        sys.exit(0)
    """), encoding="utf-8")
    launcher = tmp_path / "tidewater.cmd"
    launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
                        encoding="utf-8")
    return str(launcher if sys.platform == "win32" else script)


def test_an_invented_vendor_can_use_session_authentication(tmp_path):
    agent = MeadowlarkAgent(cli_path=whoami_cli(tmp_path))
    state = agent.availability()

    assert state.auth_mode is AuthMode.SESSION
    assert state.authentication.ok is True
    assert "someone@example.invalid" in state.authentication.detail
    assert state.readiness is Readiness.BLOCKED_POLICY


def test_an_invented_vendor_can_use_a_resolved_credential(tmp_path):
    agent = TidewaterAgent(
        cli_path=version_only_cli(tmp_path),
        sandbox=SandboxProfile(env={"TIDEWATER_ACCESS": "zzq-never-print-me"}))
    state = agent.availability()

    assert state.auth_mode is AuthMode.RESOLVED_SECRET
    assert state.authentication.ok is True
    assert "zzq-never-print-me" not in state.render(), (
        "a diagnosis gets pasted into tickets; a credential in one is leaked")


def test_the_same_invented_vendor_blocks_when_nothing_was_resolved(tmp_path):
    agent = TidewaterAgent(cli_path=version_only_cli(tmp_path),
                           sandbox=SandboxProfile(env={}))
    assert agent.availability().readiness is Readiness.BLOCKED_AUTHENTICATION


def test_two_invented_vendors_differ_only_in_their_adapter(tmp_path):
    """Different mechanisms, one port, one readiness path, no engine change."""
    session = MeadowlarkAgent(cli_path=whoami_cli(tmp_path))
    secret = TidewaterAgent(
        cli_path=version_only_cli(tmp_path),
        sandbox=SandboxProfile(env={"TIDEWATER_ACCESS": "zzq"}))

    policy = PolicyEngine.from_config(ALLOW_EVERYTHING)
    for agent in (session, secret):
        state = readiness.diagnose(agent, policy=policy,
                                   autonomy=AutonomyLevel.L1,
                                   ceiling_usd=5.0, max_dispatches=10)
        assert state.readiness is Readiness.READY, agent.name

    assert session.availability().auth_mode is not secret.availability().auth_mode


def test_the_diagnosis_renders_the_same_shape_for_any_adapter(tmp_path):
    """`doctor`'s output must not have been written around one vendor."""
    rendered = MeadowlarkAgent(cli_path=whoami_cli(tmp_path)).availability().render()
    for row in ("adapter", "auth mode", "executable", "protocol",
                "authentication", "agent", "policy", "budget", "result"):
        assert row in rendered
    assert "meadowlark" in rendered
    assert "SESSION" in rendered


def test_capabilities_are_declared_per_adapter_not_assumed():
    """A future agent may run commands. The record must be able to say so."""
    talkative = AgentCapabilities(edits_files=True, runs_commands=True,
                                  reaches_network=True, resumable=True)
    quiet = AgentCapabilities(edits_files=True)

    assert talkative.as_dict() != quiet.as_dict()
    assert not quiet.runs_commands
    assert not AgentCapabilities().edits_files, (
        "the empty capability record must claim nothing")


# ---------------------------------------------------------------------------
# Gaps a mutation sweep found: two UNKNOWNs nothing was holding
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SilentAgent(CliAgent):
    """A profile whose author forgot to say how the tool reports its sign-in.

    The interesting case, because it looks fine. The executable is there, the
    protocol is there, and the one question that decides whether anything can
    run has no way to be asked. Turning that into a `yes` broke no test until
    this one existed.
    """

    name: str = "silent"
    auth_mode: AuthMode = AuthMode.SESSION

    def argv(self, mission: Mission) -> list[str]:
        return [self.cli_path]

    def parse(self, stdout, stderr, returncode, duration) -> Outcome:
        return Outcome(status=ProcessStatus.NO_PROGRESS, summary="stub")


@dataclass(slots=True)
class HostedAgent(CliAgent):
    """A tool something else is supposed to have authenticated."""

    name: str = "hosted"
    auth_mode: AuthMode = AuthMode.DELEGATED

    def argv(self, mission: Mission) -> list[str]:
        return [self.cli_path]

    def parse(self, stdout, stderr, returncode, duration) -> Outcome:
        return Outcome(status=ProcessStatus.NO_PROGRESS, summary="stub")


def test_a_session_that_cannot_be_asked_is_never_assumed(tmp_path):
    """A tool that cannot be asked has not said yes."""
    agent = SilentAgent(cli_path=version_only_cli(tmp_path))
    state = agent.availability()

    assert state.executable.ok is True
    assert state.authentication.ok is None, "UNKNOWN, not a pass"
    assert "will not assume it" in state.authentication.detail
    assert state.readiness is Readiness.BLOCKED_AUTHENTICATION


def test_a_delegated_login_is_never_assumed_on_a_cli_either(tmp_path):
    """Covered on the process adapter and not here, which the sweep found."""
    agent = HostedAgent(cli_path=version_only_cli(tmp_path))
    state = agent.availability()

    assert state.authentication.ok is None
    assert "cannot verify" in state.authentication.detail
    assert state.readiness is Readiness.BLOCKED_AUTHENTICATION
    assert readiness.diagnose(
        agent, policy=PolicyEngine.from_config(ALLOW_EVERYTHING),
        ceiling_usd=5.0).readiness is Readiness.BLOCKED_AUTHENTICATION


def test_an_unhandled_auth_mode_blocks_rather_than_passes(tmp_path):
    """A mode added later without a branch must fail closed.

    The first version of this test used GATEWAY, which IS handled -- so it never
    reached the fallback it claimed to be testing and passed for the wrong
    reason. A mutation sweep found that. This one uses a mode the adapter has
    genuinely never heard of, which is the only way to exercise the last line.
    """
    from enum import Enum

    class FutureMode(str, Enum):
        HANDSHAKE = "HANDSHAKE"

    agent = SilentAgent(cli_path=version_only_cli(tmp_path))
    agent.auth_mode = FutureMode.HANDSHAKE

    state = agent.availability()
    assert state.authentication.ok is None, "an unknown mode must not pass"
    assert "unhandled auth mode HANDSHAKE" in state.authentication.detail
    assert state.readiness is Readiness.BLOCKED_AUTHENTICATION


def test_an_argument_that_disables_the_sandbox_is_refused_by_the_base(tmp_path):
    """The refusal lives on the base now, so every profile inherits it.

    The sweep skipped this guard because its anchor had moved into `CliAgent`;
    a guard nobody can locate is a guard nobody is checking.
    """
    from regente.adapters.runner.vendors.claude_code import ClaudeCodeAgent

    for flag in ("--dangerously-skip-permissions",
                 "--permission-mode=bypassPermissions"):
        agent = ClaudeCodeAgent(cli_path=version_only_cli(tmp_path),
                                extra_args=(flag,))
        with pytest.raises(ValueError, match="disables the permission checks"):
            agent.verify()


def test_command_execution_is_refused_by_the_base_for_every_profile(tmp_path):
    for kind in (MeadowlarkAgent, TidewaterAgent):
        agent = kind(cli_path=version_only_cli(tmp_path),
                     sandbox=SandboxProfile(allow_command_execution=True))
        with pytest.raises(ValueError, match="reopen"):
            agent.verify()
