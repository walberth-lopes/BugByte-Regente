# Mapping: task system → domain

How a `TaskProvider` translates the outside vocabulary into the engine's
vocabulary. Every deviation is declared. Where there is no perfect equivalence,
the choice is the simplest one — and it is written down that it was a choice.

Surveyed against the real board on **06/09/2026**. Status names were not assumed:
they were read.

## Status → `ExternalStatus`

The Core knows no tool's status names. It knows **position in the life cycle**,
which every work system has.

| status at the source | internal status | consequence in the engine |
|---|---|---|
| `TO DO`, `BACKLOG` | `NOT_STARTED` | available — can be dispatched |
| `PLANNING` | `IN_ANALYSIS` | available |
| `CODING`, `IN PROGRESS` | `IN_PROGRESS` | **blocked**: somebody is already on it |
| `REVIEWING`, `IN REVIEW` | `IN_REVIEW` | blocked |
| `QA STAGING`, `QA PRODUCTION` | `IN_VALIDATION` | blocked |
| `DONE` | `COMPLETED` | does not even enter the engine |
| `CANCELLED`, `WON'T DO` | `CANCELLED` | does not enter |
| **anything else** | `UNKNOWN` | **blocked** + anomaly reported |

**Why `UNKNOWN` is not coerced.** A status outside the map means somebody
changed the process. Mapping it to the closest neighbour would make the engine
work on a premise nobody verified, silently. It blocks and reports.

**The only exception — a safety net by category.** The source classifies every
status as `new` / `indeterminate` / `done`, and that classification exists even
for statuses nobody mapped. It is used only for `done` and `new`: it avoids the
worst possible error — dispatching work that has already finished — without
faking precision in the middle. `indeterminate` stays `UNKNOWN`, because "it
is in the middle" does not say whether that is code, review or validation.

## Priority → integer

Lower runs first. Spaced 20 apart so a planner can adjust without colliding with
the value from the source.

| source | internal |
|---|---|
| Highest, Blocker, Critical | 10 |
| High, Major | 30 |
| Medium, Normal | 50 |
| Low, Minor | 70 |
| Lowest, Trivial | 90 |
| missing or unknown | 50 |

An unknown priority falls in the middle, and does **not** become an anomaly: an
odd priority is the opinion of whoever wrote the card, not a data defect.

## Links → edges of the graph

**Only `blocks` becomes an edge.** This is the mapping decision with the greatest
consequence, and the easiest to get wrong.

| link at the source | internal type | becomes a dependency? |
|---|---|---|
| `Blocks` / *is blocked by* (inward) | `blocks` | **yes** |
| `Blocks` / *blocks* (outward) | `related` | no — see below |
| `parent` (subtask) | `parent` | no |
| `Relates`, `Cloners`, `Problem/Incident` | `related` | no |
| `Duplicate` | `duplicates` | no |

**The direction.** A blocking link appears on both issues, with opposite ends.
`A is blocked by B` (inward) means **A depends on B**. `A blocks B` (outward) is
the same fact seen from the other side — and the correct edge belongs to B, which
has its own inward. Recording both sides as blocking would invert half the graph.

*Checked against the 28 real `Blocks` links on the board: none inverted.*

**Why hierarchy does not block.** A subtask does not wait for its parent to
finish — it is part of what the parent is. On the real board, **95 of 100** issues
had a parent and there were **229 non-blocking links against 8 blocks**. If
hierarchy or relatedness became a dependency, nothing on the board would be
executable.

## Resources → mutual exclusion in the scheduler

A task provider **does not know which files will be touched**. Inventing that
would be fixing the world. What it does know is hierarchy, and hierarchy is a
real signal: two subtasks of the same parent almost always touch the same code.

| `resources_by` | key emitted | effect |
|---|---|---|
| `parent` (default) | `parent:<KEY>` | siblings serialise, different parents parallelise |
| `project` | `project:<KEY>` | serialises the whole project |
| `none` | — | declares no conflict |

**It is a declared heuristic.** Real precision requires an agent reading the
code, and that is Milestone 5.

## Assignee, labels, description

- **Assignee** → `assignee`, the readable name. The tool's internal id does not
  cross the port.
- **Labels** → `labels`, preserved as text. They do not become resources or
  priority: they are context.
