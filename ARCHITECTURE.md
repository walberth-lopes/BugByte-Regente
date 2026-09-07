# Architecture

Regente is an **operating system for software engineering agents**. The agent
works, the Orchestrator coordinates, the tools execute, the policies protect, and
the queue calls the human only when the answer does not exist inside the system.

## The layers

```
                    cli / ui          surface
                       │
                   app/              composition root — the only place
                       │             that knows config + adapters + engine
        ┌──────────────┴──────────────┐
     engine/                      adapters/
   orchestrator                jira, github, gcloud…
   scheduler                   (every tool name lives here)
   store, gate                        │
   supervisor                         │
        └──────────────┬──────────────┘
                    ports/            capability contracts
                       │
                    core/             pure domain: no I/O, no provider
```

The dependency always points inwards. `core/` imports nothing; `engine/` talks
only to `ports/`; adapters implement ports; `app/` ties it all together.

**This is not a convention — it is tested.** `tests/test_boundaries.py` reads the
source and fails if `core/` imports I/O, if `engine/` imports an adapter, or if a
tool's name appears in code (not in a docstring) inside `core/`, `engine/` or
`ports/`. The rule breaks in CI, not in code review.

## The three invariants

**1. State by difference, on disk.** The engine never depends on an agent's
conversation context to know where the work stopped. Task, Run, Event, Approval
and Lease live in SQLite. Killing the process in the middle of a dispatch is a
supported operation: the lease expires, the run becomes `INTERRUPTED`, the task
returns to the queue and the next tick carries on. The recurring tick **is** the
retry mechanism — there is no retry code in the middle of the flow.

**2. No critical action depends on the LLM's judgement.** Every tool request
crosses the gate:

```
Agent → ToolRequest → [risk] → [policy] → ALLOW / DENY / HUMAN_APPROVAL → Tool
```

The gate redoes the judgement from scratch. It accepts no pre-computed risk, no
justification and no verdict from the caller. An agent talked round by a PR
comment still runs into it — because what decides does not read opinion.

**3. External content is data, never an order.** Task title, description,
comment, diff: all of it is text written by third parties. An attempt at
manipulation becomes a finding, not an instruction.

## Risk and policy decide different things

This is the distinction that defines the product, and the one that keeps the
owner from becoming the bottleneck.

| | question it answers | effect of "high" |
|---|---|---|
| **Policy** | *who may do this?* | `HUMAN_APPROVAL` — the organisation decided this signature has an owner |
| **Risk** | *how much proof does this action demand?* | an adversarial second pass — it buys **work**, not waiting |

If high risk became a waiting queue, every good PR would sit waiting for a
signature — exactly the cost the engine exists to remove. What stops work is the
written rule, not the model's hesitation.

Orthogonal to both: the **autonomy ceiling** (L0 read → L4 production) per
workspace and project. Breaching the ceiling becomes `HUMAN_APPROVAL`, never
`DENY` — the human can still authorise it. What refuses outright is the rule.

The Policy Engine is **default deny**: an action with no rule allowing it does
not happen. And **the most restrictive wins**: a `DENY` is not cancelled by any
`ALLOW`, because file order must not decide security.

## Parallelism without collisions

The Orchestrator builds a dependency graph and the scheduler picks what runs now.
Before opening two slots it checks four things: dependencies completed, resource
conflict, cycle, and limits (slots, daily cap).

A conflict is **declared**, not guessed. Each task states which resource keys it
touches exclusively — `repo:api`, `migration:worker`, `file:src/auth.py` — and
any intersection is mutual exclusion. Detecting a genuine semantic conflict
requires reading the code: that is the job of an analysis agent, which feeds this
list. The scheduler never concludes on its own that two diffs "probably" coexist.

Reservation happens inside the plan itself: two candidates from the same tick
that share a resource do not go out together. Forgetting this is the classic way
to dispatch two workers onto the same migration.

## The state machine

Eighteen states, an explicit table, and a transition outside it raises an error.
Three properties that are not obvious:

- **`WAITING_HUMAN` remembers where it paused.** It is not a destination, it is a
  pause. The human says carry on, redo, or close out — but does not teleport the
  task: approving a deploy is not declaring the work finished.
- **Escalating is always possible.** From any non-terminal state there is a path
  to `WAITING_HUMAN`. The engine is never left without the option of stopping to
  ask.
- **An active state returns to the queue.** Dead worker → `READY`. Keeping the
  task in the active state "to preserve the progress" preserves nothing: the
  progress lives in the work area and in the branch, not in the label. The task
  would be alive on paper and stopped in practice — the worst possible failure
  mode, because nothing flags it.

What preserves the partial work is the **area being addressed by the task, not by
the attempt**: the resumption reopens the same tree, with the WIP commits.

## Recovery ladder

```
failed → retry → change of strategy → escalate to the human
```

Finite on purpose, and each rung has to be *different* from the previous one.
Retrying identically after a deterministic error is spending money more slowly.
On top of that, every agent has a ceiling on iterations, tool calls, cost and
time, and a non-progress detector that flags four patterns: same error, same
file, same test, same decision.

## What enters the NEEDS ME queue

