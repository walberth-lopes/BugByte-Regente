# Roadmap

Rule: every milestone delivers **working capability**, not structure for later.
A milestone does not close without proof on disk.

| # | Milestone | Closes when |
|---|---|---|
| **1** | **Core, state, policy, scheduler** ✅ | a tick discovers, builds the graph, dispatches in parallel, survives `kill -9` and escalates to the human |
| 2 | Mission Control (local UI) | the 4 questions on screen; deciding a queue item from the browser |
| **3** | **Real TaskProvider** ✅ | a real adapter reading the real board in shadow; the contract passes on two providers; zero mutation |
| **4** | **RepositoryProvider** ✅ | two real providers in shadow; identity within the tenancy; write contract declared, nothing implemented |
| **5** | **AgentRunner + validation loop** ✅ | mission selected, target resolved, clone isolated, agent run, test with a baseline, verdict and commit -- DECLARED and DISCOVERED proved end to end |
| **6** | **CI + PR** ⚠ | the full path implemented and tested against the contract; **real remote proof blocked at `NEEDS_HUMAN`** for want of an eligible task |
| **7** | **Real AgentRunner** ⚠ | the engine observes the world for itself and does not believe the agent; sandbox with no command execution; **real model blocked** for want of a credential the engine can resolve |
| 8 | ReviewerAgent | a review pinned to the SHA reviewed, with a second pass on high risk |
| 9 | CloudProvider | reading resources, logs and metrics |
| 10 | Deploy to staging | governed deploy + smoke, with rollback |
| 11 | Genuinely parallel workers | 2+ real workers at once with no collision |
| 12 | Mature escalation | notification outside the terminal; a one-click decision |
| 13 | **Second client** | another set of providers running **without touching `core/` or `engine/`** |

The second-client milestone is the only honest test of the architecture. Every
other one can pass with an engine secretly coupled to the first client.

⚠ = capability finished and proved against the contract, with the real
integration blocked by a named external dependency. See `CAPABILITIES.md`: a
green suite never substitutes for real proof.

## The order of the bets

A real adapter (3, 4) comes **before** an agent that writes code (5). The reason:
an agent writing code against a fake provider proves nothing, and it is the most
expensive thing to redo if the abstraction turns out to be wrong.

## Brakes, in every phase

`shadow: true` is born on. Turning it off is an explicit decision, after the
owner has read what the engine would have done and answered yes to the question:
nome?*