- **Description** → only in the detail, never in the listing. A listing of 100
  issues with full descriptions returned **726 KB** in the measurement.
  `partial=True` marks the record that came from the listing, so that "empty
  description" and "description not asked for" are not confused.

## Classified findings

Following the rule of not fixing the world in the wrong place:

| finding | class | where it was fixed |
|---|---|---|
| Hierarchy and relatedness became dependencies | **CORE BUG** | `engine/orchestrator.py` — only a blocking link becomes an edge |
| The engine dispatched work that already had people on it | **CORE BUG** | `engine/orchestrator.py` — relevance decides before the queue |
| The engine never re-read a change at the source | **MISSING CAPABILITY** | `engine/orchestrator.py` — `_refresh` per pass |
| `external_status` was free text and the Core could not decide | **MISSING CAPABILITY** | `ports/tasks.py` — `ExternalStatus` |
| The adapter never asked for the description | **MISSING CAPABILITY** | trimmed list / full detail |
| Snapshot name used randomised `hash()` | **CORE BUG** (mine) | `hashlib`, a name stable across processes |
| Issues with no description on the board | **DATA QUALITY** | reported as an anomaly; nothing fixed |
| Pagination by cursor, not by offset | **PROVIDER QUIRK** | handled in the adapter |
| A 726 KB response on a routine search | **PROVIDER QUIRK** | trimmed fields, never `*all` |
| A dependency pointing outside the JQL slice | **DATA QUALITY** | counted and reported, never invented |


---

# Mapping: repository → domain

Surveyed against 12 local clones and 28 real remote repositories, **06/09/2026**.

## Identity

**A name is never an identity.** A repository is `(provider, key)`, and the
engine scopes that by workspace before using it as a lock or as state:

```
RepoRef(provider, key).resource(workspace_id)
  → "repo:<workspace>/<provider>/<key>"
```

Three failure modes a bare name produces, all observed:

| failure | real evidence |
|---|---|
| directory ≠ repository | `scamchecker-legado/` points at `silverguard-br/scamchecker` |
| clients with same-named repos | two clients can have `api`; the global lock made one wait for the other |
| same repo via two providers | `git-local` and `github` both see `silverguard-br/scamchecker` |

The key comes from the **remote** when there is one; from the path only when
there is no remote — and `local/<dir>` marks that explicitly, rather than faking
an origin.

## Base branch

Read from the provider, **never assumed**. Order of attempts: `origin/HEAD` →
`origin/main` → `origin/master` → `origin/develop` → local. None of them is
`HEAD`: of the 12 real clones, **11 were on a work branch**, and reading `HEAD`
would derive new work from somebody else's half-finished code.

A repository with no base is `REPO_UNUSABLE` — it receives no work.

## Capabilities

`capabilities` says what the adapter **can** do; the policy says what it **may**
do. Mixing the two produces an `if can_write` scattered around, which nobody
audits.

The local adapter declares everything for reading **except** `READ_PULL_REQUESTS`:
plain git has no pull requests, that is a concept of the hosting service.
Declaring the absence is what lets the chain stop at `NO_CAPABILITY` before
spending a cycle.

## Classified findings — Milestone 4

| finding | class | where it was fixed |
|---|---|---|
| **`gh repo delete` crossed the gate** — allowlist by verb, not by invocation | **CORE BUG** | `adapters/repos/readonly.py` — whole invocation |
| The same in git: `remote set-url`, `config x y`, `branch -D`, `symbolic-ref A B` | **CORE BUG** | idem |
| `gh api -f k=v` sends a POST without `--method` | **PROVIDER QUIRK** | the flag is treated as a write |
| Lease global by key: same-named clients contended for the same lock | **CORE BUG** | schema v2, PK `(workspace, resource)` |
| Schema bump with no migration path | **MISSING CAPABILITY** | the `MIGRATIONS` ladder in the store |
| A task does not declare which repository it runs in | **MISSING CAPABILITY** | see [ALVO.md](ALVO.md) |
| Local directory ≠ repository name | **DATA QUALITY** | detected and flagged, never fixed |
| `scamchecker-crawler-engine` with no base branch | **DATA QUALITY** | becomes `REPO_UNUSABLE` |
