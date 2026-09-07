# Where does this task run?

Milestone 4 investigation. **Measured on 06/09/2026** against 100 real tasks and
12 real repositories — not estimated.

## The finding

**The information is missing, not hidden.**

| signal | coverage | any good? |
|---|---|---|
| component field (the *natural* field for this) | **0/100** | does not exist on the board |
| existing branch naming the task key | **14/100**, 2 ambiguous | yes, when it exists |
| label naming a repository | 72/100 match `scamchecker` | **no** — see below |
| title naming a repository | 11/100 | no, noisy |
| project | 100/100, but 1 project → 12 repos | does not discriminate |

The label looks promising and is not: `scamchecker` is at once the name of **one**
repository and the name of the **whole product**, which has twelve. Matching by
text there would swap "I do not know" for "I got it wrong confidently" — which,
in an engine that is going to write code, is infinitely worse.

## What was implemented

Evidence collection with declared confidence. **Never a guess.**

```
DECLARED  somebody stated it explicitly          (weight 100)
OBSERVED  the world shows work already started   (weight  50)
AMBIGUOUS a tie — the engine does NOT break it
ABSENT    no evidence — the engine does not invent
```

`DECLARED` beats `OBSERVED` because a branch can be the leftovers of an abandoned
attempt, whereas a map is a statement by someone who knows. A tie becomes a
question for the human, not a choice.

Key matching is by **whole word**: `K-1` does not match `K-11`. And a short name
ambiguous between two repositories does not enter the index — that would
reintroduce the guess through the back door.

## The future contract between TaskProvider and RepositoryProvider

What is missing is not code, it is **declared data**. In order of preference:

1. **The task provider emits the target** — a field of its own, a component, or a
   label convention. It is the only source that does not age, because whoever
   writes the task knows where it runs. *Cost: one process decision, zero code in
   the engine.*
2. **A map in the workspace configuration** (`by_label`, `by_project`, `by_task`).
   It covers the common case with zero guessing, and is already implemented.
3. **An analysis agent reads the code and proposes the target with evidence.**
   Expensive, and last for that reason — but the only one that resolves a new
   task in a new repository.

**None of the three requires changing the Core.** `ExternalTask.resources` and
`data` already carry the result, wherever it comes from.

## What the missing capability is worth, measured

The same 100 tasks, the same evidence, changing one axis at a time:

```
autonomy ceiling     L0 → NEEDS_HUMAN 1        L2 → CANDIDATE 1
declared map         without → AMBIGUOUS_TARGET 2, CANDIDATE 1
                     with 1 entry → AMBIGUOUS_TARGET 1, CANDIDATE 2
```

One line of map turned an ambiguity into an executable candidate, with the
evidence recorded:

```
SG-1195  DECLARED
  SG-1195 -> silverguard-br/scamchecker-dashboard-api;
  'chore/SG-1195-remove-deploy-staging-obsoleto-da-main' names SG-1195
  base=main  branch=regente/sg-1195
  resource=repo:wks_sg/git-local/silverguard-br/scamchecker-dashboard-api
```

## The chain, and where it stops

```
task → candidate repository → context → base branch
     → resources/isolation → risk/policy → execution candidate
```

Every link can reject, and rejecting is normal. The value lies in saying **at
which link** it stopped — "there is nothing to do", "I do not know where to do
it" and "I may not do it" demand opposite actions from the owner, and a single
number would hide them.

Against the real board, with no declared map, at L0:

```
NO_WORK             36   the source says somebody is already on it
NO_TARGET           61   ← the bottleneck: the declaration that is missing
AMBIGUOUS_TARGET     2   the engine refuses to break the tie
NEEDS_HUMAN          1   good evidence, but the autonomy ceiling is L0
Mutations            0
```

**The bottleneck is not the engine.** It is the information nobody declares.
