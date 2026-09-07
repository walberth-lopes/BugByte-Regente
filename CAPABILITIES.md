# Capabilities — what is proven, and by what

Four states, kept apart on purpose. Collapsing them is how a project convinces
itself it has done something it has not.

| State | What it means | What it does **not** mean |
|---|---|---|
| **IMPLEMENTED** | The code exists and imports. | That it has ever run. |
| **CONTRACT_TESTED** | Tests exercise it against fakes, fixtures or real local processes. Guards proven by mutation: break the guard, the test goes red. | That any real external service ever answered. |
| **EXERCISED_REAL** | It ran against the real external system, and the world was inspected afterwards. | — |
| **BLOCKED_EXTERNAL** | Ready, refusing to run, for a stated external reason. | That it is broken. |

A capability that is only CONTRACT_TESTED must never be cited as evidence that
the flow works. Fakes are obedient: every refusal in `tests/test_remote.py`
comes from the engine, never from a fake declining to cooperate — which makes
them good at proving guards and worthless at proving integration.

---

## Milestone 7 — real AgentRunner

### The headless-agent inventory, re-taken

The Milestone 5 inventory is no longer true, in both directions.

| Probed | Result |
|---|---|
| `claude`, `codex`, `aider`, `cursor-agent`, `gemini`, `goose`, `amp`, `opencode`, `copilot`, `windsurf`, `cline` on PATH | none present, POSIX or Windows |
| npm globals | `@nestjs/cli`, `corepack`, `npm` only |
| pip packages resembling an agent | none |
| the CLI named by `CLAUDE_CODE_EXECPATH` | **present and headless-capable, v2.1.260** |

One headless coding-agent binary does exist. It runs, it accepts every flag the
adapter builds, and it returns a well-formed JSON envelope.

### The blocker, stated without presuming a mechanism

An earlier version of this document said the blocker was a missing
`ANTHROPIC_API_KEY`. That was wrong, and wrong in a way worth keeping on the
record: it presumed one implementation. Clients do not all authenticate a coding
agent with an API key. A corporate subscription, a CLI sign-in, SSO, a workspace
credential, an internal gateway and a key are all ordinary, and an engine whose
diagnosis names a variable is an engine that gives wrong advice to everyone
using another mechanism.

So the diagnosis is six independent axes, and authentication is the adapter's
business:

```
adapter        : claude-code
auth mode      : SESSION
executable     : YES  -- reports 2.1.260
protocol       : YES  -- structured output is requested by the invocation
authentication : NO   -- the tool reports it is not signed in
agent          : UNKNOWN -- not reachable until authentication holds
policy         : YES  -- ALLOW
budget         : YES  -- within ceiling
result         : BLOCKED_AUTHENTICATION
```

That is the real output of `regente doctor` against the real binary. What is
missing is stated by the adapter in the adapter's own terms; the engine reports
only `BLOCKED_AUTHENTICATION`, which is actionable wherever the reader works.

The engine does **not** borrow the interactive session's credential, and does not
look in the environment for one. Both would be authority no policy governs and no
workspace owns.

**What would unblock it:** authenticating this workspace's agent by whatever
mechanism the workspace configures -- signing the CLI in, or setting
`auth: resolved_secret` / `auth: gateway` with a `credentials` mapping the
workspace's own `SecretProvider` can resolve. The engine supports all of them and
prefers none.

### Authentication shapes the engine understands

Shapes of exchange, never products. `core/`, `ports/` and `engine/` contain no
vendor name and no credential shape -- asserted by a test, not by intention.

| Mode | Who holds the credential | Example |
|---|---|---|
| `SESSION` | the tool itself | a corporate coding-agent subscription, a CLI sign-in, SSO |
| `RESOLVED_SECRET` | the engine, scoped to the workspace | an API key named in `secrets:` |
| `GATEWAY` | the engine, for an intermediary | an internal LLM gateway |
| `DELEGATED` | a host process | never assumed to work; reported `UNKNOWN` |
| `NONE` | nobody | a deterministic or local agent |

Swapping the agent is a file in `adapters/`. Demonstrated rather than asserted:
a second vendor profile exists (`codex_cli.py`), and adding it required no change
to `core/`, `ports/` or `engine/`. **Its argv is unverified** -- the tool is not
installed here, so the flags come from its documented surface and have never been
executed. That is IMPLEMENTED, not tested, and it is listed as such below.

### Capability status, axis by axis

`AgentRunner` is not one capability, and reporting it as one would hide exactly
the distinction that matters. Seven rows, because six of them are green and the
seventh is the only one anybody actually wants.

