# -*- coding: utf-8 -*-
"""Adapter for one specific headless coding agent CLI.

This is the only file in the project that knows this tool exists. The engine
sees `AgentRunner`; swapping this for another headless agent is a line of
configuration and no change above the adapter boundary. The vendor's name lives
here and in the registry, which is exactly where a vendor's name is allowed to
live.

**How the boundary is actually held.** The tool can, in its normal
configuration, run arbitrary shell commands -- which would give it `git push`,
`gh pr create`, `gcloud`, and every other route out of the sandbox. So it is not
run in its normal configuration. Four flags, each enforced by the CLI process
rather than by the model:

  ``--restricted``      removes the command-running tools, ignores user/project
                        settings files, confines the file tools to the working
                        directory, and refuses permission bypass.
  ``--tools``           an explicit allowlist from the built-in set. The engine
                        names reading and editing. It never names a shell.
  ``--disallowed-tools`` names the shells again anyway. Belt and braces: an
                        allowlist widened by a later edit still hits this.
  ``--permission-prompts none``  nobody can approve anything. There is no human
                        at the other end of a headless run, and a prompt that
                        cannot be answered must deny rather than wait.

Without a command tool there is no push, no pull request, no deploy and no task
write-back -- not because the agent was asked to abstain, but because the verbs
do not exist in the process. That closes the escalation routes nobody has
thought of yet, which a denylist of known-bad commands cannot do.

`permission_denials` in the tool's own output is kept and turned into findings.
When the agent reaches for something it was not given, the engine learns that it
tried -- from the sandbox, not from the agent's summary.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from ....ports.agent import (AgentCapabilities, AuthMode, Check, Finding,
                            Mission, Outcome, ProcessStatus)
from .. import mission_text
from ..cli_agent import CliAgent
from ..headless import SandboxProfile, parse_outcome

#: Tools the agent is given. Reading and editing, nothing that runs anything.
#: `Bash`, `PowerShell` and every other command tool are absent by construction.
DEFAULT_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")

#: Named again as refused, so widening the allowlist later does not silently
#: grant a shell.
DENIED_TOOLS = ("Bash", "PowerShell", "Task", "WebFetch", "WebSearch",
                "NotebookEdit", "Agent")

#: The shape the agent must answer in. Free text is not a control surface, so
#: the engine asks the tool to validate the structure for it -- and validates
#: again on this side regardless.
OUTCOME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "claim", "summary"],
    "properties": {
        "status": {"type": "string",
                   "enum": ["FINISHED", "NO_PROGRESS", "NEEDS_HUMAN"]},
        "claim": {"type": "string",
                  "enum": ["COMPLETE", "PARTIAL", "BLOCKED", "NEEDS_HUMAN", "NONE"]},
        "summary": {"type": "string"},
        "claimed_files": {
            "type": "array",
            "items": {"type": "object", "required": ["path"],
                      "properties": {"path": {"type": "string"},
                                     "additions": {"type": "integer"},
                                     "deletions": {"type": "integer"},
                                     "status": {"type": "string"}}}},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
        "findings": {
            "type": "array",
            "items": {"type": "object", "required": ["kind", "text"],
                      "properties": {"kind": {"type": "string"},
                                     "text": {"type": "string"},
                                     "path": {"type": "string"}}}},
        "escalation_requested": {"type": "boolean"},
        "escalation_reason": {"type": "string"},
    },
}


def restricted_profile(max_cost_usd: float = 2.0,
                       env: dict[str, str] | None = None) -> SandboxProfile:
    return SandboxProfile(
        allowed_tools=DEFAULT_TOOLS, denied_tools=DENIED_TOOLS,
        allow_command_execution=False, allow_network=False,
        env=dict(env or {}), max_cost_usd=max_cost_usd)


@dataclass(slots=True)
class ClaudeCodeAgent(CliAgent):
    """One profile of `CliAgent`. Everything vendor-shaped lives in these hooks.

    It authenticates by `AuthMode.SESSION` by default -- the tool holds its own
    login, whether that came from a personal sign-in, a corporate account or
    SSO, and the engine neither resolves nor stores anything. A workspace that
    prefers a resolved credential sets `auth_mode` and supplies it through the
    sandbox; the rest of this class does not change, and neither does anything
    above `adapters/`.
    """

    name: str = "claude-code"
    model: str = "sonnet"
    sandbox: SandboxProfile = field(default_factory=restricted_profile)

    #: Flags that would undo the sandbox. Refused wherever they come from.
    FORBIDDEN_FLAGS: tuple[str, ...] = (
        "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
        "--permission-mode=bypassPermissions", "bypassPermissions",
    )

    def declared_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            edits_files=True, structured_output=True, resumable=True,
            # The sandbox withholds command execution and the network; the
            # capability record says what the tool COULD do, which is why the
            # sandbox exists at all.
            runs_commands=self.sandbox.allow_command_execution,
            reaches_network=self.sandbox.allow_network,
            reports_refused_attempts=True)

    def answer_instruction(self) -> str:
        """This tool receives the schema as a flag, so the prompt can point at it."""
        return (f"A JSON object matching the schema passed to this invocation. "
                f"{mission_text.HONESTY_NOTE}")

    def auth_argv(self) -> list[str] | None:
        return [self.cli_path, "auth", "status"]

    def read_auth(self, stdout: str, stderr: str, returncode: int) -> Check:
        """Read the tool's own account of its sign-in state.

        The engine never sees a credential here and does not want to. It asks a
        yes/no question and reports the answer, which is the whole of what
        `SESSION` authentication permits it to know.
        """
        try:
            state = json.loads(stdout.strip() or "{}")
        except ValueError:
            return Check.unknown(
                f"the tool's sign-in state could not be read: {stdout[:120]}")
        if not isinstance(state, dict):
            return Check.unknown("the tool answered in an unexpected shape")
        if state.get("loggedIn"):
            method = state.get("authMethod") or "unspecified"
            return Check.yes(f"the tool reports it is signed in ({method})")
        return Check.no(
            "the tool reports it is not signed in. Authenticate it the way this "
            "workspace is meant to -- a sign-in, a corporate account, SSO -- or "
            "configure a different auth mode for this workspace")

    # ------------------------------------------------------------------
    def argv(self, mission: Mission) -> list[str]:
        """The full invocation. Every restriction visible in one place."""
        budget = min(self.sandbox.max_cost_usd, mission.budget.max_cost_usd)
        return [
            self.cli_path,
            "--print", self.prompt(mission),
            "--output-format", "json",
            "--model", self.model,
            # The sandbox, in four flags. See the module docstring.
            "--restricted",
            "--tools", ",".join(self.sandbox.allowed_tools),
            "--disallowed-tools", ",".join(self.sandbox.denied_tools),
            "--permission-prompts", "none",
            "--permission-mode", "acceptEdits",
            # No ambient configuration: no MCP servers, no project settings, no
            # skills. Whatever this run does must come from the mission.
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--setting-sources", "",
            "--no-session-persistence",
            # Money and shape.
            "--max-budget-usd", f"{budget:.2f}",
            "--json-schema", json.dumps(OUTCOME_SCHEMA),
            *self.extra_args,
        ]

    def parse(self, stdout: str, stderr: str, returncode: int,
              duration: float) -> Outcome:
        return parse_envelope(stdout, stderr, returncode, duration)


def parse_envelope(stdout: str, stderr: str, returncode: int,
                   duration: float) -> Outcome:
    """Unwrap the CLI's own JSON, then the structured answer inside it.

    Two layers, and both can fail independently: the CLI may report an API error
    in a well-formed envelope, or return a well-formed envelope whose payload is
    prose. Neither becomes success. An agent whose answer cannot be read has not
    demonstrated that it did anything -- the same rule as everywhere else.
    """
    if not stdout:
        return Outcome(status=ProcessStatus.ERROR,
                       summary=f"the agent printed nothing (exit {returncode}): "
                               f"{stderr[:200]}",
                       duration_seconds=duration, raw=stderr[:4000])
    try:
        envelope = json.loads(stdout)
    except ValueError:
        return Outcome(status=ProcessStatus.ERROR,
                       summary=f"the agent's output is not JSON: {stdout[:200]}",
                       duration_seconds=duration, raw=stdout[:4000])
    if not isinstance(envelope, dict):
        return Outcome(status=ProcessStatus.ERROR,
                       summary=f"expected an object, got "
                               f"{type(envelope).__name__}",
                       duration_seconds=duration, raw=stdout[:4000])

    denials = _denials(envelope)
    usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    cost = float(envelope.get("total_cost_usd") or 0.0)
    tokens = int((usage or {}).get("input_tokens") or 0) + \
             int((usage or {}).get("output_tokens") or 0)
    turns = int(envelope.get("num_turns") or 0)

    if envelope.get("is_error"):
        reason = str(envelope.get("result") or
                     envelope.get("terminal_reason") or "unspecified")
        status = (ProcessStatus.BUDGET
                  if "budget" in reason.lower() or
                     envelope.get("terminal_reason") == "budget_exceeded"
                  else ProcessStatus.ERROR)
        return Outcome(status=status,
                       summary=f"the agent reported an error: {reason[:300]}",
                       findings=denials, cost_usd=cost, tokens=tokens,
                       tool_calls=turns, duration_seconds=duration,
                       raw=stdout[:4000])

    result = envelope.get("result")
    payload = result if isinstance(result, dict) else _as_json(result)
    if payload is None:
        return Outcome(
            status=ProcessStatus.ERROR,
            summary="the agent answered with prose where a structured outcome "
                    "was required; free text cannot be used as a control signal",
            findings=denials, cost_usd=cost, tokens=tokens, tool_calls=turns,
            duration_seconds=duration, raw=str(result)[:4000])

    outcome = parse_outcome(payload)
    return Outcome(
        status=outcome.status, summary=outcome.summary, claim=outcome.claim,
        claimed_files=outcome.claimed_files, claimed_tests=outcome.claimed_tests,
        findings=outcome.findings + denials, blockers=outcome.blockers,
        questions=outcome.questions,
        escalation_requested=outcome.escalation_requested,
        escalation_reason=outcome.escalation_reason,
        cost_usd=cost or outcome.cost_usd, tokens=tokens or outcome.tokens,
        tool_calls=turns or outcome.tool_calls, duration_seconds=duration,
        raw=stdout[:4000])


def _as_json(text: Any) -> dict | None:
    """Read a JSON object out of the result text, or refuse.

    Tolerates a fenced code block, because that is a formatting habit rather
    than a control decision. Tolerates nothing else: anything looser would let
    prose steer the engine, which is the failure this whole design is built
    against.
    """
    if not isinstance(text, str):
        return None
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    body = body.strip()
    if not body.startswith("{"):
        return None
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _denials(envelope: dict) -> tuple[Finding, ...]:
    """Turn the CLI's own refusals into findings.

    This is the sandbox reporting on the agent, not the agent reporting on
    itself, which is what makes it worth keeping. An agent that reached for a
    shell leaves a record here even when its summary says nothing about it.
    """
    raw = envelope.get("permission_denials")
    if not isinstance(raw, list):
        return ()
    findings: list[Finding] = []
    for item in raw[:20]:
        if isinstance(item, dict):
            tool = str(item.get("tool_name") or item.get("tool") or "?")
            detail = json.dumps(item.get("tool_input") or {},
                                ensure_ascii=False)[:300]
        else:
            tool, detail = str(item)[:60], ""
        findings.append(Finding(
            kind="denied_attempt",
            text=f"the agent attempted '{tool}', which the sandbox refused: "
                 f"{detail}"))
    return tuple(findings)
