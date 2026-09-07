# -*- coding: utf-8 -*-
"""Running a coding agent that lives in another process, inside a sandbox.

`HeadlessAgent` speaks a plain protocol: the mission goes in as JSON on stdin,
a structured outcome comes back as JSON on stdout. Any headless agent fits
behind it, and the engine never learns which one.

The part that matters is not the protocol. It is `SandboxProfile`.

**A prompt is not a control.** Telling an agent "do not push" is a request to a
system whose whole job is to be persuasive about what it should do next. The
restriction has to exist outside the model, and there are exactly three places
it can live: the tools the process is given, the environment it inherits, and
the directory it can reach. This module owns all three.

The default profile grants no command execution at all. That is not caution, it
is architecture: the engine has to observe the tests independently for its
verdict to mean anything, so an agent running them buys nothing and costs the
entire authority boundary. `git push`, `gh pr create`, `gcloud`, `terraform` and
every other escalation route are variations of one capability -- running a
command -- and withholding that capability closes all of them at once, including
the ones nobody has thought of yet.

The environment is **composed, not inherited**. Passing `os.environ` through
would hand the agent every credential the engine holds: the task provider's
token, the repository token, anything else in the process. An agent that cannot
run commands still reads files, and a credential in its environment is a
credential in its context.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from ...core import childenv
from ...ports.agent import (AgentAvailability, AgentCapabilities, AgentRunner,
                            AuthMode, Check, Claim, ClaimedFile, ClaimedTest,
                            Finding, Mission, Outcome, ProcessStatus)

#: The closed vocabularies. An unrecognised word is ERROR, never success.
VALID_STATUS = {s.value for s in ProcessStatus}
VALID_CLAIM = {c.value for c in Claim}

#: Re-exported so an adapter reads one name. The rule itself lives in
#: `core/childenv.py`, because the engine's own test run needs the identical
#: one and two copies of a security rule drift.
BASE_ENV_ALLOWLIST = childenv.BASE_ALLOWLIST
CREDENTIAL_MARKS = childenv.CREDENTIAL_MARKS


def compose_env(allow: tuple[str, ...] = BASE_ENV_ALLOWLIST,
                extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build the child's environment from nothing, adding only what is named."""
    return childenv.compose(os.environ, allow, extra)


def _looks_credential(name: str) -> bool:
    return childenv.looks_credential(name)


@dataclass(frozen=True, slots=True)
class SandboxProfile:
    """What the child process is allowed to be.

    Every field defaults to the restrictive value. A profile built by someone
    who forgot a field grants nothing extra -- the same rule as `Permissions`,
    for the same reason.
    """
    #: Tools the agent may use, by name. Empty means "the implementation's
    #: minimum", never "all of them".
    allowed_tools: tuple[str, ...] = ()
    #: Tools explicitly refused, named even when `allowed_tools` already
    #: excludes them. A named refusal survives someone widening the allowlist.
    denied_tools: tuple[str, ...] = ()
    #: Whether the agent may run any command at all. False closes push, PR
    #: creation, deploy and every other escalation route in one move.
    allow_command_execution: bool = False
    allow_network: bool = False
    #: Extra environment variables, resolved by the caller from the engine's
    #: own SecretProvider. Never read from the ambient environment.
    env: dict[str, str] = field(default_factory=dict)
    env_allowlist: tuple[str, ...] = BASE_ENV_ALLOWLIST
    #: Hard ceiling on money, when the substrate can enforce one.
    max_cost_usd: float = 2.0

    def describe(self) -> dict[str, Any]:
        return {
            "allowed_tools": list(self.allowed_tools),
            "denied_tools": list(self.denied_tools),
            "command_execution": self.allow_command_execution,
            "network": self.allow_network,
            "env_vars_passed": len(self.env_allowlist) + len(self.env),
            "max_cost_usd": self.max_cost_usd,
        }