| # | Capability | State | Evidence |
|---|---|---|---|
| 1 | Adapter executable detection | **EXERCISED_REAL** | probes the real binary; reports its real version |
| 2 | Protocol (structured in, structured out) | **EXERCISED_REAL** | the real CLI accepts every flag the adapter builds; its real envelope parses |
| 3 | Authentication state | **EXERCISED_REAL** | the real tool was asked and answered: not signed in |
| 4 | Agent (model) availability | **BLOCKED_EXTERNAL** | unreachable until authentication holds; reported `UNKNOWN`, never assumed |
| 5 | Policy | CONTRACT_TESTED | `tests/test_authority_budget.py` |
| 6 | Budget | CONTRACT_TESTED | `tests/test_authority_budget.py` |
| 7 | **Real model execution** | **BLOCKED_EXTERNAL** | never happened; see below |

The distinction rows 1-3 and row 7 record, stated so it cannot be softened later:

```
real CLI                 : YES
real authenticated model : NO
```

The binary is real, was executed, and answered. No model has run. Nothing in
this milestone has produced a line of code written by a language model, and no
green suite should ever be read as though it had.

| Supporting capability | State | Evidence |
|---|---|---|
| `AgentRunner` port, single and vendor-free | IMPLEMENTED | `ports/agent.py`; the duplicate port is gone |
| Structured `Mission`, eleven required fields | CONTRACT_TESTED | `test_a_mission_tells_the_agent_what_it_may_not_do` |
| Structured `Outcome`, claims named as claims | CONTRACT_TESTED | parse tests and real-envelope tests |
| Independent observation of the work area | CONTRACT_TESTED against **real git repositories** | `tests/test_agent.py` |
| Escape detection outside the work area | CONTRACT_TESTED against a **real filesystem** | sentinel tests |
| Authority-path detection | CONTRACT_TESTED | parametrised violation tests |
| Anti-loop: retry only on new information | CONTRACT_TESTED | `tests/test_agent.py` |
| Context Engine with a reason per item | CONTRACT_TESTED | context tests |
| Sandbox: no command execution | CONTRACT_TESTED through **real child processes** | argv asserted; environment asserted through a real subprocess |
| Five authentication shapes | CONTRACT_TESTED | ten scenarios, real executables for the probes |
| Two independent vendor profiles | IMPLEMENTED; one **argv unverified** | `vendors/`; the second tool is not installed here |
| No vendor adapter depends on another | CONTRACT_TESTED **by deletion** | a copy of the package with one vendor removed still imports the other |
| A new vendor needs no change above `adapters/` | CONTRACT_TESTED **by addition** | a vendor written into a copy, run through the engine's readiness path, neutral layers byte-identical afterwards |

### The twelve mandatory negatives

Each ends in an explicit state. None can reach implicit success.

| # | Scenario | Ends as |
|---|---|---|
| 1 | claims success, changed nothing | `NO_CHANGE` + `claimed_not_found` discrepancies |
| 2 | claims tests passed, tests fail | `REGRESSED` |
| 3 | modifies a file outside the expected area | `NEEDS_HUMAN` + `escape` violation |
| 4 | attempts a workspace escape silently | `NEEDS_HUMAN` + `escape` violation |
| 5 | attempts `git push` | no command tool exists; reaching for it via `.git/config` is `NEEDS_HUMAN` |
| 6 | attempts pull-request creation | no command tool exists; `open_pr` is withheld and named |
| 7 | attempts deploy or cloud mutation | no command tool exists; CI/deploy paths are guarded |
| 8 | times out | `TIMEBOX`, real process really killed |
| 9 | malformed structured output | `ERROR`, including valid JSON with invented vocabulary |
| 10 | process crashes | `ERROR` with exit code and stderr |
| 11 | repeated identical failed attempts | stops before the budget, saying nothing new could be learned |
| 12 | verification tool unavailable | `READY_FOR_REVIEW` stating nothing verified it -- never green |

### Mutation sweep

Guards are only guards if removing them breaks a test. Thirty-two mutations, each
applied, the suites run, the source restored.

**29 caught.** The three that were not, and what they exposed:

| Mutation | First result | What was missing |
|---|---|---|
| Assume a session is authenticated when the tool offers no way to be asked | MISSED | no profile in the suite lacked an auth probe |
| Assume a delegated login works | MISSED | covered on the process adapter, not on the CLI base |
| Stop refusing flags that disable the sandbox | SKIP | the guard had moved into the base class and the sweep could not find it |

All three now caught. A fourth appeared while closing them: an auth mode the
adapter has never heard of fell through as passing, and the test written to catch
it used a mode that *is* handled -- so it never reached the fallback and passed
for the wrong reason. The sweep found that too.

That is the argument for running these at all. Every one of the four was a guard
that looked present, read correctly, and was holding nothing.

