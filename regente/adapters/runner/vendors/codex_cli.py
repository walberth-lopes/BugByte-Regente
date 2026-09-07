# -*- coding: utf-8 -*-
"""A second coding-agent CLI profile. Its purpose is to prove a claim.

The claim is the architectural criterion for this milestone: swapping one coding
agent for another must not touch `core/`, `ports/` or `engine/`. A claim like
that is cheap to make and only demonstrated by doing it, so this file exists --
a whole second vendor, in one file, under one hundred lines of actual logic,
changing nothing above the adapter boundary.

**Its invocation is unverified.** The tool is not installed in the environment
where this was written, so the flags below are written from its documented
surface and have never been run. That is stated here and in `CAPABILITIES.md`
rather than left for someone to discover: an adapter whose argv nobody has
executed is IMPLEMENTED, not tested, and calling it anything else would be the
kind of flattering lie this project keeps refusing to tell.

What IS demonstrated, and does not depend on the tool being present: the port
sufficed. No new method, no new field, no engine change. `availability()`
answers the same six axes; `AuthMode.SESSION` means the same thing; the sandbox
withholds command execution the same way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ....ports.agent import (AgentCapabilities, Check, Mission, Outcome,
                            ProcessStatus)
from .. import mission_text
from ..cli_agent import CliAgent
from ..headless import SandboxProfile, parse_outcome

#: This tool names its sandbox levels rather than its tools. The engine asks for
#: the level that forbids running commands, for the same reason as everywhere
#: else: withholding one capability closes every escalation route at once.
READ_WRITE_NO_EXEC = "workspace-write"


def restricted_profile(max_cost_usd: float = 2.0,
                       env: dict[str, str] | None = None) -> SandboxProfile:
    return SandboxProfile(
        allowed_tools=("read", "write"),
        denied_tools=("exec", "shell", "network"),
        allow_command_execution=False, allow_network=False,
        env=dict(env or {}), max_cost_usd=max_cost_usd)


@dataclass(slots=True)
class CodexCliAgent(CliAgent):
    """The same contract, a different vendor, no change above `adapters/`."""

    name: str = "codex-cli"
    model: str = ""
    sandbox: SandboxProfile = field(default_factory=restricted_profile)

    FORBIDDEN_FLAGS: tuple[str, ...] = (
        "--dangerously-bypass-approvals-and-sandbox",
        "--full-auto", "--yolo", "danger-full-access",
    )

    def declared_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            edits_files=True, structured_output=True, resumable=True,
            runs_commands=self.sandbox.allow_command_execution,
            reaches_network=self.sandbox.allow_network)

    def auth_argv(self) -> list[str] | None:
        return [self.cli_path, "login", "status"]

    def read_auth(self, stdout: str, stderr: str, returncode: int) -> Check:
        """This tool answers in prose rather than JSON, so the profile reads prose.

        Reading a vendor's output shape is exactly the work an adapter exists to
        do. The engine never sees this text -- it receives a `Check`.
        """
        text = f"{stdout}\n{stderr}".strip().lower()
        if returncode == 0 and ("logged in" in text or "authenticated" in text):
            return Check.yes("the tool reports it is signed in")
        if "not logged in" in text or returncode != 0:
            return Check.no(
                "the tool reports it is not signed in. Authenticate it the way "
                "this workspace is meant to, or configure a different auth mode")
        return Check.unknown(
            f"the tool's sign-in state could not be read: {text[:120]}")

    def answer_instruction(self) -> str:
        """This tool takes no schema flag, so the shape has to travel in the text.

        Concretely different from the other profile, which is the point: a
        shared sentence would have left this one asking for "the required
        schema" without ever having described it.
        """
        return (f"A single JSON object on its own line, with the keys "
                f"`status` (FINISHED | NO_PROGRESS | NEEDS_HUMAN), `claim` "
                f"(COMPLETE | PARTIAL | BLOCKED | NEEDS_HUMAN | NONE), "
                f"`summary`, and optionally `claimed_files`, `blockers`, "
                f"`questions`. {mission_text.HONESTY_NOTE}")

    def argv(self, mission: Mission) -> list[str]:
        argv = [
            self.cli_path, "exec", self.prompt(mission),
            "--sandbox", READ_WRITE_NO_EXEC,
            "--cd", mission.allowed_root,
            "--json",
            "--skip-git-repo-check",
        ]
        if self.model:
            argv += ["--model", self.model]
        return [*argv, *self.extra_args]

    def parse(self, stdout: str, stderr: str, returncode: int,
              duration: float) -> Outcome:
        """Last JSON object on stdout wins; anything else is an error.

        This tool streams one JSON object per line. Reading the last one is a
        vendor detail; refusing prose is not -- that rule is the same for every
        adapter, because free text cannot be a control signal whoever emits it.
        """
        if not stdout:
            return Outcome(status=ProcessStatus.ERROR,
                           summary=f"the agent printed nothing (exit "
                                   f"{returncode}): {stderr[:200]}",
                           duration_seconds=duration, raw=stderr[:4000])
        payload: Any = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict) and "status" in parsed:
                payload = parsed
        if payload is None:
            return Outcome(
                status=ProcessStatus.ERROR,
                summary="the agent answered with no structured outcome; free "
                        "text cannot be used as a control signal",
                duration_seconds=duration, raw=stdout[:4000])

        outcome = parse_outcome(payload)
        return Outcome(
            status=outcome.status, summary=outcome.summary, claim=outcome.claim,
            claimed_files=outcome.claimed_files,
            claimed_tests=outcome.claimed_tests, findings=outcome.findings,
            blockers=outcome.blockers, questions=outcome.questions,
            escalation_requested=outcome.escalation_requested,
            escalation_reason=outcome.escalation_reason,
            cost_usd=outcome.cost_usd, tokens=outcome.tokens,
            tool_calls=outcome.tool_calls, duration_seconds=duration,
            raw=stdout[:4000])