def parse_outcome(payload: Any) -> Outcome:
    """Turn a process's JSON into an outcome. Never trusts, always checks.

    A malformed payload is `ERROR`. Not a warning, not a partial parse with
    optimistic defaults: an agent that cannot report what it did has not
    demonstrated that it did anything.
    """
    if not isinstance(payload, dict):
        return Outcome(status=ProcessStatus.ERROR,
                       summary=f"expected an object, got {type(payload).__name__}")

    raw_status = str(payload.get("status", "")).upper()
    if raw_status not in VALID_STATUS:
        return Outcome(
            status=ProcessStatus.ERROR,
            summary=f"unknown status {raw_status!r}; expected one of "
                    f"{', '.join(sorted(VALID_STATUS))}")

    raw_claim = str(payload.get("claim", "NONE")).upper()
    claim = Claim(raw_claim) if raw_claim in VALID_CLAIM else Claim.NONE

    return Outcome(
        status=ProcessStatus(raw_status),
        summary=str(payload.get("summary", ""))[:2000],
        claim=claim,
        claimed_files=tuple(
            ClaimedFile(path=str(f.get("path", "")),
                        additions=int(f.get("additions") or 0),
                        deletions=int(f.get("deletions") or 0),
                        status=str(f.get("status", "modified")))
            for f in (payload.get("claimed_files") or []) if isinstance(f, dict)),
        claimed_tests=tuple(
            ClaimedTest(command=str(t.get("command", "")),
                        exit_code=(int(t["exit_code"])
                                   if t.get("exit_code") is not None else None),
                        output=str(t.get("output", ""))[:8000])
            for t in (payload.get("claimed_tests") or []) if isinstance(t, dict)),
        findings=tuple(
            Finding(kind=str(f.get("kind", "note")), text=str(f.get("text", "")),
                    path=str(f.get("path", "")))
            for f in (payload.get("findings") or []) if isinstance(f, dict)),
        blockers=tuple(str(b) for b in (payload.get("blockers") or [])),
        questions=tuple(str(q) for q in (payload.get("questions") or [])),
        escalation_requested=bool(payload.get("escalation_requested")),
        escalation_reason=str(payload.get("escalation_reason", ""))[:500],
        cost_usd=float(payload.get("cost_usd") or 0.0),
        tokens=int(payload.get("tokens") or 0),
        tool_calls=int(payload.get("tool_calls") or 0),
        duration_seconds=float(payload.get("duration_seconds") or 0.0))