A narrow criterion, because filling the queue is the guaranteed way to make the
owner stop reading it. Only what **has no answer inside the system** gets in:
human authority required by policy, an ambiguous contract that extra reading does
not resolve, an exhausted recovery ladder, or a backlog in a cycle.

**What does not get in:** high risk (it buys a second pass), a red test (it is
work), a transient error (it is a retry).

Each item carries a decision, not a diagnosis: what happened, why it matters,
what the agent already tried, options, recommendation, risk. Logs stay in the
event, on demand.

## The agent is an executor, never an authority

The contract answers four questions and refuses five.

```
Answers:                           Does not answer:
  May I run this agent?              Is this agent trustworthy?
  How do I run it?                   May it commit?
  What capabilities does it expose?  May it push?
  What happened when it ran?         May it open a PR?
                                     May it deploy?
```

The five on the right remain the Engine's and the Policy's. No adapter field
speaks about them, and a structural test enforces it -- a field with an
authority name on an adapter type would be the vendor voting on its own
permission.

### Readiness: six axes, two authorities

```
adapter -> executable | protocol | authentication | agent
engine  -> policy | budget
```

Only the adapter knows the first four. Only the engine may decide the last two
-- an adapter that filled in its own `policy=ALLOW` would be a vendor
authorising itself. Consequence: **an adapter alone never becomes READY**.

`UNKNOWN` blocks. "Could not check" never turns into "everything is fine", and
the first axis that fails is the one reported: fixing a later axis while an
earlier one is broken solves nothing.

### Authentication is the adapter's business

Five exchange formats, none preferred:

| Mode | Who holds the credential |
|---|---|
| `SESSION` | the tool itself (corporate subscription, CLI login, SSO) |
| `RESOLVED_SECRET` | the engine, scoped to the workspace |
| `GATEWAY` | the engine, for an intermediary |
| `DELEGATED` | a host process; never assumed, reported as `UNKNOWN` |
| `NONE` | nobody |

They are formats, not products -- which is why they can live in the port. A
diagnosis that said "variable X is missing" would give the wrong advice to every
client that authenticates some other way, which is most of them.

### Vendors do not know each other

```
core/ | ports/ | engine/
        v
  agent contract
        v
adapters/runner/            <- shared base and infrastructure
adapters/runner/vendors/    <- one module per vendor
```

A module in `vendors/` never imports another module in `vendors/`. A structural
rule, verified by AST, and it exists because the violation happened right here:
the second profile imported a helper from the first, nothing broke, the suite
stayed green, and the property this milestone asserts -- swapping agents is one
file -- had silently stopped being true.

Shared work moves up a level. What a second vendor would want is not, by
definition, vendor-specific.

### The constraint lives outside the model

The agent gets read and edit. It gets no tool that executes a command.
`git push`, `gh pr create`, `gcloud`, `terraform` and every escalation route
nobody has thought of yet are variations on a single capability, and denying
that capability closes all of them at once.

Two indirect routes remained, and both are closed:

- **`.git/config`.** The agent only edits files, but rewriting the remote turns
  a refused push into a permitted one. `git status` sees nothing inside `.git/`,
  so the guard is a fingerprint, not a diff.
- **The test suite.** The agent writes files, tests are files, and the engine
  runs the suite to reach a verdict -- so an agent that executes nothing could
  make the ENGINE execute for it. Writing tests is work we want; giving that
  code a badge is not. The verification environment is composed from scratch,
  the same as the agent's.

### Capability is not permission

`AgentCapabilities.runs_commands` says what the tool CAN do.
`Permissions.run_commands` says what the engine allows. From outside they look
alike and demand opposite answers: the first is a configuration to live with,
the second is a boundary to enforce.

## Multi-tenancy

`Organization → Client → Workspace → Project → Repository`, present from the very
first table. Every Task, Run and Event carries a `workspace_id`, and the external
identity is unique *per workspace* — the same `FAXINA-183` in two clients is two
tasks, and they never collide. Bolting tenancy on later would mean migrating
every table and reviewing every query; the forgotten query is precisely the one
that leaks client A's data to client B.

Credentials and autonomy live in the **workspace**, not in the project — that is
where the boundary between clients has to be inviolable.

## Decisions taken, and why

| decision | choice | reason |
|---|---|---|
| language | Python 3.13 | the agent and tooling ecosystem of the environment |
| persistence | SQLite + WAL behind the `Store` port | zero-ops, transactional, readable by hand; Postgres enters without touching the Core |
| atomicity | `BEGIN IMMEDIATE` on transition and lease | these are the two points where a duplicate dispatch is born |
| isolation | `WorkspaceProvider` port; a directory now, a worktree for code | worktree is native to git; a container is unnecessary weight today |
| agent execution | `AgentRunner` port | keeps the Core agnostic to the harness; the runner is swappable without touching the engine |
| config | YAML + policies in a separate file | a policy has to be reviewable and diffable without touching the rest |
| shadow | `true` by default | live mode is the owner's explicit decision, never a default |

## What does not exist yet

Deliberately: UI, real provider adapters, an agent that writes code, an
implemented LLMProvider, semantic conflict detection, deploy. The **ports** for
those exist and are stable; the implementations come in milestones 2–9 of the
[ROADMAP](ROADMAP.md).