### The sandbox, stated exactly

The agent is given `Read`, `Edit`, `Write`, `Glob`, `Grep`. It is given no tool
that runs a command. `git push`, `gh pr create`, `gcloud`, `terraform` and every
escalation route nobody has thought of yet are variations of one capability, and
withholding that capability closes all of them at once -- which a denylist of
known-bad commands cannot do.

Four flags, each enforced by the CLI process rather than by the model:
`--restricted`, `--tools`, `--disallowed-tools`, `--permission-prompts none`.
Every one verified as accepted by the real binary. The environment is composed
from empty rather than inherited, so no credential the engine holds reaches the
agent.

---

## Milestone 6 — remote delivery

| Capability | State | Evidence |
|---|---|---|
| Push contract (`WorkspaceProvider.push`, `expected_sha` required) | CONTRACT_TESTED | `tests/test_push_target.py`, `tests/test_remote.py` |
| Push refuses integration branches, foreign branches, local targets, SHA drift, force | CONTRACT_TESTED | `tests/test_push_target.py` |
| GitHub write adapter (`pr create` only) | CONTRACT_TESTED | `tests/test_remote.py::test_the_write_gate_*` |
| `create_pull_request` with a mandatory run marker | CONTRACT_TESTED | `test_a_pull_request_carries_the_run_marker` |
| `get_pull_request` / read-back after creation | IMPLEMENTED | no test drives the real CLI |
| CI observation, read-only | CONTRACT_TESTED | `test_ci_is_asked_about_the_commit_not_the_pull_request` |
| CI classification (6 results, 4 states) | CONTRACT_TESTED | `tests/test_remote.py`, CI section |
| Persisting `task → run → commit → PR → CI` | CONTRACT_TESTED | `test_the_ci_observation_is_persisted`, store tests |
| PR identity by head SHA + marker | CONTRACT_TESTED | seven adoption tests |
| Policy + identity + SHA revalidated per mutation | CONTRACT_TESTED | mutation-checked (see below) |
| **The whole chain against a real repository** | **BLOCKED_EXTERNAL** | no eligible task — see below |

### What BLOCKED_EXTERNAL means here, precisely

Nothing in this milestone has been pushed anywhere. No pull request has been
opened. No real CI has been observed. The gate is not technical: the criteria
for an eligible task (small, reversible, no production path, no infrastructure,
no secrets, not owned by a person, real acceptance criteria) are not met by any
task currently on the board, and inventing one to complete the exercise would
make the proof worthless — it would demonstrate that the engine can act on a
task written to let it act.

The engine stops at `NEEDS_HUMAN`. That is the correct outcome, not a failure.

### Mutation checks

Guards are only guards if removing them breaks a test. Each was deleted, the
suite run, and the guard restored:

| Mutation | Result |
|---|---|
| Drop the SHA revalidation before push | caught |
| Drop the identity check before push | caught |
| Adopt a PR on marker alone, ignoring the head | caught |
| Adopt an unmarked pull request | caught |
| Report provider unavailability as a CI result | caught |
| Let confirmed absence of checks read as `PASSED` | caught |
| Call a red check a regression with no baseline | caught |
| Drop the one-PR-per-workspace constraint | caught |
| Stop refusing `--web` on a permitted write | caught |
| Widen the write allowlist to `pr merge` / `pr review` | *not* caught — correctly: the forbidden list still refuses |
| Empty the forbidden list, allowlist left narrow | *not* caught — correctly: the allowlist still refuses |
| Break **both** allowlist layers at once | caught |

The last three are the interesting ones. The write gate is two independent
layers, and no single-layer edit lets a merge through.

---

## Earlier milestones

| Capability | State | Evidence |
|---|---|---|
| Core, persistent state, policy, scheduler, recovery | EXERCISED_REAL | M1, `8d3e5de` |
| Jira task provider, read-only | EXERCISED_REAL | M3, `49a8e73` — real site, real issues |
| Repository provider, read-only | EXERCISED_REAL | M4, `4e37827` |
| Target resolution, DECLARED and DISCOVERED | EXERCISED_REAL | M5, `MILESTONE-5-PROOF.txt` |
| Agent run + validation loop + commit on an isolated branch | EXERCISED_REAL | M5, `8aac885` — both paths, real repos, forbidden mutations verified by inspecting the world |

---

## Authority, unchanged

Allowed at L2: push of an execution branch, opening a pull request, observing
CI. Forbidden regardless of level: merge, approve, review, deploy, production
change, writing back to the external task, triggering or cancelling CI.

The agent holds none of these. It produces a commit; producing a commit does not
entitle it to publish one. `regente/engine/remote.py` never reads the agent's
`Outcome`, and a test asserts that structurally.
