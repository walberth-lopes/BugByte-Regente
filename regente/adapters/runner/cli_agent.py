# -*- coding: utf-8 -*-
"""Base for any headless coding-agent CLI, whatever authenticates it.

The thing this file exists to prevent: an engine that can only work where an API
key exists. Clients do not all have one. A client may bring a corporate coding-
agent subscription and no key at all; another a key and no subscription; another
routes everything through an internal gateway; another signs in through SSO once
and expects the tool to remember. All four are ordinary.

So authentication is the ADAPTER's business, and the engine's business is only to
ask "can you run?" and to report the answer at a granularity a person can act on:

    executable -> protocol -> authentication -> agent -> policy -> budget

The first four this class answers by probing. The last two belong to the engine
and are deliberately left `UNKNOWN` here -- an adapter that filled in its own
`policy=ALLOW` would be a vendor granting itself permission.

`AuthMode.SESSION` is the interesting one and the one an API-key-shaped design
gets wrong. The engine resolves nothing, stores nothing, and never reads the
credential; it asks the tool whether it is signed in and reports what the tool
said. A subclass supplies the probe, because only it knows how its tool answers
that question.

Subclass and override four hooks. Nothing above `adapters/` changes when you do.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field

from ...ports.agent import (AgentAvailability, AgentCapabilities, AgentRunner,
                            AuthMode, Check, Mission, Outcome, ProcessStatus)
from . import mission_text
from .headless import SandboxProfile, compose_env


@dataclass(slots=True)
class CliAgent(AgentRunner):
    """A coding agent that lives behind a command-line tool."""

    cli_path: str
    name: str = "cli-agent"
    model: str = ""
    auth_mode: AuthMode = AuthMode.SESSION
    sandbox: SandboxProfile = field(default_factory=SandboxProfile)
    kill_grace_seconds: int = 30
    probe_timeout: int = 60
    #: Extra flags from configuration, checked against `FORBIDDEN_FLAGS`.
    extra_args: tuple[str, ...] = ()

    #: Flags that would undo the sandbox, whatever their source.
    FORBIDDEN_FLAGS: tuple[str, ...] = ()

    # ---- hooks a profile overrides ------------------------------------
    def version_argv(self) -> list[str]:
        return [self.cli_path, "--version"]

    def auth_argv(self) -> list[str] | None:
        """Argv that asks the tool about its own sign-in state.

        `None` means the tool offers no such question -- which is an honest
        `UNKNOWN`, not a pass. A tool that cannot be asked has not said yes.
        """
        return None

    def read_auth(self, stdout: str, stderr: str, returncode: int) -> Check:
        raise NotImplementedError

    def answer_instruction(self) -> str:
        """How THIS tool wants its structured answer requested.

        A hook rather than shared text, and the reason is not style. "Reply with
        JSON matching the required schema" reads agnostic and is not: it
        presumes the schema arrived out of band, which is true of a tool that
        takes a schema flag and false of one that does not. A profile without a
        flag has to inline the schema, and inheriting the other sentence would
        leave it asking for a shape it never described.
        """
        return mission_text.HONESTY_NOTE

    def prompt(self, mission: Mission) -> str:
        """The mission as text, plus this profile's answer instruction."""
        return mission_text.render(mission, self.answer_instruction())

    def argv(self, mission: Mission) -> list[str]:
        raise NotImplementedError

    def parse(self, stdout: str, stderr: str, returncode: int,
              duration: float) -> Outcome:
        raise NotImplementedError

    def declared_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(edits_files=True, structured_output=True)

    # ---- the diagnosis -------------------------------------------------
    def availability(self) -> AgentAvailability:
        base = AgentAvailability(auth_mode=self.auth_mode, adapter=self.name,
                                 capabilities=self.declared_capabilities())
        rc, out, err = self._probe(self.version_argv())
        if rc is None:
            return _replace(base, executable=Check.no(
                f"'{self.cli_path}' is not runnable: {err}"))
        if rc != 0:
            return _replace(base, executable=Check.no(
                f"'{self.cli_path} --version' exited {rc}: {err[:160]}"))
        executable = Check.yes(f"{self.cli_path} reports {out.strip()[:60]}")

        try:
            protocol = self.check_protocol()
        except NotImplementedError:
            protocol = Check.unknown("this adapter does not probe its protocol")

        authentication = self.check_authentication()
        # The agent itself cannot be reached before authentication holds, and
        # saying UNKNOWN is the honest answer rather than guessing either way.
        agent = (Check.unknown("not reachable until authentication holds")
                 if not authentication else self.check_agent())
        return _replace(base, executable=executable, protocol=protocol,
                        authentication=authentication, agent=agent)

    def check_protocol(self) -> Check:
        return Check.yes("structured output is requested by the invocation")

    def check_authentication(self) -> Check:
        """Ask the tool, or say the engine cannot know.

        Deliberately never inspects the environment for a credential. Doing so
        would make one authentication shape look like the only real one and give
        a wrong diagnosis to every client using another.
        """
        if self.auth_mode is AuthMode.NONE:
            return Check.yes("this agent needs no credential")

        if self.auth_mode is AuthMode.SESSION:
            argv = self.auth_argv()
            if argv is None:
                return Check.unknown(
                    "the tool offers no way to report its sign-in state; the "
                    "engine cannot confirm it and will not assume it")
            rc, out, err = self._probe(argv)
            if rc is None:
                return Check.no(f"could not ask the tool: {err}")
            return self.read_auth(out, err, rc)

        if self.auth_mode in (AuthMode.RESOLVED_SECRET, AuthMode.GATEWAY):
            # The engine resolved something and handed it over at construction.
            # Whether the far side accepts it is not knowable without spending
            # money, so a present credential is `yes` and an absent one is `no`
            # -- and an invalid one surfaces at the first run, as an error.
            if self.sandbox.env:
                return Check.yes(
                    f"the engine resolved {len(self.sandbox.env)} credential(s) "
                    f"for this workspace")
            return Check.no(
                "this workspace is configured to authenticate with a resolved "
                "credential and none was resolved")

        if self.auth_mode is AuthMode.DELEGATED:
            return Check.unknown(
                "a host process is expected to authenticate on the agent's "
                "behalf; the engine cannot verify that and will not assume it")

        return Check.unknown(f"unhandled auth mode {self.auth_mode.value}")

    def check_agent(self) -> Check:
        return Check.yes(f"model '{self.model}' requested" if self.model
                         else "the tool's default agent")

    def verify(self) -> None:
        if self.sandbox.allow_command_execution:
            raise ValueError(
                "command execution would reopen push, pull request creation and "
                "deploy; this adapter refuses to run with it enabled")
        for flag in self.extra_args:
            if any(bad in flag for bad in self.FORBIDDEN_FLAGS):
                raise ValueError(f"refused extra argument '{flag}': it disables "
                                 f"the permission checks the sandbox relies on")
        state = self.availability()
        if not state.executable:
            raise ValueError(state.blocking_reason())

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "model": self.model or "(default)",
                "auth_mode": self.auth_mode.value,
                "command_execution": "denied",
                "tools": ",".join(self.sandbox.allowed_tools)}

    # ---- running -------------------------------------------------------
    def run(self, mission: Mission) -> Outcome:
        started = time.monotonic()
        limit = mission.budget.max_process_seconds + self.kill_grace_seconds
        try:
            p = subprocess.run(
                self.argv(mission), cwd=mission.allowed_root,
                capture_output=True, encoding="utf-8", errors="replace",
                env=compose_env(self.sandbox.env_allowlist, self.sandbox.env),
                timeout=limit, input="")
        except subprocess.TimeoutExpired:
            return Outcome(status=ProcessStatus.TIMEBOX,
                           summary=f"the agent exceeded {limit}s and was killed",
                           duration_seconds=time.monotonic() - started)
        except (OSError, ValueError) as e:
            return Outcome(status=ProcessStatus.ERROR,
                           summary=f"could not start the agent: {e}"[:300],
                           duration_seconds=time.monotonic() - started)
        return self.parse((p.stdout or "").strip(), (p.stderr or "").strip(),
                          p.returncode, time.monotonic() - started)

    # ---- probing -------------------------------------------------------
    def _probe(self, argv: list[str]) -> tuple[int | None, str, str]:
        """Run a short read-only command. `None` return code means it never ran.

        The environment is composed here too. A probe that inherited the parent
        environment would leak credentials into a process the engine started for
        no reason other than to ask a question.
        """
        try:
            p = subprocess.run(argv, capture_output=True, encoding="utf-8",
                               errors="replace", timeout=self.probe_timeout,
                               env=compose_env(self.sandbox.env_allowlist,
                                               self.sandbox.env),
                               input="")
        except (OSError, subprocess.SubprocessError) as e:
            return None, "", f"{type(e).__name__}: {e}"[:200]
        return p.returncode, (p.stdout or ""), (p.stderr or "")


def _replace(a: AgentAvailability, **kw) -> AgentAvailability:
    fields = {"auth_mode": a.auth_mode, "adapter": a.adapter,
              "executable": a.executable, "protocol": a.protocol,
              "authentication": a.authentication, "agent": a.agent,
              "policy": a.policy, "budget": a.budget,
              "capabilities": a.capabilities}
    return AgentAvailability(**{**fields, **kw})
