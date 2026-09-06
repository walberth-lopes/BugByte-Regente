# Roadmap

Rule: every milestone delivers **working capability**, not structure for later.
A milestone does not close without proof on disk.

| # | Milestone | Closes when |
|---|---|---|
| **1** | **Core, state, policy, scheduler** ✅ | a tick discovers, builds the graph, dispatches in parallel, survives `kill -9` and escalates to the human |
| 2 | Mission Control (local UI) | the 4 questions on screen; deciding a queue item from the browser |
| **3** | **Real TaskProvider** ✅ | a real adapter reading the real board in shadow; the contract passes on two providers; zero mutation |
| **4** | **RepositoryProvider** ✅ | two real providers in shadow; identity within the tenancy; write contract declared, nothing implemented |
| 5 | CoderAgent | a worker that writes real code in an isolated worktree |
| 6 | CI + PR | the engine follows checks and reacts to red |
| 7 | ReviewerAgent | a review pinned to the reviewed SHA, with a second pass on high risk |
| 8 | CloudProvider | reading resources, logs and metrics |
| 9 | Deploy to staging | governed deploy + smoke, with rollback |
| 10 | Genuinely parallel workers | 2+ real simultaneous workers without collision |
| 11 | Mature escalation | notification outside the terminal; one-click decision |
| 12 | **Second client** | another set of providers running **without touching `core/` or `engine/`** |

Milestone 12 is the only honest test of the architecture. The other eleven can
pass with an engine secretly coupled to the first client.

## The order of the bets

A real adapter (3, 4) comes **before** an agent that writes code (5). The reason:
an agent writing code against a fake provider proves nothing, and it is the most
expensive thing to redo if the abstraction turns out to be wrong.

## Brakes, in every phase

`shadow: true` is born on. Turning it off is an explicit decision, after the
owner has read what the engine would have done and answered yes to the question:
*would I sign this with my name?*