@dataclass(slots=True)
class HeadlessAgent(AgentRunner):
    """Runs a headless coding agent as a child process, sandboxed.

    The contract with the process: it receives the mission as JSON on stdin and
    prints a JSON outcome on stdout. Its working directory is the isolated area,
    and nothing outside it is its business.
    """

    command: list[str]
    name: str = "headless"
    #: How this agent proves who it is. `NONE` suits a local or deterministic
    #: process; `RESOLVED_SECRET` and `GATEWAY` mean the engine resolved
    #: something through its own SecretProvider and put it in `sandbox.env`.
    #: The adapter never reads the ambient environment looking for a credential:
    #: doing so would make one authentication shape look like the only real one.
    auth_mode: AuthMode = AuthMode.NONE
    sandbox: SandboxProfile = field(default_factory=SandboxProfile)
    #: Grace above the mission's own process ceiling, for a process that is
    #: shutting down cleanly. Small: this is politeness, not a second budget.
    kill_grace_seconds: int = 20

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "command": " ".join(self.command[:2]),
                "sandbox": json.dumps(self.sandbox.describe())}

    def availability(self) -> AgentAvailability:
        capabilities = AgentCapabilities(
            edits_files=True, structured_output=True,
            runs_commands=self.sandbox.allow_command_execution,
            reaches_network=self.sandbox.allow_network)
        if not self.command:
            return AgentAvailability(
                auth_mode=self.auth_mode, adapter=self.name,
                capabilities=capabilities,
                executable=Check.no("no command is configured"))

        binary = shutil.which(self.command[0]) or (
            self.command[0] if os.path.isfile(self.command[0]) else None)
        if binary is None:
            return AgentAvailability(
                auth_mode=self.auth_mode, adapter=self.name,
                capabilities=capabilities,
                executable=Check.no(f"'{self.command[0]}' is not on PATH and is "
                                    f"not a file"))

        return AgentAvailability(
            auth_mode=self.auth_mode, adapter=self.name,
            capabilities=capabilities,
            executable=Check.yes(f"{binary}"),
            protocol=Check.yes("JSON on stdin, structured outcome on stdout"),
            authentication=self._authentication(),
            agent=Check.yes("the configured process"))

    def _authentication(self) -> Check:
        """Report the shape this workspace configured, never a guess about it."""
        if self.auth_mode is AuthMode.NONE:
            return Check.yes("this agent needs no credential")
        if self.auth_mode in (AuthMode.RESOLVED_SECRET, AuthMode.GATEWAY):
            if self.sandbox.env:
                return Check.yes(
                    f"the engine resolved {len(self.sandbox.env)} credential(s) "
                    f"for this workspace")
            return Check.no(
                "this workspace authenticates with a credential the engine "
                "resolves, and none was resolved")
        if self.auth_mode is AuthMode.SESSION:
            return Check.unknown(
                "this process is expected to hold its own session; it offers no "
                "way to ask, so the engine will not assume it")
        return Check.unknown(
            "a host process is expected to authenticate on the agent's behalf; "
            "the engine cannot verify that")

    def verify(self) -> None:
        if not self.command:
            raise ValueError("no command configured for the headless agent")
        if self.sandbox.allow_command_execution:
            # Loud on purpose. This is the configuration that reopens push, PR
            # creation and deploy, and it should never be reached by accident.
            raise ValueError(
                "this profile grants command execution to the agent, which "
                "reopens every authority the engine withholds. If it is really "
                "wanted, it must be stated in configuration and reviewed there")

    def run(self, mission: Mission) -> Outcome:
        payload = json.dumps(mission.as_dict(), ensure_ascii=False)
        started = time.monotonic()
        limit = mission.budget.max_process_seconds + self.kill_grace_seconds
        try:
            p = subprocess.run(
                self.command, input=payload, cwd=mission.allowed_root,
                capture_output=True, encoding="utf-8", errors="replace",
                env=compose_env(self.sandbox.env_allowlist, self.sandbox.env),
                timeout=limit)
        except subprocess.TimeoutExpired:
            return Outcome(
                status=ProcessStatus.TIMEBOX,
                summary=f"the process exceeded {limit}s and was killed",
                duration_seconds=time.monotonic() - started)
        except (OSError, ValueError) as e:
            return Outcome(status=ProcessStatus.ERROR,
                           summary=f"could not start the agent: {e}"[:300],
                           duration_seconds=time.monotonic() - started)

        duration = time.monotonic() - started
        stdout = (p.stdout or "").strip()
        if p.returncode != 0 and not stdout:
            return Outcome(
                status=ProcessStatus.ERROR,
                summary=f"exit {p.returncode}: {(p.stderr or '').strip()[:300]}",
                duration_seconds=duration, raw=(p.stderr or "")[:4000])
        try:
            parsed = json.loads(stdout or "null")
        except ValueError:
            return Outcome(
                status=ProcessStatus.ERROR,
                summary=f"output is not JSON: {stdout[:200]}",
                duration_seconds=duration, raw=stdout[:4000])

        outcome = parse_outcome(parsed)
        return _with_duration(outcome, duration, stdout)


def _with_duration(outcome: Outcome, duration: float, raw: str) -> Outcome:
    """Stamp the engine's measured wall time when the process reported none.

    Never overwritten when the process gave its own: it knows its internals
    better than the wrapper does, and the wrapper's number includes start-up.
    """
    if outcome.duration_seconds:
        return Outcome(**{**_fields(outcome), "raw": raw})
    return Outcome(**{**_fields(outcome), "duration_seconds": duration, "raw": raw})


def _fields(o: Outcome) -> dict[str, Any]:
    return {
        "status": o.status, "summary": o.summary, "claim": o.claim,
        "claimed_files": o.claimed_files, "claimed_tests": o.claimed_tests,
        "findings": o.findings, "blockers": o.blockers, "questions": o.questions,
        "escalation_requested": o.escalation_requested,
        "escalation_reason": o.escalation_reason, "cost_usd": o.cost_usd,
        "tokens": o.tokens, "tool_calls": o.tool_calls,
        "duration_seconds": o.duration_seconds, "raw": o.raw,
    }
