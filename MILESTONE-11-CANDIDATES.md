# Milestone 11 — the search for an eligible task

Recorded before any code was written, as the milestone requires. Nothing here
was created, edited or nudged into eligibility: this is the board as found on
2026-09-07.

## The pool

`statusCategory != Done AND assignee IS EMPTY` returned 50 issues across two
projects. Removing epics and ideas — neither is a unit of executable work —
leaves **20 concrete unassigned `TO DO` items**, all in project SG.

## Evaluation

| candidate | eligible | reason_if_rejected |
|---|---|---|
| SG-1341 API runtime source detection + alerting | NO | production API behaviour and alerting; operational impact |
| SG-1330 / SG-1329 replace CNPJ.ws with official registry | NO | multi-repository programme, production data source swap |
| SG-1337 / SG-1336 / SG-1335 / SG-1334 / SG-1333 / SG-1332 / SG-1331 CNPJ-RFB subtasks | NO | infrastructure provisioning, BigQuery materialisation, GCS crawler, cache capacity, canary rollout — every one is production infrastructure |
| SG-1066 / SG-599 bulk query pipeline | NO | substantial feature spanning server and API; not small, not reversible in one change |
| SG-1106 create webhook and publish as secret | NO | **secret creation**; explicitly forbidden |
| SG-1075 audit deploy of every project and server | NO | infrastructure audit; no single repository; no code change |
| SG-1074 review staging deploy and define per-environment model | NO | deployment configuration; operational risk |
| SG-317 investigate CNPJs without a tactic | NO | investigation, not a code change |
| SG-56 dataset monitoring | NO | monitoring/infrastructure |
| SG-143 / SG-161 CAM graph enrichment and ingestion service | NO | substantial features; multi-component |
| **SG-315** attach icon breaks when showing an error in CAM | **NO** | **see below** |

## SG-315, the only structurally plausible candidate

```
candidate        SG-315 "ícone de anexar B.O ta quebrando ao exibir erro no CAM"
eligibility      REJECTED
target           UNRESOLVED
target_evidence  NONE
risk             low — a layout defect
policy           would permit; L2 is enough for push and pull request
required_auth    headless coding agent — BLOCKED_AUTHENTICATION
reason_if_rejected
                 1. No resolvable target. The issue carries no repository
                    field, no label and no component. A search across the
                    organisation found no branch, no pull request and no commit
                    referencing SG-315. `cam-web` is a plausible home for a
                    front-end layout bug, but plausible is not evidence, and the
                    engine's own invariant refuses to start work on a target
                    resolved by inference: "everything succeeds, in the wrong
                    place" is the failure that produces no error at all.
                 2. The acceptance criteria live in a screenshot. The text says
                    the icon "breaks and goes inside the copy"; the pasted image
                    is what says where and how. An agent cannot satisfy a
                    criterion it cannot read, and a change made against a guess
                    is not a fix.
```

## Verdict

```
M11 real-world path = BLOCKED_BY_REAL_WORLD_INPUT
```

Two independent gates, either of which alone would block it:

```
eligible task    NO   no candidate has a target resolvable by evidence
agent            NO   BLOCKED_AUTHENTICATION (M7)
```

The agent gate. Read again at the end of the milestone, because a blocker
quoted from a run nobody can reproduce is not evidence:

```
adapter        : claude-code
auth mode      : SESSION
executable     : YES  -- ...\claude-code.1.260\claude.exe reports 2.1.260
protocol       : YES  -- structured output is requested by the invocation
authentication : NO   -- the tool reports it is not signed in
agent          : UNKNOWN  -- not reachable until authentication holds
policy         : YES  -- policy ALLOW: regra 'run'
budget         : YES  -- no ceiling configured
result         : BLOCKED_AUTHENTICATION
```

Two things about how that was obtained, because they change what it proves.

The **executable** is not on `PATH`; it lives under a versioned directory and
had to be named in full. An engine that resolved it by name would have reported
`BLOCKED_EXECUTABLE` and been right about the wrong thing.

The **policy** axis is open only because this reading used a throwaway config
with a single `agent.run` ALLOW. The repository's own `policies/default.yaml`
has no such rule, so `regente doctor` in `sombra-sg/` stops earlier:

```
FALHA  prontidao do agente   BLOCKED_POLICY -- nenhuma regra permite 'agent.run' em '*'
```

Both readings are true and neither was adjusted to look better. The scratch
policy exists so the axes *after* policy could be read at all; opening
`agent.run` in the repository's own policy set to make a report greener would
have been the kind of relaxation this milestone forbids.

No task was invented. No task was edited to become eligible. No credential was
borrowed from the interactive session, and no policy was loosened in the
repository to make an axis pass.

## Defects found while gathering this evidence

Reading the blocker end to end meant running the shipped config, which had not
been run since the rename to English. Three references were dangling:

| where | stale | effect |
|---|---|---|
| `sombra-sg/regente.yaml` | `policies: ../policies/padrao.yaml` | the file does not exist; the engine could not start at all |
| `sombra-sg/regente.yaml` | `workspace_provider: diretorio` | no such adapter; `KeyError` naming the three that do exist |
| `regente/adapters/registry.py` | reads `o["segredos"]`; composition writes `"secrets"` | the Jira HTTP transport could never be constructed -- `KeyError: 'segredos'` at composition, not a legible error |

The third is the same defect as the dispatch counter in Milestone 6: a writer
and a reader that disagreed after a rename, with nothing in between to notice.
A `KeyError` from inside a factory is what a silent rename looks like from the
outside, and it is why contract tests and migrations are treated here as safety
mechanisms rather than as quality.

## What was built instead

The milestone's own instruction: *implement only what can be validated without
inventing work*. The architectural half of M11 needs neither a real task nor a
real agent, and it is the half that was actually missing — `TESTING` was a dead
end. See `CAPABILITIES.md`, Milestone 11.
